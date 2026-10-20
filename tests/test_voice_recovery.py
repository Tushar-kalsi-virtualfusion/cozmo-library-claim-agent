import asyncio
import base64
import json
import time
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

from claim_agent.live import LiveBridge
from claim_agent.openai_live import OpenAILiveBridge


class RecordingSocket:
    def __init__(self):
        self.sent = []

    async def send(self, value):
        self.sent.append(json.loads(value))


class VoiceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.runner = SimpleNamespace(
            publish=self.events.append,
            state=SimpleNamespace(sweep=SimpleNamespace(country="India", currency="INR"),
                                  events=[], metrics={}, review_reasons=[]))
        self.bridge = OpenAILiveBridge(self.runner, SimpleNamespace(client=True, key="test"), "model")
        self.socket = RecordingSocket()

    async def audio(self, response="r1", item="a1"):
        await self.bridge.handle_event(self.socket, {
            "type": "response.output_audio.delta", "response_id": response,
            "item_id": item, "content_index": 0, "delta": base64.b64encode(bytes(4800)).decode()})

    async def recover(self, request, code, error_type="invalid_request_error"):
        try:
            await self.bridge.handle_event(self.socket, {
                "type": "error", "error": {"type": error_type, "code": code,
                                          "event_id": request.get("event_id", "untracked")}})
        except RuntimeError as error:
            self.fail(f"A recoverable control request stopped the voice connection: {error}")

    async def test_truncation_race_does_not_prevent_the_next_spoken_response(self):
        await self.audio()
        self.bridge.acknowledge({"item_id": "a1", "played_ms": 40})
        await self.bridge.handle_event(self.socket, {"type": "input_audio_buffer.speech_started"})
        truncate = self.socket.sent[-1]
        await self.recover(truncate, "invalid_value")
        await self.bridge.handle_event(self.socket, {"type": "response.done", "response": {"id": "r1", "status": "cancelled"}})
        await self.bridge.handle_event(self.socket, {"type": "input_audio_buffer.speech_stopped"})
        await self.bridge.handle_event(self.socket, {"type": "response.created", "response": {"id": "r2"}})
        await self.audio("r2", "a2")
        self.assertEqual([e["item_id"] for e in self.events if e["type"] == "audio"], ["a1", "a2"])
        self.assertFalse(any(e["type"] == "voice_error" for e in self.events))

    async def test_real_provider_truncation_error_without_event_id_recovers_only_matching_recent_request(self):
        # Observed from the provider: this particular validation error omits event_id.
        await self.bridge.send_control(self.socket, {"type":"conversation.item.truncate",
            "item_id":"a1", "content_index":0, "audio_end_ms":1500})
        event={"type":"error", "error":{"type":"invalid_request_error", "code":"invalid_value",
            "message":"Audio content of 1300ms is already shorter than 1500ms", "param":None, "event_id":None}}
        try:
            await self.bridge.handle_event(self.socket,event)
        except RuntimeError as error:
            self.fail(f"The observed provider truncation race closed voice: {error}")
        self.assertFalse(any(e["type"]=="voice_error" for e in self.events))
        # An identical error with no matching request is not silently swallowed.
        with self.assertRaises(RuntimeError):
            await self.bridge.handle_event(self.socket,event)

    async def test_uncorrelated_errors_do_not_match_expired_or_different_truncations(self):
        await self.bridge.send_control(self.socket, {"type":"conversation.item.truncate",
            "item_id":"a1", "content_index":0, "audio_end_ms":1500})
        for message in ["Invalid session setting", "Audio content of 1300ms is already shorter than 2000ms"]:
            with self.assertRaises(RuntimeError):
                await self.bridge.handle_event(self.socket,{"type":"error","error":{
                    "type":"invalid_request_error","code":"invalid_value","message":message}})
        with patch('claim_agent.openai_live.time.monotonic',return_value=time.monotonic()+20):
            with self.assertRaises(RuntimeError):
                await self.bridge.handle_event(self.socket,{"type":"error","error":{
                    "type":"invalid_request_error","code":"invalid_value",
                    "message":"Audio content of 1300ms is already shorter than 1500ms"}})

    async def test_fully_played_audio_is_not_truncated_on_the_next_user_turn(self):
        await self.audio()
        self.bridge.acknowledge({"item_id": "a1", "played_ms": 100})
        await self.bridge.handle_event(self.socket, {"type": "response.done", "response": {"id": "r1", "status": "completed"}})
        await self.bridge.handle_event(self.socket, {"type": "input_audio_buffer.speech_started"})
        self.assertFalse(any(e["type"] == "conversation.item.truncate" for e in self.socket.sent))

    async def test_response_creation_race_preserves_pending_summary_until_current_turn_finishes(self):
        self.bridge.pending_instruction = "Read the final server summary."
        self.bridge.final_summary_pending = True
        await self.bridge.maybe_respond(self.socket)
        request = self.socket.sent[-1]
        await self.bridge.handle_event(self.socket, {"type": "response.created", "response": {"id": "automatic"}})
        await self.recover(request, "conversation_already_has_active_response")
        self.assertTrue(self.bridge.final_summary_pending)
        await self.bridge.handle_event(self.socket, {"type": "response.done", "response": {"id": "automatic", "status": "completed"}})
        self.assertEqual(len([e for e in self.socket.sent if e["type"] == "response.create"]), 2)
        self.assertIn("Read the final server summary.", self.socket.sent[-1]["response"]["instructions"])

    async def test_unrelated_and_billing_errors_still_fail_explicitly(self):
        await self.audio()
        await self.bridge.handle_event(self.socket, {"type": "input_audio_buffer.speech_started"})
        event_id = self.socket.sent[-1].get("event_id", "untracked")
        for code, reference in [("invalid_value", "unrelated"), ("credit_balance_exhausted", event_id)]:
            with self.subTest(code=code), self.assertRaises(RuntimeError):
                await self.bridge.handle_event(self.socket, {"type": "error", "error": {
                    "type": "invalid_request_error", "code": code, "event_id": reference}})

    async def test_failed_response_is_reported_instead_of_silently_waiting_forever(self):
        with self.assertRaises(RuntimeError):
            await self.bridge.handle_event(self.socket, {"type": "response.done", "response": {
                "id": "r1", "status": "failed", "status_details": {"error": {"code": "server_error"}}}})

    def test_audio_backpressure_drops_chunks_without_closing_voice(self):
        bridge = LiveBridge(self.runner, None, "model")
        for _ in range(bridge.outgoing.maxsize + 10):
            bridge.offer(("audio", b"\0\0"))
        self.assertFalse(any(e["type"] == "voice_error" for e in self.events))
        self.assertEqual(bridge.outgoing.qsize(), 64)
        self.assertEqual(self.runner.state.metrics.get("live_audio_chunks_dropped"), 10)

    async def test_session_responds_automatically_without_paid_idle_loops(self):
        sent = []
        greeted = asyncio.Event()

        class Socket:
            async def send(self, value):
                event = json.loads(value)
                sent.append(event)
                if event["type"] == "response.create":
                    greeted.set()

            async def recv(self):
                return '{"type":"session.updated"}'

            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.Future()

        @asynccontextmanager
        async def connection(*args, **kwargs):
            yield Socket()

        with patch("claim_agent.openai_live.connect", connection):
            task = asyncio.create_task(self.bridge.run())
            try:
                await asyncio.wait_for(greeted.wait(), 1)
                config = sent[0]["session"]["audio"]["input"]
                self.assertIsNotNone(config.get("noise_reduction"))
                vad = config["turn_detection"]
                self.assertEqual(vad['type'], 'semantic_vad')
                self.assertEqual(vad['eagerness'], 'high')
                self.assertTrue(vad["interrupt_response"])
                self.assertTrue(vad["create_response"])
                self.assertNotIn('idle_timeout_ms', vad)
                self.assertEqual(sent[0]['session']['reasoning'], {'effort':'low'})
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
