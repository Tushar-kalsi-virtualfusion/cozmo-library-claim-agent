import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from claim_agent.config import Settings
from claim_agent.models import FrameMetadata
from claim_agent.pipeline import SweepRunner
from claim_agent.providers import ProviderUnavailable
from claim_agent.server import create_app
from claim_agent.storage import EvidenceStore
from claim_agent.vision import Scan, Detection
from claim_agent.local_evidence import merge_supported_scan
from tests.test_hybrid import jpeg, meta

QUAD = [[.1,.1],[.7,.1],[.7,.8],[.1,.8]]


def local_frame(id='f1', title='Dune', text='Dune Frank Herbert', needs_cloud=False):
    values = meta(id).model_dump()
    values['ocr'] = [{'text':text,'center':[.4,.4]}]
    values['local_recognition'] = {'model':'YOLO','detections':[{'label':'book','confidence':.9,'polygon':QUAD}],
        'sharpness':100,'dark_fraction':0,'bright_fraction':0,'fingerprint':'0000000000000000','inference_ms':12}
    values['local_analysis'] = {'source':'apple_foundation_models','status':'complete','books':[
        {'polygon':QUAD,'visible_text':text,'title':title,'author':'Frank Herbert','isbn':None,'confidence':.9}],
        'objects':[],'needs_cloud':needs_cloud,'reasons':[]}
    return values


class LocalValidationTests(unittest.TestCase):
    def test_cloud_detection_limit_never_truncates_supported_local_identity(self):
        local=Scan(detections=[Detection(kind='book',polygon=QUAD,title='Dune',author='Frank Herbert')])
        cloud=Scan(detections=[Detection(kind='item',category='object',polygon=[[.8,.1],[.9,.1],[.9,.2],[.8,.2]]) for _ in range(250)])
        merged=merge_supported_scan(local,cloud)
        self.assertEqual(len(merged.detections),250)
        self.assertIn('Dune',[d.title for d in merged.detections])

    def test_analysis_rejects_unknown_source_status_and_invalid_geometry(self):
        good=local_frame()
        self.assertEqual(FrameMetadata.model_validate(good).local_analysis.books[0].title,'Dune')
        for field,value in [('source','cloud'),('status','invented'),('needs_cloud','false')]:
            wrong=local_frame(); wrong['local_analysis'][field]=value
            with self.assertRaises(ValidationError): FrameMetadata.model_validate(wrong)
        for polygon in [[[0,0],[0,0],[0,0],[0,0]],[[0,0],[1,1],[1,0],[0,1]]]:
            wrong=local_frame(); wrong['local_analysis']['books'][0]['polygon']=polygon
            with self.assertRaises(ValidationError): FrameMetadata.model_validate(wrong)


class LocalRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store=EvidenceStore(Path(self.tmp.name))
        self.state=self.store.create('iPhone','India','INR')
        self.calls=[]; self.price_calls=[]
        async def inspect(image,metadata):
            self.calls.append(metadata.id)
            return Scan()
        async def quote(*args):
            self.price_calls.append(args)
            raise AssertionError('Local-first must not initiate paid pricing')
        self.vision=SimpleNamespace(provider=SimpleNamespace(usage=[]),inspect=inspect)
        self.runner=SweepRunner(self.state,self.store,Settings(),self.vision,SimpleNamespace(quote=quote))
        self.runner.start(); self.addAsyncCleanup(self.runner.close)
        self.catalogue=self.enterContext(patch('claim_agent.local_evidence.catalogue_candidates', return_value=[
            {'source':'Open Library (candidate only)','url':'https://openlibrary.org/works/OL893415W',
             'title':'Dune','authors':['Frank Herbert'],'candidate_isbns':['9780441172719'],'first_publish_year':1965}]))

    async def process(self, values, seed=1):
        await self.runner.upload(FrameMetadata.model_validate(values),jpeg(seed),b'',b'')
        await asyncio.wait_for(self.runner.queue.join(),2)
        await asyncio.wait_for(self.runner.price_queue.join(),2)

    async def test_supported_local_book_uses_no_paid_vision_or_pricing(self):
        await self.process(local_frame())
        self.assertEqual(self.calls,[])
        self.assertEqual(self.price_calls,[])
        book=self.state.books[0]
        self.assertEqual((book.title,book.author,book.status),('Dune','Frank Herbert','identified'))
        self.assertEqual((book.isbn,book.edition),('',''))
        self.assertIsNone(book.spine_height_cm)
        self.assertTrue(book.catalogue_candidates)
        self.assertEqual(self.runner.snapshot().metrics['cost_usd'],0)
        self.assertEqual(self.state.metrics['routing'][-1]['route'],'local')

    async def test_unique_ocr_catalogue_match_has_consistent_downstream_identity(self):
        values=local_frame()
        values['local_recognition']['detections'][0]['confidence']=.7
        values['local_analysis']['books'][0]['confidence']=.7
        await self.process(values)
        self.assertEqual((self.state.books[0].title,self.state.books[0].status),('Dune','identified'))
        self.assertEqual(self.calls,[])
        self.assertEqual(self.state.metrics['routing'][-1]['route'],'local')

    async def test_unsupported_clues_and_ocr_outside_book_cannot_create_identity(self):
        values=local_frame(title='The Hobbit',text='unreadable')
        values['ocr']=[{'text':'The Hobbit Tolkien','center':[.95,.95]}]
        values['local_recognition']['detections']=[]
        await self.process(values)
        self.assertFalse(self.state.books)
        self.assertFalse(self.calls)
        self.assertTrue(self.state.metrics['routing'][-1]['reasons'])

    async def test_cloud_title_without_independent_ocr_cannot_become_identified(self):
        values=local_frame(); values['local_analysis']['books']=[]; values['ocr']=[]
        values['local_recognition']['detections']=[{'label':'decorative object','confidence':.9,'polygon':QUAD}]
        values['local_analysis']['objects']=[{'category':'decorative object','confidence':.9,'polygon':QUAD}]
        async def inspect(image,metadata):
            self.calls.append(metadata.id)
            return Scan(detections=[Detection(kind='book',polygon=QUAD,title='Dune',author='Frank Herbert',
                                             visible_text='Dune Frank Herbert',confidence=.99)])
        self.vision.inspect=inspect
        await self.process(values)
        self.assertEqual(self.calls,['f1'])
        self.assertEqual(self.state.books[0].status,'unidentified')
        self.assertEqual(self.state.books[0].title,'')
        self.assertEqual(self.price_calls,[])

    async def test_cloud_empty_result_cannot_discard_locally_supported_book(self):
        values=local_frame()
        object_quad=[[.75,.1],[.95,.1],[.95,.8],[.75,.8]]
        values['local_recognition']['detections'].append({'label':'chair','confidence':.9,'polygon':object_quad})
        values['local_analysis']['objects']=[{'category':'chair','confidence':.9,'polygon':object_quad}]
        await self.process(values)
        self.assertEqual(self.calls,['f1'])
        self.assertEqual(self.state.books[0].title,'Dune')

    async def test_readable_book_does_not_hide_unreadable_book_coverage_gap(self):
        values=local_frame()
        unreadable_quad=[[.75,.1],[.95,.1],[.95,.8],[.75,.8]]
        values['local_recognition']['detections'].append({'label':'novel','confidence':.9,'polygon':unreadable_quad})
        values['local_analysis']['books'].append({'polygon':unreadable_quad,'visible_text':'','title':None,
                                                 'author':None,'isbn':None,'confidence':.9})
        await self.process(values)
        self.assertEqual(self.state.books[0].title,'Dune')
        self.assertEqual(self.calls,[])
        self.assertEqual(self.state.frame_status['f1'],'local_pending')
        self.assertTrue(any('no readable' in reason for reason in self.state.review_reasons))

    async def test_concurrent_identical_catalogue_lookup_waits_instead_of_spending(self):
        candidates=self.catalogue.return_value
        async def slow_catalogue(*args):
            await asyncio.sleep(.05)
            return candidates
        self.catalogue.side_effect=slow_catalogue
        for id,seed in [('f1',1),('f2',2)]:
            values=local_frame(id); values['local_analysis']['books'][0]['confidence']=.8
            await self.runner.upload(FrameMetadata.model_validate(values),jpeg(seed),b'',b'')
        await asyncio.wait_for(self.runner.queue.join(),2)
        self.assertEqual(self.calls,[])
        self.assertTrue(self.state.books)

    async def test_novel_book_scene_with_same_partial_text_can_receive_bounded_review(self):
        self.runner.settings.cloud_cooldown_s=0
        await self.process(local_frame(title=None,text='Unknown A',needs_cloud=True))
        values=local_frame('f2',title=None,text='Unknown A',needs_cloud=True)
        values['camera_transform'][12]=1
        await self.process(values,seed=2)
        self.assertEqual(self.calls,['f1','f2'])

    async def test_explicit_local_unavailability_can_escalate_a_clear_novel_scene(self):
        values=local_frame(); values['local_recognition']['detections']=[]; values['ocr']=[]
        values['local_analysis'].update(status='unavailable',books=[],objects=[],needs_cloud=True,reasons=['Model unavailable'])
        await self.process(values)
        self.assertEqual(self.calls,['f1'])
        self.assertIn('incomplete',str(self.state.metrics['routing'][-1]['reasons']).lower())

    async def test_claude_server_mode_escalates_with_existing_cloud_budget_and_cooldown(self):
        for id,seed in [('f1',1),('f2',2)]:
            values=local_frame(id); values['local_recognition']['detections']=[]; values['ocr']=[]
            values['camera_transform'][12]=seed
            values['local_analysis'].update(source='cloud_requested',status='unavailable',books=[],objects=[],
                needs_cloud=True,reasons=['Claude server mode; on-device language model is disabled'])
            await self.process(values,seed=seed)
        self.assertEqual(self.calls,['f1'])
        self.assertIn('cooldown',str(self.state.metrics['routing'][-1]['reasons']).lower())
        self.runner.settings.cloud_max_calls=1
        self.runner.settings.cloud_cooldown_s=0
        values['id']='f3'; values['camera_transform'][12]=3
        await self.process(values,seed=3)
        self.assertEqual(self.calls,['f1'])
        self.assertIn('budget',str(self.state.metrics['routing'][-1]['reasons']).lower())

    async def test_incomplete_local_coverage_preserves_included_supported_books(self):
        values=local_frame(); values['local_analysis'].update(status='partial',reasons=['local_coverage_incomplete'])
        await self.process(values)
        self.assertEqual(self.calls,['f1'])
        self.assertEqual(self.state.books[0].title,'Dune')

    async def test_blurry_unavailable_scene_is_deferred_without_spending(self):
        import io
        from PIL import Image
        values=local_frame(); values['local_analysis'].update(status='unavailable',books=[],objects=[])
        output=io.BytesIO(); Image.new('RGB',(100,100),(120,120,120)).save(output,format='JPEG')
        await self.runner.upload(FrameMetadata.model_validate(values),output.getvalue(),b'',b'')
        await asyncio.wait_for(self.runner.queue.join(),2)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.state.frame_status['f1'],'local_pending')
        self.assertIn('quality',str(self.state.metrics['routing'][-1]['reasons']))

    async def test_local_objects_remain_provisional_and_do_not_invent_metrics(self):
        values=local_frame(); values['local_analysis']['books']=[]; values['ocr']=[]
        values['local_recognition']['detections']=[{'label':'chair','confidence':.9,'polygon':QUAD}]
        values['local_analysis']['objects']=[{'category':'reading a book','confidence':.99,'polygon':QUAD}]
        self.runner.settings.cloud_max_calls=0
        await self.process(values)
        self.assertFalse(self.state.items)
        self.assertFalse(self.calls)
        self.assertTrue(self.state.metrics['routing'][-1]['provisional_objects'])
        self.assertIsNone(self.runner.snapshot().room.floor_area_m2)

    async def test_semantic_book_clue_is_supported_by_polygon_without_a_class_whitelist(self):
        values=local_frame()
        values['local_recognition']['detections'][0]['label']='novel'
        await self.process(values)
        self.assertEqual(self.state.books[0].title,'Dune')
        self.assertEqual(self.calls,[])

    async def test_unlisted_physical_category_can_escalate_but_raw_actions_cannot(self):
        values=local_frame(); values['local_analysis']['books']=[]; values['ocr']=[]
        values['local_recognition']['detections']=[{'label':'air conditioner','confidence':.9,'polygon':QUAD}]
        values['local_analysis']['objects']=[{'category':'air conditioner','confidence':.9,'polygon':QUAD}]
        await self.process(values)
        self.assertEqual(self.calls,['f1'])
        self.assertFalse(self.state.items)
        values['id']='f2'
        values['local_recognition']['detections'][0]['label']='reading a book'
        values['local_analysis']['objects']=[]
        self.runner.settings.cloud_cooldown_s=0
        await self.process(values,seed=2)
        self.assertEqual(self.calls,['f1'])

    async def test_distinct_views_of_same_object_category_are_not_permanently_suppressed(self):
        values=local_frame(); values['local_analysis']['books']=[]; values['ocr']=[]
        values['local_recognition']['detections']=[{'label':'wireless router','confidence':.9,'polygon':QUAD}]
        values['local_analysis']['objects']=[{'category':'wireless router','confidence':.9,'polygon':QUAD}]
        self.runner.settings.cloud_cooldown_s=0
        await self.process(values)
        values['id']='f2'; values['camera_transform'][12]=1
        await self.process(values,seed=2)
        self.assertEqual(self.calls,['f1','f2'])

    async def test_missing_analysis_is_not_reported_as_success(self):
        await self.process(meta().model_dump())
        self.assertFalse(self.calls)
        self.assertEqual(self.state.frame_status['f1'],'local_pending')
        self.assertTrue(any('local' in r.lower() for r in self.state.review_reasons))

    async def test_clear_unresolved_evidence_is_bounded_by_cooldown_and_novelty(self):
        self.runner.settings.cloud_max_calls=2
        self.runner.settings.cloud_cooldown_s=60
        await self.process(local_frame(title=None,text='Unknown A',needs_cloud=True))
        await self.process(local_frame('f2',title=None,text='Unknown B',needs_cloud=True),seed=2)
        self.assertEqual(self.calls,['f1'])
        self.assertEqual(self.state.metrics['routing'][-1]['route'],'deferred')
        self.assertIn('cooldown',str(self.state.metrics['routing'][-1]['reasons']))
        self.runner.settings.cloud_cooldown_s=0
        await self.process(local_frame('f3',title=None,text='Unknown A',needs_cloud=True),seed=1)
        self.assertEqual(self.calls,['f1'])
        await self.process(local_frame('f4',title=None,text='Unknown C',needs_cloud=True),seed=4)
        await self.process(local_frame('f5',title=None,text='Unknown D',needs_cloud=True),seed=5)
        self.assertEqual(self.calls,['f1','f4'])
        self.assertIn('budget',str(self.state.metrics['routing'][-1]['reasons']))

    async def test_credit_failure_is_persisted_and_not_retried_after_reload(self):
        async def fail(image,metadata):
            self.calls.append(metadata.id)
            raise ProviderUnavailable('OpenAI request failed (credit_balance_exhausted)')
        self.vision.inspect=fail
        self.runner.settings.cloud_cooldown_s=0
        await self.process(local_frame(title=None,text='Unknown A',needs_cloud=True))
        await self.runner.close()
        self.runner=SweepRunner(self.store.load(self.state.sweep.id),self.store,Settings(),self.vision,None)
        self.runner.start(); self.addAsyncCleanup(self.runner.close)
        await self.process(local_frame('f2',title=None,text='Unknown B',needs_cloud=True),seed=2)
        self.assertEqual(self.calls,['f1'])
        self.assertTrue(self.runner.state.metrics['cloud_routing']['disabled_reason'])


class LocalVoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.client=self.enterContext(TestClient(create_app(Settings(data_dir=Path(self.tmp.name),pair_token='token'))))
        self.headers={'Authorization':'Bearer token'}
        self.id=self.client.post('/api/onboarding',headers=self.headers,json={'device':'iPhone','capture_supported':True}).json()['id']
        self.path=f'/api/onboarding/{self.id}/local-voice'

    def call(self,name,arguments):
        return self.client.post(self.path,headers=self.headers,json={'name':name,'arguments':arguments})

    def test_local_voice_tools_require_confirmation_and_separate_readiness(self):
        self.assertEqual(self.client.get(self.path).status_code,401)
        self.assertEqual(self.client.post(self.path,json={'name':'request_capture','arguments':{'ready':True}}).status_code,401)
        context=self.client.get(self.path,headers=self.headers).json()
        self.assertEqual({t['name'] for t in context['tools']},{'configure_session','request_capture'})
        self.assertTrue(context['instructions']); self.assertTrue(context['greeting'])
        self.assertEqual(self.call('request_capture',{'ready':True}).status_code,422)
        args={'language':'Hindi','country':'India','currency':'INR','capture_mode':'lidar','confirmed':False}
        self.assertEqual(self.call('configure_session',args).status_code,422)
        args['confirmed']=True
        self.assertTrue(self.call('configure_session',args).json()['ok'])
        self.assertEqual(self.call('arbitrary_tool',{}).status_code,422)
        body={'device':'iPhone','onboarding_id':self.id}
        self.assertEqual(self.client.post('/api/sweeps',headers=self.headers,json=body).status_code,409)
        with self.client.websocket_connect(f'/api/onboarding/{self.id}/live',headers=self.headers) as ws:
            self.assertEqual(ws.receive_json()['type'],'setup_confirmed')
            self.assertTrue(self.call('request_capture',{'ready':True}).json()['ok'])
            self.assertEqual(ws.receive_json()['type'],'capture_requested')
        self.assertIsNone(self.client.app.state.onboarding[self.id].live)
        result=self.client.post('/api/sweeps',headers=self.headers,json=body)
        self.assertEqual(result.status_code,200,result.text)
        path=f"/api/sweeps/{result.json()['sweep']['id']}/local-voice"
        self.assertEqual(self.client.get(path,headers=self.headers).status_code,200)
        wrong=self.client.post(path,headers=self.headers,json={'name':'correct_inventory','arguments':{'action':'exclude','ref_id':'missing','value':''}})
        self.assertEqual(wrong.status_code,422)

    def test_legacy_start_voice_cannot_silently_incur_cloud_cost(self):
        with self.client.websocket_connect(f'/api/onboarding/{self.id}/live',headers=self.headers) as ws:
            ws.send_json({'type':'start_voice'})
            event=ws.receive_json()
            self.assertEqual(event['type'],'voice_error')
        self.assertIsNone(self.client.app.state.onboarding[self.id].live)
