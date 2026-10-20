"""Real Claude SDK boundary and authenticated assistant contract; no paid requests."""
import asyncio
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

import httpx2
from anthropic import AsyncAnthropic
from fastapi.testclient import TestClient
from pydantic import Field

from claim_agent.anthropic_provider import AnthropicProvider
from claim_agent.config import Settings
from claim_agent.models import Book, Item, Model
from claim_agent.providers import ProviderUnavailable
from claim_agent.server import create_app
from tests.test_anthropic import response
from tests.test_hybrid import jpeg
from claim_agent.vision import ProposedScan


def plan(**values):
    return {"reply":"Which language would you like?", "intent":"provide", "language":"", "country":"",
            "currency":"", "capture_mode":"", "speech_locale":"", "tool_name":"", "tool_arguments":"", **values}


def turn(**values):
    return {"request_id":str(uuid4()), "mode":"setup", "text":"English please", **values}


class Reply(Model):
    reply: str = Field(max_length=1200)


class ClaudeTextProviderTests(unittest.IsolatedAsyncioTestCase):
    async def make_provider(self, body=None, status=200, handler=None, **kwargs):
        self.requests=[]
        def respond(request):
            self.requests.append(request)
            return httpx2.Response(status,json=body if body is not None else response('{"reply":"Hello"}'))
        sdk=AsyncAnthropic(api_key="test-key",max_retries=3,
                           http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler or respond)))
        provider=AnthropicProvider(client=sdk,**kwargs)
        self.addAsyncCleanup(provider.close)
        self.assertTrue(callable(getattr(provider,"structured_text",None)), "Claude text generation is missing")
        return provider

    async def test_text_uses_structured_schema_server_instructions_and_independent_image_budget(self):
        provider=await self.make_provider(workspace_id="wrkspc_fixture",max_calls=0)
        result=await provider.structured_text("Owner text",Reply,system="Trusted instructions")
        self.assertEqual(result.reply,"Hello")
        request=self.requests[0]; payload=json.loads(request.content)
        self.assertEqual(payload['system'],'Trusted instructions')
        self.assertEqual(payload['messages'],[{'role':'user','content':'Owner text'}])
        self.assertLessEqual(payload['max_tokens'],4096)
        self.assertEqual(payload['output_config']['format']['type'],'json_schema')
        self.assertEqual(request.headers['anthropic-workspace-id'],'wrkspc_fixture')
        self.assertEqual(provider.calls,0)
        self.assertEqual(provider.usage[0]['usage']['input_tokens'],1000)
        with self.assertRaisesRegex(ProviderUnavailable,'budget'):
            await provider.structured_image('Inspect',jpeg(),ProposedScan)

    async def test_text_refusal_truncation_and_invalid_output_preserve_usage(self):
        for body in [response('{"reply":"Hello"}',stop='max_tokens'),response('{"reply":"Hello"}',stop='refusal'),
                     response('{"reply":123}'),response('{"reply":"'+'x'*1201+'"}')]:
            with self.subTest(body=body):
                provider=await self.make_provider(body)
                with self.assertRaises(ProviderUnavailable):
                    await provider.structured_text('Owner text',Reply,system='Trusted instructions')
                self.assertEqual(provider.usage[0]['usage']['output_tokens'],100)

    async def test_text_error_redacts_provider_details_and_disables_sdk_retries(self):
        provider=await self.make_provider({'type':'error','error':{'type':'overloaded_error','message':'private test-key'}},529)
        with self.assertRaises(ProviderUnavailable) as error:
            await provider.structured_text('Owner text',Reply,system='Trusted instructions')
        self.assertNotIn('private',str(error.exception)); self.assertNotIn('test-key',str(error.exception))
        self.assertEqual(len(self.requests),1)
        self.assertEqual(provider.usage[0]['error_type'],'overloaded_error')
        self.assertIsNone(provider.usage[0]['usage'])

    async def test_text_deadline_records_unknown_usage_without_retry(self):
        started=[]
        async def slow(request):
            started.append(request)
            await asyncio.sleep(1)
            return httpx2.Response(200,json=response('{"reply":"Hello"}'))
        provider=await self.make_provider(handler=slow)
        with self.assertRaises(ProviderUnavailable):
            await provider.structured_text('Owner text',Reply,system='Trusted instructions',timeout_s=.01)
        self.assertEqual(len(started),1)
        self.assertIsNone(provider.usage[0]['usage'])


class ClaudeAssistantAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.requests=[]; self.output=plan(); self.stop='end_turn'; self.http_status=200; self.provider_error=None
        self.request_started=None; self.request_release=None
        async def handler(request):
            self.requests.append(request)
            if self.request_started:
                self.request_started.set()
                await asyncio.to_thread(self.request_release.wait,3)
            body=self.provider_error or response(json.dumps(self.output),stop=self.stop)
            body['id']='msg_assistant_'+str(len(self.requests))
            return httpx2.Response(self.http_status,json=body)
        def factory(*args,**kwargs):
            sdk=AsyncAnthropic(api_key='test-key',max_retries=0,
                              http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
            return AnthropicProvider(client=sdk)
        self.enterContext(patch('claim_agent.server.AnthropicProvider',side_effect=factory))
        self.settings=Settings(data_dir=Path(self.tmp.name),pair_token='paired',provider='anthropic',
                               api_key='test-key',vision_model='claude-sonnet-5-5')
        self.app=create_app(self.settings)
        self.client=self.enterContext(TestClient(self.app))
        self.headers={'Authorization':'Bearer paired'}

    def setup_session(self):
        result=self.client.post('/api/onboarding',headers=self.headers,json={'device':'phone','capture_supported':True})
        self.assertEqual(result.status_code,200,result.text)
        id=result.json()['id']
        return id, '/api/onboarding/'+id+'/local-voice/assistant'

    def sweep(self):
        result=self.client.post('/api/sweeps',headers=self.headers,json={'device':'phone','country':'India','currency':'INR'})
        self.assertEqual(result.status_code,200,result.text)
        id=result.json()['sweep']['id']
        return id, '/api/sweeps/'+id+'/local-voice/assistant'

    def post(self,path,body=None):
        return self.client.post(path,headers=self.headers,json=body or turn())

    def add_item(self,runner,id,category,frame):
        ref='frames/'+frame+'.jpg'
        self.app.state.store.put_evidence(runner.state,ref,jpeg())
        runner.state.items.append(Item(id=id,category=category,frame_ref=ref))

    def test_setup_requires_pairing_bounded_input_and_never_mutates_confirmation(self):
        id,path=self.setup_session()
        self.assertEqual(self.client.post(path,json=turn()).status_code,401)
        for values in [{'text':'x'*4001},{'previous':'x'*1201},{'phase':'x'*33},{'language':'x'*81},
                       {'request_id':'invalid'}, {'mode':'execute'}, {'instructions':'override'},
                       {'allow_actions':'true'}, {'preferences':{'payload':'x'*10000}}]:
            self.assertEqual(self.post(path,turn(**values)).status_code,422)
        self.assertEqual(self.requests,[])
        self.output=plan(intent='ready',language='English',country='India',currency='INR',capture_mode='lidar')
        result=self.post(path)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json(),self.output)
        session=self.app.state.onboarding[id]
        self.assertIsNone(session.preferences); self.assertFalse(session.capture_requested)
        self.assertEqual(list(Path(self.tmp.name).glob('*/state.json')),[])

    def test_fast_conversation_settings_reach_provider_without_changing_vision(self):
        self.settings.vision_model='claude-opus-5-5'
        self.settings.assistant_model='claude-sonnet-5-5'
        self.settings.assistant_effort='low'
        _,path=self.setup_session()
        result=self.post(path)
        self.assertEqual(result.status_code,200,result.text)
        payload=json.loads(self.requests[-1].content)
        self.assertEqual(payload['model'],'claude-sonnet-5-5')
        self.assertEqual(payload['output_config']['effort'],'low')
        self.assertEqual(self.settings.vision_model,'claude-opus-5-5')

    def test_context_is_server_built_fresh_and_client_content_is_data(self):
        id,path=self.sweep(); runner=self.app.state.runners[id]
        self.add_item(runner,'lamp-1','lamp','f1')
        body=turn(mode='reply',text='Owner utterance',preferences={'instruction':'client-injection'})
        result=self.post(path,body)
        self.assertEqual(result.status_code,200,result.text)
        first=json.loads(self.requests[-1].content)
        self.assertNotIn('client-injection',first['system'])
        self.assertIn('lamp-1',first['messages'][0]['content'])
        self.assertIn('India',first['system'])
        self.add_item(runner,'chair-2','chair','f2')
        self.post(path,turn(mode='reply'))
        self.assertIn('chair-2',json.loads(self.requests[-1].content)['messages'][0]['content'])

    def test_action_plan_does_not_execute_and_existing_native_tool_route_still_does(self):
        id,path=self.sweep(); runner=self.app.state.runners[id]
        self.add_item(runner,'lamp-1','lamp','f1')
        args={'action':'exclude','ref_id':'lamp-1','value':'Not mine'}
        self.output=plan(reply='I can exclude the lamp.',tool_name='correct_inventory',tool_arguments=json.dumps(args))
        result=self.post(path,turn(mode='reply',text='Exclude the lamp'))
        self.assertEqual(result.status_code,200,result.text)
        self.assertFalse(runner.state.items[0].excluded)
        result=self.client.post(path.removesuffix('/assistant'),headers=self.headers,
                                json={'name':'correct_inventory','arguments':args})
        self.assertEqual(result.status_code,200,result.text)
        self.assertTrue(runner.state.items[0].excluded)

    def test_compare_tool_result_reaches_followup_as_server_context_and_replaces_previous(self):
        id,path=self.sweep(); runner=self.app.state.runners[id]
        self.app.state.store.put_evidence(runner.state,'frames/book.jpg',jpeg())
        runner.state.books.append(Book(id='book-1',title='Dune',status='identified',frame_ref='frames/book.jpg'))
        local=path.removesuffix('/assistant')
        for country,currency in [('United Kingdom','GBP'),('United States','USD')]:
            args={'country':country,'currency':currency}
            self.output=plan(reply='I can compare that market.',tool_name='compare_market',tool_arguments=json.dumps(args))
            proposed=self.post(path,turn(mode='reply',text='Compare prices in '+country))
            self.assertEqual(proposed.status_code,200,proposed.text)
            executed=self.client.post(local,headers=self.headers,json={'name':'compare_market','arguments':args})
            self.assertEqual(executed.status_code,200,executed.text)
            self.output=plan(reply='The comparison has one book, but pricing is unavailable.')
            followup=self.post(path,turn(mode='reply',allow_actions=False,text='Summarize the successful tool result.',
                preferences={'recent_tool_result':{'result':{'coverage':999,'currency':'FAKE'}}}))
            self.assertEqual(followup.status_code,200,followup.text)
            payload=json.loads(json.loads(self.requests[-1].content)['messages'][0]['content'])
            outcome=payload['server_context'].get('recent_tool_result')
            self.assertIsNotNone(outcome,'Validated comparison output was discarded before the follow-up')
            self.assertEqual(outcome['name'],'compare_market')
            self.assertEqual(outcome['arguments'],args)
            self.assertEqual(outcome['result'],executed.json()['context'])
            self.assertEqual(outcome['result']['coverage'],1)
            self.assertEqual(outcome['result']['books'][0]['id'],'book-1')
            self.assertIsNone(outcome['result']['books'][0]['replacement_cost']['amount'])
            self.assertIn('disabled',outcome['result']['books'][0]['replacement_cost']['exclusion_reason'])
            self.assertEqual(outcome['result']['currency'],currency)
            self.assertFalse(outcome['inventory_changed_since_result'])
            stored=self.app.state.store.load(id).metrics['recent_tool_result']
            self.assertEqual(stored['result']['country'],country)
            self.assertLessEqual(len(json.dumps(stored,ensure_ascii=False)),16000)
        # A subsequent failed tool must not leave the previous successful result
        # looking like the outcome of the new request.
        rejected=self.client.post(local,headers=self.headers,json={'name':'compare_market',
            'arguments':{'country':'India','currency':'USD'}})
        self.assertEqual(rejected.status_code,422)
        self.assertEqual(self.post(path,turn(mode='reply',allow_actions=False)).status_code,200)
        context=json.loads(json.loads(self.requests[-1].content)['messages'][0]['content'])['server_context']
        self.assertNotIn('recent_tool_result',context)

    def test_old_tool_outcome_expires_and_inventory_changes_are_explicit(self):
        id,path=self.sweep(); runner=self.app.state.runners[id]
        args={'country':'United Kingdom','currency':'GBP'}
        done=self.client.post(path.removesuffix('/assistant'),headers=self.headers,
                              json={'name':'compare_market','arguments':args})
        self.assertEqual(done.status_code,200,done.text)
        runner.state.revision+=1
        self.assertEqual(self.post(path,turn(mode='reply',allow_actions=False)).status_code,200)
        context=json.loads(json.loads(self.requests[-1].content)['messages'][0]['content'])['server_context']
        outcome=context.get('recent_tool_result')
        self.assertIsNotNone(outcome,'Recent tool outcome is missing')
        self.assertTrue(outcome['inventory_changed_since_result'])
        runner.state.metrics['recent_tool_result']['completed_at']='2000-01-01T00:00:00+00:00'
        self.assertEqual(self.post(path,turn(mode='reply',allow_actions=False)).status_code,200)
        context=json.loads(json.loads(self.requests[-1].content)['messages'][0]['content'])['server_context']
        self.assertNotIn('recent_tool_result',context)

    def test_setup_translation_and_read_only_turns_reject_actionable_output(self):
        _,setup_path=self.setup_session(); _,sweep_path=self.sweep()
        self.output=plan(tool_name='compare_market',tool_arguments='{"country":"India","currency":"INR"}')
        for path,body in [(setup_path,turn()),(setup_path,turn(mode='reply')),
                          (sweep_path,turn(mode='translate')),(sweep_path,turn(mode='reply',allow_actions=False))]:
            result=self.post(path,body)
            self.assertEqual(result.status_code,502,result.text)
        self.assertEqual(len(self.requests),4)
        self.assertEqual(self.app.state.runners[next(iter(self.app.state.runners))].snapshot().metrics['costed_requests'],2)

    def test_unknown_or_invalid_tool_arguments_never_become_an_action(self):
        _,path=self.sweep()
        for name,args in [('configure_session','{}'),('correct_inventory','[]'),('correct_inventory','{'),
                          ('correct_inventory','{"action":"exclude","ref_id":"missing","value":""}'),
                          ('compare_market','{"country":"India","currency":"USD"}'),
                          ('','{"action":"exclude"}')]:
            self.output=plan(tool_name=name,tool_arguments=args)
            result=self.post(path,turn(mode='reply'))
            self.assertEqual(result.status_code,502,result.text)

    def test_identical_retry_is_cached_conflict_rejected_and_budget_is_per_target(self):
        _,path=self.setup_session(); self.settings.assistant_max_calls=1
        body=turn(); first=self.post(path,body)
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(self.post(path,body).json(),first.json())
        self.assertEqual(self.post(path,body|{'text':'changed'}).status_code,409)
        self.assertEqual(self.post(path,turn()).status_code,429)
        self.assertEqual(len(self.requests),1)
        _,other=self.setup_session()
        self.assertEqual(self.post(other,body).status_code,200)
        self.assertEqual(len(self.requests),2)

    def test_concurrent_identical_requests_bill_once(self):
        _,path=self.setup_session(); body=turn()
        self.request_started=threading.Event(); self.request_release=threading.Event()
        with ThreadPoolExecutor(max_workers=2) as executor:
            first=executor.submit(self.post,path,body)
            self.assertTrue(self.request_started.wait(2),'Request never reached the SDK')
            second=executor.submit(self.post,path,body)
            self.request_release.set()
            for result in (first.result(timeout=5),second.result(timeout=5)):
                self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(len(self.requests),1)

    def test_failed_output_is_cached_and_billed_usage_persists_after_restart(self):
        id,path=self.sweep(); body=turn(mode='reply')
        self.stop='max_tokens'
        result=self.post(path,body)
        self.assertEqual(result.status_code,502,result.text)
        self.assertEqual(self.post(path,body).status_code,502)
        metrics=self.client.get('/api/sweeps/'+id,headers=self.headers).json()['metrics']
        self.assertEqual(metrics['costed_requests'],1)
        self.assertAlmostEqual(metrics['cost_usd'],.003)
        with TestClient(create_app(self.settings)) as restarted:
            replay=restarted.post(path,headers=self.headers,json=body)
            self.assertEqual(replay.status_code,502,replay.text)
            metrics=restarted.get('/api/sweeps/'+id,headers=self.headers).json()['metrics']
            self.assertEqual(metrics['costed_requests'],1)
        self.assertEqual(len(self.requests),1)

    def test_restoring_exact_inflight_disk_state_keeps_uncertain_charge_unknown(self):
        id,path=self.sweep(); body=turn(mode='reply')
        self.request_started=threading.Event(); self.request_release=threading.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending=executor.submit(self.post,path,body)
            try:
                self.assertTrue(self.request_started.wait(2))
                # Capture the actual durable snapshot while the SDK call has
                # started but has not returned usage, exactly as after a crash.
                interrupted=(Path(self.tmp.name)/id/'state.json').read_bytes()
            finally:
                self.request_release.set()
            self.assertEqual(pending.result(timeout=5).status_code,200)
        completed=self.client.get('/api/sweeps/'+id,headers=self.headers).json()['metrics']
        self.assertEqual(completed['costed_requests'],1)
        self.assertEqual(completed['uncosted_requests'],0)
        with tempfile.TemporaryDirectory() as root:
            restored=Path(root)/id; restored.mkdir()
            (restored/'state.json').write_bytes(interrupted)
            from dataclasses import replace
            with TestClient(create_app(replace(self.settings,data_dir=Path(root)))) as restarted:
                before=restarted.get('/api/sweeps/'+id,headers=self.headers).json()['metrics']
                self.assertEqual(before['cost_status'],'unavailable')
                self.assertIsNone(before['cost_usd'])
                self.assertEqual(before['uncosted_requests'],1)
                retry=restarted.post(path,headers=self.headers,json=body)
                self.assertEqual(retry.status_code,503,retry.text)
                after=restarted.get('/api/sweeps/'+id,headers=self.headers).json()['metrics']
                self.assertEqual(after['uncosted_requests'],1)
        self.assertEqual(len(self.requests),1)

    def test_setup_usage_transfers_once_and_sweep_text_and_vision_share_one_ledger(self):
        id,path=self.setup_session()
        self.assertEqual(self.post(path).status_code,200)
        local=path.removesuffix('/assistant')
        self.client.post(local,headers=self.headers,json={'name':'configure_session','arguments':{
            'language':'English','country':'India','currency':'INR','capture_mode':'lidar','confirmed':True}})
        self.client.post(local,headers=self.headers,json={'name':'request_capture','arguments':{'ready':True}})
        body={'device':'phone','onboarding_id':id}
        first=self.client.post('/api/sweeps',headers=self.headers,json=body)
        self.assertEqual(first.status_code,200,first.text)
        sid=first.json()['sweep']['id']; runner=self.app.state.runners[sid]
        self.assertEqual(first.json()['metrics']['costed_requests'],1)
        self.output=plan(reply='Please scan the shelves.')
        self.assertEqual(self.post('/api/sweeps/'+sid+'/local-voice/assistant',turn(mode='reply')).status_code,200)
        runner.provider.usage.append({'provider':'anthropic','model':'claude-sonnet-5-5','response_id':'msg_vision',
                                      'usage':response()['usage']})
        packet=runner.snapshot()
        self.assertEqual(packet.metrics['costed_requests'],3)
        self.assertEqual(len(packet.metrics['provider_usage']),2)
        self.assertEqual(len(packet.metrics['onboarding_assistant_usage']),1)
        retry=self.client.post('/api/sweeps',headers=self.headers,json=body)
        self.assertEqual(retry.json()['metrics']['costed_requests'],3)

    def test_handoff_during_assistant_request_cannot_lose_setup_usage(self):
        id,path=self.setup_session(); local=path.removesuffix('/assistant')
        self.client.post(local,headers=self.headers,json={'name':'configure_session','arguments':{
            'language':'English','country':'India','currency':'INR','capture_mode':'lidar','confirmed':True}})
        self.client.post(local,headers=self.headers,json={'name':'request_capture','arguments':{'ready':True}})
        self.request_started=threading.Event(); self.request_release=threading.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending=executor.submit(self.post,path,turn())
            try:
                self.assertTrue(self.request_started.wait(2))
                handoff=self.client.post('/api/sweeps',headers=self.headers,json={'device':'phone','onboarding_id':id})
                self.assertEqual(handoff.status_code,409,handoff.text)
            finally:
                self.request_release.set()
            self.assertEqual(pending.result(timeout=5).status_code,200)
        self.assertFalse(self.app.state.onboarding[id].consumed)

    def test_health_declares_claude_assistant_support_without_keys(self):
        data=self.client.get('/api/health').json()
        self.assertEqual(data.get('assistant_provider'),'anthropic')
        self.assertTrue(data.get('assistant_supported'))
        self.assertEqual(data.get('assistant_model'),'claude-sonnet-5-5')
        self.assertNotIn('test-key',json.dumps(data))
