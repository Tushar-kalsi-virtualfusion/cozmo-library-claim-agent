"""Realtime setup proofs: output streams before STT; only later heard turns mutate."""
import asyncio
import base64
import json
import unittest
from types import SimpleNamespace

from claim_agent.onboarding import OnboardingSession
from claim_agent.openai_live import OpenAILiveBridge, RealtimeSetupGate, bounded_voice_context, spoken_text


class Socket:
    def __init__(self): self.sent=[]
    async def send(self, value): self.sent.append(json.loads(value))


class RealtimeSetupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runner=OnboardingSession('phone',True,True)
        self.events=[]
        self.runner.publish=self.events.append
        self.bridge=OpenAILiveBridge(self.runner,SimpleNamespace(client=True,key='fake'),'model')
        self.socket=Socket()
        self.candidate={'language':'English','country':'India','currency':'INR','capture_mode':'lidar',
            'readback_template':'We will speak {language}, price in {currency} in {country}, and use {capture_mode}. Is that correct?',
            'readiness_question':'Are you ready to start the room sweep?'}
        self.preferences={k:v for k,v in self.candidate.items() if k not in {'readback_template','readiness_question'}}|{'confirmed':True}

    async def asyncTearDown(self):
        tasks=list(self.bridge.tool_tasks)
        if self.bridge.guidance_timer: tasks.append(self.bridge.guidance_timer)
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)

    async def event(self,type,**values):
        await self.bridge.handle_event(self.socket,{'type':type,**values})

    async def owner(self,id,text=None,*,created_first=False):
        await self.event('input_audio_buffer.speech_started',item_id=id)
        await self.event('input_audio_buffer.speech_stopped',item_id=id)
        if created_first: await self.event('response.created',response={'id':'r-'+id})
        await self.event('input_audio_buffer.committed',item_id=id)
        if not created_first: await self.event('response.created',response={'id':'r-'+id})
        if text is not None: await self.transcribe(id,text)
        return 'r-'+id

    async def transcribe(self,id,text):
        await self.event('conversation.item.input_audio_transcription.completed',item_id=id,transcript=text)

    async def tool(self,id,name,args,call_id=None,wait=True):
        await self.event('response.function_call_arguments.done',response_id=id,name=name,
            arguments=json.dumps(args),call_id=call_id or name+'-'+id)
        if wait:
            await asyncio.gather(*self.bridge.tool_tasks)
            await asyncio.sleep(0)

    async def finish(self,id):
        await self.event('response.done',response={'id':id,'status':'completed','usage':{'total_tokens':1}})

    async def prompt(self,id,*,ack=100,transcript=None):
        request=self.socket.sent[-1]['response']
        self.assertEqual(request['tool_choice'],'none')
        await self.event('response.created',response={'id':id,'metadata':request['metadata']})
        expected=self.bridge.responses[id]['prompt'][1]
        await self.event('response.output_audio.delta',response_id=id,item_id='a-'+id,
            delta=base64.b64encode(b'\x00\x10'*2400).decode())
        await self.event('response.output_audio.done',response_id=id,item_id='a-'+id)
        await self.event('response.output_audio_transcript.done',response_id=id,transcript=expected if transcript is None else transcript)
        await self.finish(id)
        self.bridge.acknowledge({'item_id':'a-'+id,'played_ms':ack})

    async def prepare(self):
        response=await self.owner('settings','English, India, Indian rupees.')
        await self.tool(response,'present_setup',self.candidate)
        await self.finish(response)
        self.assertIsNone(self.runner.preferences)

    async def test_audio_streams_before_transcription_but_tools_wait(self):
        response=await self.owner('settings',created_first=True)
        await self.tool(response,'present_setup',self.candidate,wait=False)
        await self.event('response.output_audio.delta',response_id=response,item_id='fast',delta=base64.b64encode(b'\1\0'*24).decode())
        self.assertTrue(any(e['type']=='audio' for e in self.events))
        self.assertEqual(self.bridge.setup_gate.phase,'collecting')
        await self.transcribe('settings','English, India and rupees please.')
        await asyncio.gather(*self.bridge.tool_tasks)
        self.assertEqual(self.bridge.setup_gate.phase,'readback_prompt')

    async def test_full_readback_then_distinct_confirmation_then_distinct_readiness(self):
        await self.prepare()
        await self.prompt('readback',ack=40)
        self.assertEqual(self.bridge.setup_gate.phase,'readback_prompt')
        # Acknowledging only part cannot authorize confirmation, even with a transcript.
        with self.assertRaises(ValueError):
            await self.bridge.invoke_tool('configure_session',self.preferences,turn=self.bridge.turns['settings'])
        self.bridge.acknowledge({'item_id':'a-readback','played_ms':100})
        self.assertEqual(self.bridge.setup_gate.phase,'confirmation')
        response=await self.owner('confirmation')
        await self.tool(response,'configure_session',self.preferences,wait=False)
        await asyncio.sleep(0)
        self.assertIsNone(self.runner.preferences)
        await self.transcribe('confirmation','Yes, those settings are correct.')
        await asyncio.gather(*self.bridge.tool_tasks); await asyncio.sleep(0)
        self.assertIsNotNone(self.runner.preferences)
        self.assertFalse(self.runner.capture_requested)
        await self.finish(response)
        await self.prompt('readiness')
        with self.assertRaises(ValueError):
            await self.bridge.invoke_tool('request_capture',{'ready':True},turn=self.bridge.turns['confirmation'])
        response=await self.owner('ready','Yes, I am ready.')
        await self.tool(response,'request_capture',{'ready':True})
        self.assertTrue(self.runner.capture_requested)

    async def test_no_prompt_no_action_and_blank_transcript_recovers(self):
        response=await self.owner('noise',' ... ')
        await self.tool(response,'configure_session',self.preferences)
        self.assertIsNone(self.runner.preferences)
        await self.finish(response)
        self.assertFalse(any(e['type']=='voice_error' for e in self.events))
        response=await self.owner('real','English, India and rupees.')
        await self.tool(response,'present_setup',self.candidate)
        self.assertEqual(self.bridge.setup_gate.phase,'readback_prompt')

    async def test_readback_interrupted_and_late_playback_cannot_confirm(self):
        await self.prepare(); await self.prompt('readback',ack=40)
        await self.bridge.interrupt(self.socket,manual=True)
        self.bridge.acknowledge({'item_id':'a-readback','played_ms':100})
        response=await self.owner('early','Yes.')
        await self.tool(response,'configure_session',self.preferences)
        self.assertIsNone(self.runner.preferences)
        self.assertEqual(self.bridge.setup_gate.phase,'readback_prompt')
        self.assertTrue(any(e['type']=='interrupted' for e in self.events))

    async def test_mismatch_has_one_bounded_repair_and_no_idle_loop(self):
        await self.prepare(); await self.prompt('bad',transcript='Only English.')
        self.assertEqual(self.bridge.setup_gate.phase,'readback_prompt')
        await self.bridge.maybe_respond(self.socket)
        await self.prompt('bad-again',transcript='Only English.')
        before=len(self.socket.sent)
        await self.bridge.maybe_respond(self.socket)
        await self.event('input_audio_buffer.timeout_triggered')
        self.assertEqual(len(self.socket.sent),before)
        self.assertEqual(self.bridge.setup_gate.phase,'readback_prompt')

    async def test_hindi_prompt_and_saved_readiness_survive_reconnect(self):
        self.candidate.update(language='Hindi',readback_template='भाषा {language}, देश {country}, मुद्रा {currency} और स्कैन {capture_mode} होगा। क्या यह सही है?',readiness_question='क्या आप स्कैन शुरू करने के लिए तैयार हैं?')
        await self.prepare()
        self.assertIn('भारतीय रुपये',self.bridge.setup_gate.readback)
        self.assertNotIn('lidar',self.bridge.setup_gate.readback)
        self.runner.preferences=self.bridge.setup_gate.preferences
        fresh=RealtimeSetupGate(self.runner)
        self.assertEqual(fresh.readiness,self.candidate['readiness_question'])
        self.assertEqual(spoken_text('INR, LiDAR'),spoken_text('Indian rupees — लाइडार'))

    async def test_duplicate_tool_and_usage_events_do_not_repeat_mutation_or_charge(self):
        response=await self.owner('settings','English India rupees')
        await self.tool(response,'present_setup',self.candidate,call_id='same')
        version=self.bridge.setup_gate.version
        await self.tool(response,'present_setup',self.candidate,call_id='same')
        self.assertEqual(self.bridge.setup_gate.version,version)
        await self.tool(response,'present_setup',self.candidate,call_id='different-call-same-action')
        self.assertEqual(self.bridge.setup_gate.version,version)
        await self.finish(response); await self.finish(response)
        rows=self.runner.state.metrics['live_usage']
        self.assertEqual(len([row for row in rows if row['response_id']==response]),1)

    def test_unknown_usage_is_preserved_then_upgraded_without_double_counting(self):
        self.bridge.record_usage('model','aborted',None)
        self.assertIsNone(self.runner.state.metrics['live_usage'][0]['usage'])
        self.bridge.record_usage('model','aborted',{'total_tokens':12})
        self.assertEqual(len(self.runner.state.metrics['live_usage']),1)
        self.assertEqual(self.runner.state.metrics['live_usage'][0]['usage'],{'total_tokens':12})

    async def test_manual_interrupt_even_idle_reopens_native_audio(self):
        await self.bridge.interrupt(self.socket,manual=True)
        self.assertEqual(self.events[-1],{'type':'interrupted'})
        await self.bridge.create_response(self.socket,'Hello')
        metadata=self.socket.sent[-1]['response']['metadata']
        await self.bridge.interrupt(self.socket,manual=True)
        await self.event('response.created',response={'id':'late','metadata':metadata})
        await self.event('response.output_audio.delta',response_id='late',item_id='late-a',delta=base64.b64encode(b'\1\0'*24).decode())
        self.assertFalse(any(e['type']=='audio' for e in self.events))
        self.assertTrue(any(e['type']=='response.cancel' for e in self.socket.sent))

    async def test_prompt_creation_race_keeps_guarded_readback_metadata(self):
        await self.prepare()
        request=self.socket.sent[-1]
        await self.event('response.created',response={'id':'automatic'})
        await self.event('error',error={'type':'invalid_request_error',
            'code':'conversation_already_has_active_response','event_id':request['event_id']})
        await self.finish('automatic')
        retry=self.socket.sent[-1]['response']
        self.assertEqual(retry['tool_choice'],'none')
        await self.prompt('readback')
        self.assertEqual(self.bridge.setup_gate.phase,'confirmation')

    async def test_output_transcript_can_arrive_after_playback_ack(self):
        await self.prepare()
        request=self.socket.sent[-1]['response']
        await self.event('response.created',response={'id':'readback','metadata':request['metadata']})
        await self.event('response.output_audio.delta',response_id='readback',item_id='a',
            delta=base64.b64encode(b'\1\0'*2400).decode())
        await self.event('response.output_audio.done',response_id='readback',item_id='a')
        await self.finish('readback')
        self.bridge.acknowledge({'item_id':'a','played_ms':100})
        self.assertIsNone(self.bridge.pending_prompt)
        self.assertEqual(self.bridge.setup_gate.phase,'readback_prompt')
        await self.event('response.output_audio_transcript.done',response_id='readback',transcript=self.bridge.setup_gate.readback)
        self.assertEqual(self.bridge.setup_gate.phase,'confirmation')

    async def test_missing_input_transcription_times_out_only_the_action(self):
        self.bridge.tool_wait_s=.01
        response=await self.owner('missing')
        await self.tool(response,'present_setup',self.candidate)
        self.assertEqual(self.bridge.setup_gate.phase,'collecting')
        self.assertFalse(any(event['type']=='voice_error' for event in self.events))
        result=json.loads(self.socket.sent[-1]['item']['output'])
        self.assertFalse(result['ok'])

    async def test_large_inventory_keeps_truthful_totals_and_identity_index(self):
        context={'totals':{'book_count':60,'books_replacement_cost':'195000.00','currency':'INR'},
            'processing':{'pricing_searching':20},
            'books':[{'id':f'b{i}','title':'A book '+str(i),'pricing_reason':'long explanation '*100,
                      'replacement_cost':{'amount':None,'exclusion_reason':'Unknown '*300}} for i in range(60)],
            'items':[{'id':f'i{i}','category':'Object','pricing_reason':'unidentified '*300} for i in range(20)]}
        self.assertGreater(len(json.dumps(context)),16000)
        output=bounded_voice_context(context)
        self.assertLessEqual(len(json.dumps(output,ensure_ascii=False)),16000)
        self.assertTrue(output['context_partial'])
        self.assertEqual(output['totals'],context['totals'])
        self.assertEqual(output['record_counts'],{'book_records':60,'item_records':20})
        self.assertIn({'id':'b59','label':'A book 59'},output['inventory_index'])
        self.bridge.setup_gate=None; self.runner.voice_context=lambda:context
        await self.bridge.owner_context(self.socket)
        self.assertEqual(self.socket.sent[-1]['item']['role'],'system')
