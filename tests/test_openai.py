import asyncio
import base64
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

from claim_agent.config import Settings
from claim_agent.openai_provider import OpenAIProvider
from claim_agent.openai_live import OpenAILiveBridge, PCMResampler
from claim_agent.providers import ProviderUnavailable
from claim_agent.vision import Scan, VisionProvider
from tests.test_geometry import frame


def response(output, status="completed"):
    return {"id":"resp_test","object":"response","created_at":1,"status":status,
            "model":"gpt-5.4-mini","output":output,
            "usage":{"input_tokens":20,"output_tokens":8,"total_tokens":28}}


class OpenAITests(unittest.IsolatedAsyncioTestCase):
    async def provider(self, body, status=200):
        self.requests=[]
        async def respond(request):
            self.requests.append(json.loads(request.content))
            return httpx.Response(status,json=body)
        client=AsyncOpenAI(api_key="unit-test",max_retries=0,
                           http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        provider=OpenAIProvider(client=client)
        self.addAsyncCleanup(provider.close)
        return provider

    async def test_image_extraction_uses_real_sdk_schema_and_records_usage(self):
        body=response([{"type":"message","id":"msg","role":"assistant","status":"completed",
                        "content":[{"type":"output_text","text":Scan(guidance="Move slowly").model_dump_json(),"annotations":[]}]}])
        provider=await self.provider(body)
        scan=await VisionProvider(provider).inspect(b"jpeg",frame())
        self.assertEqual(scan.guidance,"Move slowly")
        request=self.requests[0]
        self.assertEqual(request["input"][0]["content"][1]["image_url"],"data:image/jpeg;base64,anBlZw==")
        self.assertTrue(request["text"]["format"]["strict"])
        self.assertFalse(request["store"])
        self.assertEqual(request["input"][0]["content"][1]["detail"],"original")
        self.assertEqual(provider.usage[0]["usage"]["input_tokens"],20)

    async def test_search_uses_retrieval_sources_not_model_link_text(self):
        body=response([{"type":"web_search_call","id":"ws","status":"completed","action":{
            "type":"search","query":"book","sources":[{"type":"url","url":"https://shop.example/book"}]}},
            {"type":"message","id":"msg","role":"assistant","status":"completed","content":[{
                "type":"output_text","text":"Invented https://fake.example/price 1234","annotations":[]}]}])
        provider=await self.provider(body)
        self.assertEqual(await provider.search_urls("book in India"),["https://shop.example/book"])
        self.assertEqual(self.requests[0]["tools"][0]["type"],"web_search")

    async def test_missing_key_budget_and_quota_fail_explicitly(self):
        with self.assertRaisesRegex(ProviderUnavailable,"OPENAI_API_KEY"):
            await OpenAIProvider().search_urls("book")
        provider=await self.provider({"error":{"message":"private server diagnostic", "type":"insufficient_quota", "code":"credit_balance_exhausted"}},429)
        with self.assertRaisesRegex(ProviderUnavailable,"credit_balance_exhausted") as raised:
            await provider.search_urls("book")
        self.assertNotIn("private server diagnostic",str(raised.exception))
        with self.assertRaisesRegex(ProviderUnavailable,"credit_balance_exhausted"):
            await provider.search_urls("book")
        self.assertEqual(len(self.requests),1)
        provider=await self.provider(response([]))
        provider.max_calls=0
        with self.assertRaisesRegex(ProviderUnavailable,"budget"):
            await provider.search_urls("book")

    def test_openai_selection_does_not_reuse_google_key_or_models(self):
        with patch.dict(os.environ,{"CLAIM_PROVIDER":"openai","OPENAI_API_KEY":"test-openai","GOOGLE_API_KEY":"test-google"},clear=True), patch("claim_agent.config.load_dotenv"):
            settings=Settings.from_env()
        self.assertEqual(settings.provider,"openai")
        self.assertEqual(settings.api_key,"test-openai")
        self.assertEqual(settings.vision_model,"gpt-6-astra")
        self.assertEqual(settings.live_model,"gpt-realtime-2.1")

    async def test_precision_vision_and_search_keep_separate_models_and_usage(self):
        provider=await self.provider(response([]))
        provider.model="gpt-6-astra"
        await provider.request(input="test", max_output_tokens=20)
        await provider.search_urls("router India")
        self.assertEqual([r["model"] for r in self.requests],["gpt-6-astra","gpt-5.4-mini"])
        self.assertEqual([r["model"] for r in provider.usage],["gpt-6-astra","gpt-5.4-mini"])

    def test_resampling_keeps_chunk_boundaries_and_duration(self):
        import numpy as np
        pcm=(np.sin(np.arange(1600)*.11)*20000).astype('<i2').tobytes()
        full=PCMResampler().convert(pcm)
        chunked=PCMResampler()
        parts=b''.join(chunked.convert(pcm[i:i+320]) for i in range(0,len(pcm),320))
        self.assertEqual(parts,full)
        self.assertLessEqual(abs(len(full)-4800),2)

    async def test_interruption_clears_audio_and_truncates_to_acknowledged_playback(self):
        events=[]; sent=[]
        runner=SimpleNamespace(publish=events.append,state=SimpleNamespace(sweep=SimpleNamespace(country="India",currency="INR"),events=[],metrics={},review_reasons=[]))
        bridge=OpenAILiveBridge(runner,SimpleNamespace(client=True,key="test"),"model")
        class Socket:
            async def send(self,value): sent.append(json.loads(value))
        socket=Socket()
        audio={"type":"response.output_audio.delta","response_id":"r1","item_id":"a1","content_index":0,
               "delta":base64.b64encode(bytes(4800)).decode()}
        await bridge.handle_event(socket,audio)
        bridge.acknowledge({"item_id":"a1","content_index":0,"played_ms":40})
        await bridge.handle_event(socket,{"type":"input_audio_buffer.speech_started"})
        await bridge.handle_event(socket,audio)
        self.assertEqual([e["type"] for e in events],["audio","interrupted"])
        self.assertTrue(sent[-1].get("event_id"))
        self.assertEqual({k:v for k,v in sent[-1].items() if k!="event_id"},
                         {"type":"conversation.item.truncate","item_id":"a1","content_index":0,"audio_end_ms":40})

    async def test_live_correction_reaches_revision_checked_pipeline(self):
        events=[]; sent=[]; corrections=[]
        async def correct(args): corrections.append(args)
        runner=SimpleNamespace(publish=events.append,state=SimpleNamespace(sweep=SimpleNamespace(country="India",currency="INR"),events=[],metrics={},review_reasons=[]),
                               correct=correct,voice_context=lambda:{"books":[]})
        bridge=OpenAILiveBridge(runner,SimpleNamespace(client=True,key="test"),"model")
        class Socket:
            async def send(self,value): sent.append(json.loads(value))
        socket=Socket()
        await bridge.handle_event(socket, {'type':'input_audio_buffer.speech_started','item_id':'owner1'})
        await bridge.handle_event(socket, {'type':'input_audio_buffer.speech_stopped','item_id':'owner1'})
        await bridge.handle_event(socket, {'type':'input_audio_buffer.committed','item_id':'owner1'})
        await bridge.handle_event(socket, {'type':'response.created','response':{'id':'r1'}})
        await bridge.handle_event(socket, {'type':'conversation.item.input_audio_transcription.completed','item_id':'owner1','transcript':'That book is not mine.'})
        await bridge.handle_event(socket,{"type":"response.function_call_arguments.done","name":"correct_inventory","response_id":"r1",
            "call_id":"c1","arguments":json.dumps({"action":"exclude","ref_id":"b1","value":"not mine"})})
        await asyncio.gather(*bridge.tool_tasks)
        await bridge.handle_event(socket, {'type':'response.done','response':{'id':'r1','status':'completed'}})
        self.assertEqual(corrections[0]["ref_id"],"b1")
        self.assertTrue(any(e.get('item',{}).get('call_id')=='c1' for e in sent))
        self.assertEqual(sent[-1]["type"],"response.create")

    async def test_interrupted_tool_call_cannot_change_inventory(self):
        events=[]; corrections=[]
        async def correct(args): corrections.append(args)
        runner=SimpleNamespace(publish=events.append,state=SimpleNamespace(sweep=SimpleNamespace(country="India",currency="INR"),events=[],metrics={},review_reasons=[]),
                               correct=correct,voice_context=lambda:{})
        bridge=OpenAILiveBridge(runner,SimpleNamespace(client=True,key="test"),"model")
        class Socket:
            async def send(self,value): pass
        socket=Socket()
        await bridge.handle_event(socket,{"type":"response.created","response":{"id":"r1"}})
        await bridge.handle_event(socket,{"type":"input_audio_buffer.speech_started"})
        await bridge.handle_event(socket,{"type":"response.function_call_arguments.done","response_id":"r1",
            "name":"correct_inventory","call_id":"c1","arguments":'{"action":"exclude","ref_id":"b1","value":"not mine"}'})
        self.assertEqual(corrections,[])

    async def test_final_summary_waits_for_active_response_then_speaks(self):
        sent=[]
        runner=SimpleNamespace(publish=lambda e:None,state=SimpleNamespace(sweep=SimpleNamespace(country="India",currency="INR"),events=[],metrics={},review_reasons=[]))
        bridge=OpenAILiveBridge(runner,SimpleNamespace(client=True,key="test"),"model")
        class Socket:
            async def send(self,value): sent.append(json.loads(value))
        socket=Socket()
        await bridge.handle_event(socket,{"type":"response.created","response":{"id":"r1"}})
        await bridge.send_context(socket,json.dumps({"capture_ended":True,"summary":{"book_count":3}}))
        self.assertEqual([e["type"] for e in sent],["conversation.item.create"])
        await bridge.handle_event(socket,{"type":"response.done","response":{"id":"r1"}})
        self.assertEqual(sent[-1]["type"],"response.create")
        self.assertIn("summary",sent[-1]["response"]["instructions"])
        self.assertIn("Do not infer ownership exclusions",sent[-1]["response"]["instructions"])
