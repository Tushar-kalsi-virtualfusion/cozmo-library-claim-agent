import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx2
from anthropic import AsyncAnthropic
from fastapi.testclient import TestClient

from claim_agent.config import Settings
from claim_agent.costing import usage_cost
from claim_agent.local_evidence import latch_credit_error
from claim_agent.providers import ProviderUnavailable
from claim_agent.server import create_app
from claim_agent.vision import ProposedScan
from tests.test_hybrid import jpeg


def response(text='{"detections":[],"quality_issues":[],"guidance":"Hold steady"}', stop="end_turn"):
    return {"id":"msg_fixture", "type":"message", "role":"assistant", "model":"claude-sonnet-5-5",
            "content":[{"type":"text","text":text}], "stop_reason":stop,"stop_sequence":None,
            "usage":{"input_tokens":1000,"output_tokens":100,"cache_creation_input_tokens":0,
                     "cache_read_input_tokens":0,"service_tier":"standard","inference_geo":"global"}}


class ClaudeProviderTests(unittest.IsolatedAsyncioTestCase):
    async def provider(self, body=None, status=200, **kwargs):
        try:
            from claim_agent.anthropic_provider import AnthropicProvider
        except ImportError:
            self.fail("Claude provider has not been implemented")
        self.requests=[]
        def handler(request):
            self.requests.append(request)
            return httpx2.Response(status,json=body if body is not None else response())
        client=AsyncAnthropic(api_key="test-key",max_retries=0,
                              http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
        provider=AnthropicProvider(client=client,**kwargs)
        self.addAsyncCleanup(provider.close)
        return provider

    async def test_image_schema_workspace_and_usage_reach_real_sdk_boundary(self):
        provider=await self.provider(workspace_id="wrkspc_fixture")
        photo=jpeg()
        scan=await provider.structured_image("Inventory visible objects",photo,ProposedScan)
        request=self.requests[0]; payload=json.loads(request.content)
        self.assertEqual(scan.guidance,"Hold steady")
        self.assertEqual(request.headers['anthropic-workspace-id'],"wrkspc_fixture")
        self.assertEqual(str(request.url),'https://api.anthropic.com/v1/messages')
        content=payload['messages'][0]['content']
        self.assertEqual(base64.b64decode(content[0]['source']['data']),photo)
        self.assertEqual(content[0]['source']['media_type'],'image/jpeg')
        self.assertEqual(payload['output_config']['format']['type'],'json_schema')
        self.assertEqual(payload['output_config']['effort'],'high')
        self.assertFalse(payload['output_config']['format']['schema']['additionalProperties'])
        self.assertEqual(provider.usage[0]['usage']['input_tokens'],1000)
        self.assertEqual(provider.usage[0]['provider'],'anthropic')
        self.assertEqual(provider.usage[0]['response_id'],'msg_fixture')

    async def test_truncated_json_is_not_inventory_but_usage_is_retained(self):
        provider=await self.provider(response(stop='max_tokens'))
        with self.assertRaises(ProviderUnavailable):
            await provider.structured_image('Inventory',jpeg(),ProposedScan)
        self.assertEqual(provider.usage[0]['usage']['output_tokens'],100)

    async def test_invalid_structured_output_does_not_lose_billable_usage(self):
        provider=await self.provider(response('{"detections":[{"kind":"invented"}]}'))
        with self.assertRaises(ProviderUnavailable):
            await provider.structured_image('Inventory',jpeg(),ProposedScan)
        self.assertEqual(len(provider.usage),1)
        self.assertEqual(provider.usage[0]['usage']['input_tokens'],1000)

    async def test_access_error_is_redacted_latched_and_not_retried(self):
        provider=await self.provider({'type':'error','error':{'type':'authentication_error','message':'private diagnostic test-key'}},401)
        for _ in range(2):
            with self.assertRaises(ProviderUnavailable) as error:
                await provider.structured_image('Inventory',jpeg(),ProposedScan)
            self.assertNotIn('private diagnostic',str(error.exception))
            self.assertNotIn('test-key',str(error.exception))
        self.assertEqual(len(self.requests),1)
        metrics={}; latch_credit_error(metrics,error.exception)
        self.assertTrue(metrics['cloud_routing']['disabled_reason'])

    async def test_workspace_and_credit_errors_stop_repeated_calls(self):
        for message in ['anthropic-workspace-id is required','Your credit balance is too low']:
            with self.subTest(message=message):
                provider=await self.provider({'type':'error','error':{'type':'invalid_request_error','message':message}},400)
                for _ in range(2):
                    with self.assertRaises(ProviderUnavailable) as error:
                        await provider.structured_image('Inventory',jpeg(),ProposedScan)
                self.assertEqual(len(self.requests),1)
                metrics={}; latch_credit_error(metrics,error.exception)
                self.assertTrue(metrics['cloud_routing']['disabled_reason'])

    async def test_budget_and_invalid_images_make_no_network_request(self):
        provider=await self.provider(max_calls=0)
        with self.assertRaisesRegex(ProviderUnavailable,'budget'):
            await provider.structured_image('Inventory',jpeg(),ProposedScan)
        self.assertEqual(self.requests,[])
        provider.max_calls=1
        with self.assertRaises(ProviderUnavailable):
            await provider.structured_image('Inventory',b'not a jpeg',ProposedScan)
        self.assertEqual(self.requests,[])

    async def test_pricing_does_not_silently_call_another_provider(self):
        provider=await self.provider()
        with self.assertRaisesRegex(ProviderUnavailable,'search'):
            await provider.search_urls('Dune India')
        self.assertEqual(len(self.requests),1)


class ClaudeConfigurationTests(unittest.TestCase):
    def test_environment_selects_anthropic_credentials_without_openai_fallback(self):
        env={'CLAIM_PROVIDER':'anthropic','ANTHROPIC_API_KEY':'test-claude',
             'ANTHROPIC_WORKSPACE_ID':'wrkspc_fixture','OPENAI_API_KEY':'test-openai'}
        with patch.dict(os.environ,env,clear=True), patch('claim_agent.config.load_dotenv'):
            try: settings=Settings.from_env()
            except ValueError as error: self.fail(str(error))
        self.assertEqual(settings.api_key,'test-claude')
        self.assertEqual(settings.anthropic_workspace_id,'wrkspc_fixture')
        self.assertTrue(settings.vision_model.startswith('claude-'))
        self.assertEqual(settings.live_model,'')
        self.assertTrue(settings.local_first)
        self.assertFalse(settings.cloud_voice or settings.paid_pricing)

    def test_anthropic_factory_and_local_voice_routes_do_not_start_gemini(self):
        with tempfile.TemporaryDirectory() as root:
            settings=Settings(data_dir=Path(root),pair_token='test',provider='anthropic',api_key='test-key',
                              vision_model='claude-sonnet-5-5',cloud_voice=True)
            app=create_app(settings)
            with TestClient(app) as client:
                headers={'Authorization':'Bearer test'}
                reply=client.post('/api/onboarding',headers=headers,json={'device':'test','capture_supported':True})
                self.assertEqual(reply.status_code,200)
                session=app.state.onboarding[reply.json()['id']]
                self.assertEqual(type(session.provider).__name__,'AnthropicProvider')
                context=client.get('/api/onboarding/'+session.id+'/local-voice',headers=headers)
                self.assertEqual(context.status_code,200)
                with client.websocket_connect('/api/onboarding/'+session.id+'/live',headers=headers) as socket:
                    socket.send_json({'type':'start_voice'})
                    event=socket.receive_json()
                    self.assertEqual(event['type'],'voice_error')
                    self.assertIsNone(session.live)

    def test_invalid_workspace_configuration_does_not_expose_key(self):
        settings=Settings(provider='anthropic',api_key='test-private',anthropic_workspace_id='default',vision_model='claude-sonnet-5-5')
        with tempfile.TemporaryDirectory() as root:
            settings.data_dir=Path(root)
            with TestClient(create_app(settings)) as client:
                data=client.get('/api/health').json()
                self.assertFalse(data['model_ready'])
                self.assertIn('workspace',data['provider_configuration_error'].lower())
                self.assertNotIn(settings.api_key,json.dumps(data))

    def test_claude_cost_includes_cache_reads_and_both_cache_write_durations(self):
        usage=response()['usage']
        usage.update(cache_read_input_tokens=2000,cache_creation_input_tokens=1000,
                     cache_creation={'ephemeral_5m_input_tokens':600,'ephemeral_1h_input_tokens':400})
        row={'provider':'anthropic','model':'claude-sonnet-5-5','response_id':'msg_test','usage':usage}
        result=usage_cost([row,row])
        # 1000*2 + 2000*.1 + 600*2.5 + 400*4 + 100*10, per million.
        self.assertIsNotNone(result['cost_usd'])
        self.assertAlmostEqual(result['cost_usd'],.0063)
        self.assertEqual(result['costed_requests'],1)
        self.assertEqual(result['cost_status'],'estimated')

    def test_unknown_cache_breakdown_or_nonstandard_tier_is_not_undercosted(self):
        for extra in [{'cache_creation_input_tokens':100}, {'service_tier':'priority'}, {'inference_geo':'us'}]:
            usage=response()['usage']|extra
            result=usage_cost([{'provider':'anthropic','model':'claude-sonnet-5-5','usage':usage}])
            self.assertEqual(result['cost_status'],'unavailable')
            self.assertIsNone(result['cost_usd'])
