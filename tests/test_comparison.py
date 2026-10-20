import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from PIL import Image

from claim_agent.comparison import register_comparison_routes
from claim_agent.config import Settings
from claim_agent.models import Book, Price
from claim_agent.pipeline import SweepRunner
from claim_agent.storage import EvidenceStore
from claim_agent.vision import Scan, Detection
from tests.test_geometry import frame


class FakeProvider:
    def __init__(self):
        self.usage = []
        self.closed = False

    async def close(self):
        self.closed = True


class FakeVision:
    def __init__(self, provider, seen, gate=None, fail=False):
        self.provider, self.seen, self.gate, self.should_fail = provider, seen, gate, fail

    async def inspect(self, jpeg, metadata):
        self.seen.append((jpeg, metadata.model_copy(deep=True)))
        if self.gate:
            await self.gate.wait()
        if self.should_fail:
            raise RuntimeError('private provider error must not leak')
        return Scan(detections=[Detection(kind='book', polygon=[[.1,.1],[.2,.1],[.2,.4],[.1,.4]])])


class ComparisonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = EvidenceStore(Path(self.tmp.name))
        self.settings = Settings(data_dir=Path(self.tmp.name), queue_size=1, finish_timeout=2, local_first=False)
        self.source = self.store.create('LiDAR test', 'India', 'INR', synthetic=True)
        self.source.metrics['capture_models'] = {'provider':self.settings.provider, 'vision_model':self.settings.vision_model}
        out=io.BytesIO()
        Image.new('RGB',(1000,1000),(80,120,150)).save(out,format='JPEG')
        self.jpeg=out.getvalue()
        for n in range(3):
            meta=frame().model_copy(update={'id':f'f{n}'})
            self.source.frames[meta.id]=meta
            self.store.put_evidence(self.source,f'frames/{meta.id}.jpg',self.jpeg)
            self.store.put_evidence(self.source,f'depth/{meta.id}.bin',bytes(40000))
        self.source.geometry={'do_not_inherit':True}
        self.source.books=[Book(id='owner-corrected',frame_ref='frames/f0.jpg',title='Owner title',user_claims=['owner claim'],replacement_cost=Price(amount='99'))]
        self.source.completion='partial'
        self.source.metrics['time_to_packet_s']=8
        self.store.save(self.source)
        self.seen=[]
        self.created=[]
        self.gate=None
        self.should_fail=False
        def make_runner(state):
            provider=FakeProvider()
            runner=SweepRunner(state,self.store,self.settings,FakeVision(provider,self.seen,self.gate,self.should_fail),None)
            runner.provider=provider
            self.created.append(runner)
            return runner
        self.source_runner=SweepRunner(self.source,self.store,self.settings,None,None)
        async def runner_for(id):
            if id!=self.source.sweep.id: raise HTTPException(404)
            return self.source_runner
        async def auth(request:Request):
            if request.headers.get('authorization')!='Bearer test': raise HTTPException(401)
        self.app=FastAPI()
        self.active=set()
        register_comparison_routes(self.app,auth,runner_for,make_runner,{},self.store,self.settings,self.active)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://test',headers={'authorization':'Bearer test'})
        self.addAsyncCleanup(self.client.aclose)
        self.url=f'/api/sweeps/{self.source.sweep.id}/camera-comparison'

    async def test_independent_same_jpegs_bounded_replay_and_unknowns(self):
        response=await self.client.post(self.url)
        self.assertEqual(response.status_code,200,response.text)
        result=response.json()
        self.assertEqual(result['status'],'complete')
        # All three fixture frames contain exactly the same JPEG. The independent
        # camera runner recognizes it once and records the other two as duplicates.
        self.assertEqual(len(self.seen),1)
        self.assertTrue(all(jpeg==self.jpeg and meta.depth_width==meta.depth_height==0 for jpeg,meta in self.seen))
        camera=self.created[0].state
        self.assertEqual(camera.sweep.synthetic,self.source.sweep.synthetic)
        self.assertFalse(camera.geometry)
        self.assertFalse(any(ref.startswith(('depth/','geometry/')) for ref in camera.evidence))
        self.assertFalse(any(b.id=='owner-corrected' or b.user_claims for b in camera.books))
        self.assertEqual(camera.frame_status,{'f0':'processed','f1':'duplicate_suppressed','f2':'duplicate_suppressed'})
        self.assertEqual(camera.metrics['duplicate_images_suppressed'],2)
        self.assertIsNone(result['deltas_camera_minus_lidar']['floor_area_m2'])
        self.assertIsNone(result['accuracy']['book_count'])
        self.assertFalse(result['timing']['directly_comparable'])
        self.assertIsNone(result['camera']['cost_usd'])
        self.assertEqual(len(result['provenance']['frames']),3)
        self.assertTrue(self.created[0].provider.closed)
        self.assertFalse(self.created[0].workers)
        self.assertTrue(any('duplicate' in n.lower() and 'tracking' in n.lower() for n in result['limitations']))
        self.assertEqual(self.source.books[0].replacement_cost.amount,99)

    async def test_complete_retry_and_get_do_not_repeat_provider_calls(self):
        first=(await self.client.post(self.url)).json()
        second=(await self.client.post(self.url)).json()
        self.assertEqual(first,second)
        self.assertEqual((await self.client.get(self.url)).json(),first)
        self.assertEqual(len(self.created),1)

    async def test_parallel_post_is_rejected_without_duplicate_cost(self):
        self.gate=asyncio.Event()
        task=asyncio.create_task(self.client.post(self.url))
        for _ in range(100):
            if self.seen: break
            await asyncio.sleep(.005)
        second=await self.client.post(self.url)
        self.assertEqual(second.status_code,409,second.text)
        self.assertEqual(second.json()['detail']['code'],'already_running')
        self.gate.set()
        self.assertEqual((await task).status_code,200)
        self.assertEqual(len(self.created),1)

    async def test_live_handoff_shares_replay_capacity_and_releases_it(self):
        from unittest.mock import patch
        from claim_agent.server import create_app
        self.settings.max_sweeps = 1
        self.settings.api_key = "offline-test"
        self.settings.pair_token = "test"
        self.gate = asyncio.Event()
        with patch('claim_agent.server.GeminiProvider', side_effect=lambda *args: FakeProvider()), \
             patch('claim_agent.server.VisionProvider', side_effect=lambda p: FakeVision(p,self.seen,self.gate)), \
             patch('claim_agent.server.PriceProvider', return_value=None):
            app = create_app(self.settings)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',headers={'authorization':'Bearer test'}) as client:
                task = asyncio.create_task(client.post(self.url))
                try:
                    for _ in range(100):
                        if self.seen: break
                        await asyncio.sleep(.005)
                    self.assertTrue(self.seen, 'Replay must be held inside provider work')
                    id = (await client.post('/api/onboarding',json={'device':'phone','capture_supported':True})).json()['id']
                    session = app.state.onboarding[id]
                    await session.invoke_voice_tool('configure_session',{'language':'Hindi','country':'India','currency':'INR','capture_mode':'lidar','confirmed':True})
                    await session.invoke_voice_tool('request_capture',{'ready':True})
                    body = {'device':'phone','onboarding_id':id}
                    blocked = await client.post('/api/sweeps',json=body)
                    self.assertEqual(blocked.status_code,409,blocked.text)
                    self.assertFalse(session.consumed)
                    duplicate = await client.post(self.url)
                    self.assertEqual(duplicate.status_code,409)
                    self.assertEqual(duplicate.json()['detail']['code'],'already_running')
                    self.gate.set()
                    self.assertEqual((await task).status_code,200)
                    created = await client.post('/api/sweeps',json=body)
                    self.assertEqual(created.status_code,200,created.text)
                    await client.post('/api/sweeps/'+created.json()['sweep']['id']+'/finish',json={'duration_s':0})
                    self.assertEqual(len(self.seen),1)
                finally:
                    self.gate.set()
                    await task
                    for runner in app.state.runners.values():
                        await runner.close()
                        await runner.provider.close()

    async def test_auth_and_unfinished_model_mismatch(self):
        self.assertEqual((await self.client.post(self.url,headers={'authorization':''})).status_code,401)
        self.assertEqual((await self.client.get(self.url)).status_code,404)
        self.source.completion='capturing'
        self.assertEqual((await self.client.post(self.url)).status_code,409)
        self.source.completion='partial'
        self.source.metrics['capture_models']['vision_model']='other'
        self.assertEqual((await self.client.post(self.url)).status_code,409)
        self.assertFalse(self.created)

    async def test_failure_is_honest_and_retry_does_not_silently_spend_again(self):
        self.should_fail=True
        result=(await self.client.post(self.url)).json()
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['camera']['frames_processed'],0)
        self.assertEqual(result['camera']['frames_failed'],1)
        self.assertEqual(result['analysis_status'],'failed')
        self.assertTrue(result['workflow_complete'])
        self.assertIsNone(result['camera']['book_count'])
        self.assertIsNone(result['camera']['object_count'])
        self.assertTrue(result['camera']['review_count'])
        self.assertNotIn('private provider error',json.dumps(result))
        self.assertTrue(self.created[0].provider.closed)
        self.assertEqual((await self.client.post(self.url)).json(),result)

    async def test_report_setup_failure_does_not_leak_shared_capacity(self):
        from unittest.mock import patch
        with patch('claim_agent.comparison._summary',side_effect=ValueError('Invalid source packet')):
            with self.assertRaisesRegex(ValueError,'Invalid source packet'):
                await self.client.post(self.url)
        self.assertFalse(self.active)
        self.assertFalse(self.created)
        self.assertEqual((await self.client.post(self.url)).status_code,200)

    async def test_timeout_cleans_provider_and_is_persisted(self):
        self.gate=asyncio.Event()
        self.settings.finish_timeout=.02
        result=(await self.client.post(self.url)).json()
        self.assertEqual(result['status'],'failed')
        self.assertFalse(self.active)
        self.assertTrue(self.created[0].provider.closed)
        self.assertFalse(self.created[0].workers)
        self.assertEqual((await self.client.post(self.url)).json(),result)
        self.assertEqual(len(self.created),1)

    async def test_frozen_shared_truth_uses_independent_physical_id_maps(self):
        from evaluation.compare_packets import evaluate_pair
        await self.client.post(self.url)
        directory=self.store.directory(self.source.sweep.id)/'camera-comparison'
        with self.assertRaisesRegex(ValueError,'Freeze'):
            evaluate_pair(directory,{})
        truth={'frozen_at':'2026-10-17T00:00:00Z','book_count':1,
               'books':[{'id':'physical-book','legible':True,'title':'A real title',
                         'predicted_ids':{'lidar':'owner-corrected','camera':None}}]}
        result=evaluate_pair(directory,truth)
        self.assertEqual(result['evaluations']['lidar']['metrics']['title_identification']['legible_truth'],1)
        self.assertEqual(result['evaluations']['camera']['metrics']['title_identification']['legible_truth'],1)
        self.assertEqual(result['evaluations']['camera']['metrics']['title_identification']['recall'],0)
        truth['books'][0]['predicted_ids'].pop('camera')
        with self.assertRaisesRegex(ValueError,'explicit'):
            evaluate_pair(directory,truth)

    async def test_paired_checks_score_modes_independently_and_emit_null_safe_deltas(self):
        from evaluation.compare_packets import evaluate_pair
        directory = Path(self.tmp.name)/'paired-evaluation'
        directory.mkdir()
        (directory/'comparison.json').write_text(json.dumps({'status':'complete','experiment':'same-frame','source_sweep_id':'source','synthetic':True,'limitations':[]}))
        for mode, title in [('lidar','Actual book'),('camera','Wrong title')]:
            packet = {'sweep':{'captured_at':'2026-10-17T10:00:00Z'},'books':[{'id':mode,'title':title,'id_confidence':.99},{'id':'checked-'+mode,'title':'Independently checked prediction','id_confidence':.99}], 'room':{}}
            (directory/f'{mode}_packet.json').write_text(json.dumps(packet))
        truth = {'frozen_at':'2026-10-17T14:00:00+05:30','book_count':1,
                 'books':[{'id':'physical','legible':True,'title':'Actual book','predicted_ids':{'lidar':'lidar','camera':'camera'}}],
                 'identity_checks':[{'predicted_ids':{'lidar':'checked-lidar','camera':'checked-camera'},'correct_by_mode':{'lidar':True,'camera':False}}]}
        result = evaluate_pair(directory,truth)
        self.assertEqual(result['evaluations']['lidar']['metrics']['title_identification']['confidently_wrong'],0)
        self.assertEqual(result['evaluations']['camera']['metrics']['title_identification']['confidently_wrong'],2)
        deltas = result['deltas_camera_minus_lidar']
        self.assertEqual(deltas['title_recall']['value'],-100)
        self.assertEqual(deltas['title_recall']['unit'],'percentage_points')
        self.assertEqual(deltas['confidently_wrong']['value'],2)
        self.assertIsNone(deltas['floor_area_relative_error']['value'])
        self.assertIsNone(deltas['spine_coverage']['value'])
        truth['identity_checks'][0]['correct_by_mode']={'lidar':False,'camera':None}
        unchecked=evaluate_pair(directory,truth)
        self.assertEqual(unchecked['evaluations']['camera']['metrics']['title_identification']['unverified_high_confidence'],1)
        self.assertIsNone(unchecked['deltas_camera_minus_lidar']['confidently_wrong']['value'])
        self.assertIsNone(unchecked['deltas_camera_minus_lidar']['confident_wrong_fraction']['value'])
        truth['identity_checks'][0].pop('correct_by_mode')
        truth['identity_checks'][0]['correct'] = True
        with self.assertRaisesRegex(ValueError,'correct_by_mode'):
            evaluate_pair(directory,truth)
        truth['identity_checks'] = []
        for timestamp in ['not-a-timestamp','2026-10-17T10:00:01Z','2026-10-17T09:00:00']:
            truth['frozen_at'] = timestamp
            with self.assertRaises(ValueError):
                evaluate_pair(directory,truth)

    async def test_saved_and_cached_reports_notify_voice_without_more_provider_cost(self):
        from types import SimpleNamespace
        contexts=[]
        self.source_runner.live=SimpleNamespace(offer_context=contexts.append)
        events=asyncio.Queue(maxsize=5)
        self.source_runner.listeners.add(events)
        first=await self.client.post(self.url)
        second=await self.client.post(self.url)
        self.assertEqual(first.status_code,200)
        self.assertEqual(second.status_code,200)
        self.assertEqual(len(self.created),1)
        self.assertEqual(len(contexts),2)
        self.assertEqual(events.get_nowait()['type'],'comparison_ready')
        self.assertNotIn('response',contexts[0]['summary']['camera'])
        self.assertTrue(contexts[0]['comparison_ready'])

    async def test_corrupt_saved_jpeg_fails_cleanly_and_is_not_retried(self):
        self.store.evidence_path(self.source.sweep.id,'frames/f0.jpg').write_bytes(b'changed')
        result=(await self.client.post(self.url)).json()
        self.assertEqual(result['status'],'failed')
        self.assertFalse(self.active)
        self.assertTrue(self.created[0].provider.closed)
        self.assertFalse(self.seen)
        self.assertEqual((await self.client.post(self.url)).json(),result)
        self.assertEqual(len(self.created),1)

    async def test_stale_running_report_becomes_interrupted_without_replaying(self):
        directory=self.store.directory(self.source.sweep.id)/'camera-comparison'
        directory.mkdir()
        (directory/'comparison.json').write_text(json.dumps({'status':'running','source_sweep_id':self.source.sweep.id}))
        result=(await self.client.post(self.url)).json()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['analysis_status'],'interrupted')
        self.assertFalse(self.created)
