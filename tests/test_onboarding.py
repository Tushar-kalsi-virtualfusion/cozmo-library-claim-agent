import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import json

from fastapi.testclient import TestClient

from claim_agent.config import Settings
from claim_agent.server import create_app
from claim_agent.onboarding import OnboardingSession
from claim_agent.openai_live import OpenAILiveBridge
from tests.test_api import upload_payload


class OnboardingAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = self.enterContext(TestClient(create_app(Settings(data_dir=Path(self.tmp.name), pair_token="test-token"))))
        self.headers = {"Authorization": "Bearer test-token"}

    def setup_session(self, supported=True):
        response = self.client.post("/api/onboarding", headers=self.headers,
                                    json={"device": "test phone", "capture_supported": supported})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["id"]

    def test_setup_is_authenticated_and_does_not_create_a_sweep(self):
        self.assertEqual(self.client.post("/api/onboarding", json={"device":"test","capture_supported":False}).status_code, 401)
        self.setup_session(False)
        self.assertEqual(self.client.get("/api/sweeps", headers=self.headers).json(), [])

    def test_spoken_confirmation_and_readiness_required_before_handoff(self):
        id = self.setup_session()
        session = self.client.app.state.onboarding[id]
        body = {"device":"test phone", "onboarding_id":id}
        self.assertEqual(self.client.post("/api/sweeps", headers=self.headers, json=body).status_code, 409)
        asyncio.run(session.invoke_voice_tool("configure_session", {"language":"Hindi", "country":"India", "currency":"INR", "capture_mode":"lidar","confirmed":True}))
        self.assertEqual(self.client.post("/api/sweeps", headers=self.headers, json=body).status_code, 409)
        session.state.events.append({"type":"transcript","role":"user","text":"Hindi, India, rupees. Yes, ready."})
        session.state.metrics["live_usage"] = [{"response_id":"setup-test","usage":{"total_tokens":10}}]
        asyncio.run(session.invoke_voice_tool("request_capture", {"ready":True}))
        result = self.client.post("/api/sweeps", headers=self.headers, json=body)
        self.assertEqual(result.status_code, 200, result.text)
        sweep = result.json()["sweep"]
        self.assertEqual((sweep["country"],sweep["currency"],sweep["language"]), ("India","INR","Hindi"))
        runner = self.client.app.state.runners[sweep["id"]]
        self.assertEqual(runner.state.metrics["onboarding_live_usage"][0]["response_id"], "setup-test")
        self.assertTrue(runner.state.events)
        retry = self.client.post("/api/sweeps", headers=self.headers, json=body)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["sweep"]["id"], sweep["id"])
        self.assertEqual(len(self.client.get("/api/sweeps", headers=self.headers).json()), 1)

    def test_lost_handoff_reply_recovers_after_session_cleanup_and_restart(self):
        id = self.setup_session()
        session = self.client.app.state.onboarding[id]
        asyncio.run(session.invoke_voice_tool("configure_session", {"language":"Hindi", "country":"India", "currency":"INR", "capture_mode":"comparison", "confirmed":True}))
        asyncio.run(session.invoke_voice_tool("request_capture", {"ready":True}))
        body = {"device":"test phone", "onboarding_id":id}
        # The server succeeded; the device did not receive/decode this first reply.
        first = self.client.post("/api/sweeps", headers=self.headers, json=body).json()
        self.setup_session()  # Prunes consumed sessions.
        self.assertNotIn(id, self.client.app.state.onboarding)
        retry = self.client.post("/api/sweeps", headers=self.headers, json=body)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["sweep"]["id"], first["sweep"]["id"])
        with TestClient(create_app(Settings(data_dir=Path(self.tmp.name), pair_token="test-token"))) as restarted:
            recovered = restarted.post("/api/sweeps", headers=self.headers, json=body)
            self.assertEqual(recovered.status_code, 200, recovered.text)
            self.assertEqual(recovered.json()["sweep"]["id"], first["sweep"]["id"])
            self.assertEqual(recovered.json()["sweep"]["language"], "Hindi")
            self.assertTrue(recovered.json()["sweep"]["comparison_requested"])
            self.assertEqual(len(restarted.get("/api/sweeps", headers=self.headers).json()), 1)
            wrong_device = restarted.post("/api/sweeps", headers=self.headers, json={**body,"device":"other phone"})
            self.assertEqual(wrong_device.status_code, 409)

    def test_simulator_and_invalid_preferences_never_start_real_capture(self):
        id = self.setup_session(False)
        session = self.client.app.state.onboarding[id]
        for args in [
            {"language":"Hindi","country":"India","currency":"USD","capture_mode":"lidar","confirmed":True},
            {"language":"Hindi","country":"India","currency":"INR","capture_mode":"lidar","confirmed":False},
            {"language":"","country":"India","currency":"INR","capture_mode":"lidar","confirmed":True},
        ]:
            with self.assertRaises(ValueError): asyncio.run(session.invoke_voice_tool("configure_session", args))
        self.assertIsNone(session.preferences)
        asyncio.run(session.invoke_voice_tool("configure_session", {"language":"Hindi","country":"India","currency":"INR","capture_mode":"camera","confirmed":True}))
        with self.assertRaisesRegex(RuntimeError,"physical"):
            asyncio.run(session.invoke_voice_tool("request_capture", {"ready":True}))
        self.assertFalse(session.capture_requested)
        self.assertEqual(self.client.get("/api/sweeps", headers=self.headers).json(), [])

    def test_expired_setup_cannot_be_consumed(self):
        id = self.setup_session()
        session = self.client.app.state.onboarding[id]
        asyncio.run(session.invoke_voice_tool("configure_session", {"language":"English","country":"India","currency":"INR","capture_mode":"lidar","confirmed":True}))
        asyncio.run(session.invoke_voice_tool("request_capture", {"ready":True}))
        session.created_at -= 601
        result = self.client.post("/api/sweeps", headers=self.headers, json={"device":"test", "onboarding_id":id})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.json()['detail']['code'],'handoff_unavailable')
        self.client.app.state.onboarding.pop(id)
        missing=self.client.post('/api/sweeps',headers=self.headers,json={"device":"test","onboarding_id":id})
        self.assertEqual(missing.json()['detail']['code'],'handoff_unavailable')
        self.assertEqual(self.client.get('/api/sweeps',headers=self.headers).json(),[])

    def test_camera_handoff_cannot_accept_lidar_evidence(self):
        response=self.client.post('/api/onboarding',headers=self.headers,json={"device":"camera phone","capture_supported":False,"camera_supported":True})
        id=response.json()['id']; session=self.client.app.state.onboarding[id]
        asyncio.run(session.invoke_voice_tool('configure_session',{"language":"English","country":"India","currency":"INR","capture_mode":"camera","confirmed":True}))
        asyncio.run(session.invoke_voice_tool('request_capture',{"ready":True}))
        sweep=self.client.post('/api/sweeps',headers=self.headers,json={"device":"camera phone","onboarding_id":id}).json()['sweep']
        self.assertEqual(sweep['capture_mode'],'camera')
        self.assertFalse(sweep['comparison_requested'])
        path=f"/api/sweeps/{sweep['id']}"
        payload=upload_payload()
        self.assertEqual(self.client.post(path+'/frames',headers=self.headers,json=payload).status_code,422)
        payload['metadata'].update(depth_width=0,depth_height=0)
        self.assertEqual(self.client.post(path+'/frames',headers=self.headers,json=payload).status_code,200)
        self.assertEqual(self.client.post(path+'/geometry',headers=self.headers,json={"ar_session_id":"session"}).status_code,422)

    def test_setup_websocket_requires_pairing_and_returns_confirmed_preferences(self):
        from starlette.websockets import WebSocketDisconnect
        id=self.setup_session()
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(f'/api/onboarding/{id}/live'): pass
        session=self.client.app.state.onboarding[id]
        asyncio.run(session.invoke_voice_tool('configure_session',{"language":"English","country":"India","currency":"INR","capture_mode":"comparison","confirmed":True}))
        with self.client.websocket_connect(f'/api/onboarding/{id}/live',headers=self.headers) as ws:
            event=ws.receive_json()
            self.assertEqual(event['type'],'setup_confirmed')
            self.assertEqual(event['preferences']['capture_mode'],'comparison')


class SpokenToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_interrupted_setup_cannot_confirm_or_request_capture(self):
        session=OnboardingSession('test',True)
        bridge=OpenAILiveBridge(session,SimpleNamespace(client=True,key='test'),'model')
        sent=[]
        class Socket:
            async def send(self,value): sent.append(json.loads(value))
        socket=Socket()
        await bridge.handle_event(socket,{"type":"response.created","response":{"id":"r1"}})
        await bridge.handle_event(socket,{"type":"input_audio_buffer.speech_started"})
        await bridge.handle_event(socket,{"type":"response.function_call_arguments.done","response_id":"r1","name":"configure_session","call_id":"c1",
            "arguments":json.dumps({"language":"English","country":"India","currency":"INR","capture_mode":"lidar","confirmed":True})})
        self.assertIsNone(session.preferences)
        self.assertFalse(session.capture_requested)
        self.assertEqual(sent,[])

    async def test_spoken_market_request_returns_actual_results(self):
        calls=[]
        async def second(locale):
            calls.append(locale)
            return {"coverage":0,"books":[],"currency":locale.currency}
        runner=SimpleNamespace(second_locale=second,publish=lambda event:None)
        bridge=OpenAILiveBridge(runner,None,'model')
        result=await bridge.invoke_tool('compare_market',{"country":"United Kingdom","currency":"GBP"})
        self.assertEqual(calls[0].currency,'GBP')
        self.assertEqual(result['coverage'],0)
        with self.assertRaises(ValueError):
            await bridge.invoke_tool('compare_market',{"country":"India","currency":"USD"})
