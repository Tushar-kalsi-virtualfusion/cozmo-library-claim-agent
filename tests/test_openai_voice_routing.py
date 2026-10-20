import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from uuid import uuid4

from fastapi.testclient import TestClient

from claim_agent.config import Settings
from claim_agent.server import create_app


class OpenAIVoiceRoutingTests(unittest.TestCase):
    def test_openai_selects_realtime_even_with_old_speech_credentials(self):
        env = {'CLAIM_PROVIDER': 'openai', 'OPENAI_API_KEY': 'test-openai',
               'ELEVENLABS_API_KEY': 'test-eleven', 'ANTHROPIC_API_KEY': 'test-claude',
               'CLAIM_SPEECH_PROVIDER': 'elevenlabs', 'CLAIM_SPEECH_OUTPUT_MODEL': 'eleven_v4',
               'CLAIM_ASSISTANT_MODEL': 'claude-opus-5-5'}
        with patch.dict(os.environ, env, clear=True), patch('claim_agent.config.load_dotenv'):
            settings = Settings.from_env()
        self.assertEqual(settings.speech_provider, 'openai')
        self.assertEqual(settings.assistant_model, settings.live_model)
        self.assertEqual(settings.speech_output_model, settings.live_model)
        self.assertEqual(settings.speech_voice, 'marin')
        self.assertFalse(settings.cloud_voice)  # Explicit billing opt-in remains required.

    def test_health_and_old_rest_routes_never_fall_back_to_other_providers(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory), pair_token='pair', provider='openai',
                                api_key='test-openai', vision_model='gpt-6-astra',
                                live_model='gpt-realtime-2.1', cloud_voice=True,
                                speech_provider='openai', elevenlabs_api_key='private-eleven')
            with TestClient(create_app(settings)) as client:
                headers = {'Authorization': 'Bearer pair'}
                health = client.get('/api/health').json()
                self.assertEqual(health['voice_transport'], 'realtime')
                self.assertTrue(health['realtime_ready'])
                self.assertEqual(health['assistant_provider'], 'openai')
                self.assertEqual(health['assistant_model'], 'gpt-realtime-2.1')
                self.assertEqual(health['speech_provider'], 'openai')
                self.assertNotIn('test-openai', str(health))
                session_id = client.post('/api/onboarding', headers=headers, json={
                    'device': 'test', 'capture_supported': True}).json()['id']
                target = client.app.state.onboarding[session_id]
                path = f'/api/onboarding/{session_id}/local-voice'
                context = client.get(path, headers=headers).json()
                self.assertEqual(context['speech']['transport'], 'realtime')
                self.assertTrue(context['speech']['ready'])
                with patch.object(target.speech.client, 'post', new_callable=AsyncMock) as post:
                    old = client.post(path+'/speech', headers=headers, json={
                        'request_id': str(uuid4()), 'text': 'Hello'})
                    self.assertEqual(old.status_code, 503)
                    post.assert_not_called()
                settings.api_key = ''
                health = client.get('/api/health').json()
                self.assertEqual(health['voice_transport'], 'realtime')
                self.assertFalse(health['realtime_ready'])
                self.assertFalse(health['speech_ready'])

    def test_socket_routes_manual_interrupt_to_owned_bridge(self):
        class Bridge:
            def __init__(self, runner, provider, model):
                self.runner = runner
                self.events = []
            def start(self):
                self.runner.publish({'type': 'voice_ready'})
            def offer(self, event):
                self.events.append(event)
                self.runner.publish({'type': 'interrupted'})
            async def close(self): pass

        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory), pair_token='pair', provider='openai',
                                api_key='test-openai', cloud_voice=True)
            with TestClient(create_app(settings)) as client, patch('claim_agent.server.OpenAILiveBridge', Bridge):
                headers = {'Authorization': 'Bearer pair'}
                session_id = client.post('/api/onboarding', headers=headers, json={
                    'device': 'test', 'capture_supported': True}).json()['id']
                target = client.app.state.onboarding[session_id]
                with client.websocket_connect(f'/api/onboarding/{session_id}/live', headers=headers) as ws:
                    ws.send_json({'type': 'start_voice'})
                    self.assertEqual(ws.receive_json()['type'], 'voice_ready')
                    ws.send_json({'type': 'interrupt_voice'})
                    self.assertEqual(ws.receive_json()['type'], 'interrupted')
                    self.assertEqual(target.live.events, [('interrupt', None)])
