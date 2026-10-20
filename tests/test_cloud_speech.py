import asyncio
import base64
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from claim_agent.config import Settings
from claim_agent.cloud_speech import CloudSpeech, SpeechRequest, TranscribeRequest, validate_audio
from claim_agent.costing import usage_cost
from claim_agent.server import create_app


def wav(silent=False):
    output=io.BytesIO()
    with wave.open(output,'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes((b'\x00\x00' if silent else b'\x00\x10')*16000)
    return output.getvalue()


class SpeechTests(unittest.IsolatedAsyncioTestCase):
    def service(self, handler, **values):
        self.requests=[]
        async def send(req):
            self.requests.append(req)
            value=handler(req)
            return await value if hasattr(value,'__await__') else value
        target=SimpleNamespace(state=SimpleNamespace(metrics={}))
        settings=Settings(speech_provider='elevenlabs',elevenlabs_api_key='private-secret', **values)
        service=CloudSpeech(target,settings,client=httpx.AsyncClient(transport=httpx.MockTransport(send)))
        self.addAsyncCleanup(service.close)
        return service

    async def test_complete_pcm_wav_private_key_and_idempotency(self):
        s=self.service(lambda req:httpx.Response(200,content=b'\x00\x10'*24000,headers={'request-id':'voice-response','character-cost':'5'}),speech_output_model='eleven_v4')
        req=SpeechRequest(request_id=uuid4(),text='Hello',language='en-IN')
        a,b=await asyncio.gather(s.speak(req),s.speak(req))
        self.assertEqual(a,b);self.assertEqual(len(self.requests),1)
        with wave.open(io.BytesIO(base64.b64decode(a['audio_base64'])),'rb') as w:
            self.assertEqual((w.getnchannels(),w.getsampwidth(),w.getframerate(),w.getnframes()),(1,2,24000,24000))
        body=json.loads(self.requests[0].content)
        self.assertEqual(body['text'],'Hello');self.assertEqual(body['language_code'],'en')
        self.assertEqual(body['model_id'],'eleven_v4');self.assertEqual(self.requests[0].headers['xi-api-key'],'private-secret')
        ledger=json.dumps(s.target.state.metrics)
        self.assertNotIn('private-secret',ledger);self.assertNotIn('audio_base64',ledger)
        self.assertAlmostEqual(usage_cost(s.target.state.metrics['speech_usage'])['cost_usd'],.0004)
        with self.assertRaises(HTTPException) as e: await s.speak(req.model_copy(update={'text':'different'}))
        self.assertEqual(e.exception.status_code,409)

    async def test_scribe_uploads_audio_detects_language_and_validates_before_billing(self):
        s=self.service(lambda req:httpx.Response(200,json={'text':'English please','language_code':'eng'}))
        result=await s.transcribe(TranscribeRequest(request_id=uuid4(),audio_base64=base64.b64encode(wav()).decode()))
        self.assertEqual(result,{'text':'English please','language':'eng'})
        self.assertIn(b'scribe_v2',self.requests[0].content)
        for data in [b'bad wav',wav(True)]:
            with self.assertRaises(ValueError): await s.transcribe(TranscribeRequest(request_id=uuid4(),audio_base64=base64.b64encode(data).decode()))
        self.assertEqual(len(self.requests),1)

    async def test_error_timeout_and_cancellation_never_retry_or_leak(self):
        s=self.service(lambda req:httpx.Response(401,json={'detail':{'message':'private-secret'}}))
        req=SpeechRequest(request_id=uuid4(),text='Hello')
        for _ in range(2):
            with self.assertRaises(HTTPException) as e: await s.speak(req)
            self.assertNotIn('private-secret',str(e.exception.detail))
        self.assertEqual(len(self.requests),1)
        async def slow(req): await asyncio.sleep(10)
        s=self.service(slow,speech_timeout_s=.01)
        with self.assertRaises(HTTPException): await s.speak(req)
        self.assertEqual(len(self.requests),1)
        self.assertEqual(usage_cost(s.target.state.metrics['speech_usage'])['cost_status'],'unavailable')
        s=self.service(slow)
        task=asyncio.create_task(s.speak(req)); await asyncio.sleep(.01);task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        with self.assertRaises(HTTPException): await s.speak(req)
        self.assertEqual(len(self.requests),1)

    async def test_budget_restart_cache_eviction_and_truncation(self):
        s=self.service(lambda req:httpx.Response(200,content=b'\x00\x10'*24000),speech_max_calls=1)
        req=SpeechRequest(request_id=uuid4(),text='Hello')
        await s.speak(req); s.cache.clear()
        with self.assertRaises(HTTPException) as e: await s.speak(req)
        self.assertEqual(e.exception.status_code,503)
        with self.assertRaises(HTTPException) as e: await s.speak(SpeechRequest(request_id=uuid4(),text='Hi'))
        self.assertEqual(e.exception.status_code,429)
        self.assertEqual(len(self.requests),1)
        s=self.service(lambda req:httpx.Response(200,content=b'odd'))
        with self.assertRaises(HTTPException):await s.speak(req)
        self.assertEqual(s.target.state.metrics['speech_requests'][str(req.request_id)]['status'],'failed')

    async def test_busy_distinct_turn_is_not_queued_or_billed(self):
        admitted=asyncio.Event();release=asyncio.Event()
        async def slow(req):
            admitted.set();await release.wait()
            return httpx.Response(200,content=b'\x00\x10'*24000)
        service=self.service(slow)
        task=asyncio.create_task(service.speak(SpeechRequest(request_id=uuid4(),text='First')))
        await admitted.wait()
        try:
            with self.assertRaises(HTTPException) as error:
                await service.speak(SpeechRequest(request_id=uuid4(),text='Second'))
            self.assertEqual(error.exception.status_code,409)
            self.assertEqual(len(self.requests),1)
        finally:
            release.set();await task

    async def prepared_guide(self):
        from tests.test_sweep_guide import SweepGuideTests
        fixture=SweepGuideTests(methodName='runTest')
        await fixture.asyncSetUp()
        self.addAsyncCleanup(fixture.runner.close)
        self.addCleanup(fixture.tmp.cleanup)
        fixture.add_frame(status='processed');fixture.add_book()
        fixture.guide.record('processed','f1',new_ids=['b1'])
        result=await fixture.request()
        return fixture,result['event_id']

    async def test_stale_guidance_before_provider_has_no_billable_call(self):
        fixture,event_id=await self.prepared_guide()
        service=self.service(lambda req:httpx.Response(200,content=b'\x00\x10'*24000))
        service.target=fixture.runner
        fixture.add_frame('f2');fixture.guide.record('uploaded','f2')
        body=SpeechRequest(request_id=uuid4(),text='This view is saved; move on.',guidance_event_id=event_id)
        with self.assertRaises(HTTPException) as error:await service.speak(body)
        self.assertEqual(error.exception.status_code,409)
        self.assertEqual(error.exception.detail['code'],'guidance_stale')
        self.assertEqual(self.requests,[])
        self.assertFalse(service.target.state.metrics.get('speech_usage'))

    async def test_new_upload_during_guidance_tts_discards_audio_and_keeps_usage(self):
        fixture,event_id=await self.prepared_guide()
        admitted=asyncio.Event();release=asyncio.Event()
        async def held(req):
            admitted.set();await release.wait()
            return httpx.Response(200,content=b'\x00\x10'*24000,headers={'request-id':'accepted-guidance'})
        service=self.service(held);service.target=fixture.runner
        body=SpeechRequest(request_id=uuid4(),text='This view is saved; move on.',guidance_event_id=event_id)
        task=asyncio.create_task(service.speak(body))
        await asyncio.wait_for(admitted.wait(),1)
        fixture.add_frame('f2');fixture.guide.record('uploaded','f2');release.set()
        with self.assertRaises(HTTPException) as error:await task
        self.assertEqual(error.exception.status_code,409)
        self.assertEqual(error.exception.detail['code'],'guidance_stale')
        self.assertNotIn(str(body.request_id),service.cache)
        row=service.target.state.metrics['speech_usage'][0]
        self.assertEqual(row['usage']['characters'],len(body.text))
        self.assertEqual(row['error_type'],'guidance_stale')
        self.assertGreater(usage_cost([row])['cost_usd'],0)
        with self.assertRaises(HTTPException):await service.speak(body)
        self.assertEqual(len(self.requests),1,'Discarding accepted audio must never repay for the same request')

    async def test_cached_guidance_is_revalidated_without_new_provider_call(self):
        fixture,event_id=await self.prepared_guide()
        service=self.service(lambda req:httpx.Response(200,content=b'\x00\x10'*24000))
        service.target=fixture.runner
        body=SpeechRequest(request_id=uuid4(),text='This view is saved; move on.',guidance_event_id=event_id)
        first=await service.speak(body)
        self.assertEqual(await service.speak(body),first)
        fixture.add_frame('f2');fixture.guide.record('uploaded','f2')
        with self.assertRaises(HTTPException) as error:await service.speak(body)
        self.assertEqual(error.exception.status_code,409)
        self.assertEqual(error.exception.detail['code'],'guidance_stale')
        self.assertEqual(len(self.requests),1)
        self.assertEqual(len(service.target.state.metrics['speech_usage']),1)

    async def test_response_limits_stream_and_malformed_json_is_closed(self):
        consumed=[]
        class Large(httpx.AsyncByteStream):
            async def __aiter__(self):
                for i in range(20):
                    consumed.append(i);yield b'x'*1_000_000
        service=self.service(lambda req:httpx.Response(200,stream=Large()))
        with self.assertRaises(HTTPException):await service.speak(SpeechRequest(request_id=uuid4(),text='Hello'))
        self.assertLess(len(consumed),8)
        service=self.service(lambda req:httpx.Response(200,json=[]))
        body=TranscribeRequest(request_id=uuid4(),audio_base64=base64.b64encode(wav()).decode())
        with self.assertRaises(HTTPException):await service.transcribe(body)
        self.assertEqual(service.target.state.metrics['speech_requests'][str(body.request_id)]['status'],'failed')


class SpeechAPITests(unittest.TestCase):
    def test_authenticated_config_and_handoff_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            app=create_app(Settings(data_dir=Path(directory),pair_token='pair',speech_provider='elevenlabs',elevenlabs_api_key='private'))
            with TestClient(app) as c:
                self.assertEqual(c.post('/api/onboarding/missing/local-voice/speech',json={}).status_code,401)
                c.headers['Authorization']='Bearer pair'
                id=c.post('/api/onboarding',json={'device':'iphone','capture_supported':True}).json()['id']
                config=c.get(f'/api/onboarding/{id}/local-voice').json()['speech']
                self.assertEqual(config['provider'],'elevenlabs');self.assertTrue(config['ready'])
                self.assertNotIn('private',json.dumps(config))
                session=app.state.onboarding[id]
                session.state.metrics['speech_usage']=[{'provider':'elevenlabs','model':'eleven_v4','usage':{'characters':5}}]
                c.post(f'/api/onboarding/{id}/local-voice',json={'name':'configure_session','arguments':{'language':'English','country':'India','currency':'INR','capture_mode':'lidar','confirmed':True}})
                c.post(f'/api/onboarding/{id}/local-voice',json={'name':'request_capture','arguments':{'ready':True}})
                result=c.post('/api/sweeps',json={'device':'iphone','onboarding_id':id}).json()
                self.assertEqual(len(result['metrics']['onboarding_speech_usage']),1)

    def test_http_speech_roundtrip_and_no_event_guidance(self):
        with tempfile.TemporaryDirectory() as directory:
            app=create_app(Settings(data_dir=Path(directory),pair_token='pair',speech_provider='elevenlabs',elevenlabs_api_key='private'))
            with TestClient(app) as c:
                c.headers['Authorization']='Bearer pair'
                id=c.post('/api/onboarding',json={'device':'iphone','capture_supported':True}).json()['id']
                session=app.state.onboarding[id]
                session.speech.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,content=b'\x00\x10'*24000)))
                payload={'request_id':str(uuid4()),'text':'Hello','language':'en'}
                response=c.post(f'/api/onboarding/{id}/local-voice/speech',json=payload)
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.json()['audio_mime'],'audio/wav')
                self.assertEqual(c.post(f'/api/onboarding/{id}/local-voice/speech',json=payload).json(),response.json())
                self.assertEqual(len(session.state.metrics['speech_usage']),1)
                id=c.post('/api/sweeps',json={'device':'test','country':'India','currency':'INR','synthetic':True}).json()['sweep']['id']
                self.assertEqual(c.post(f'/api/sweeps/{id}/local-voice/guidance',json={'request_id':str(uuid4())}).json(),{'reply':'','event_id':''})
