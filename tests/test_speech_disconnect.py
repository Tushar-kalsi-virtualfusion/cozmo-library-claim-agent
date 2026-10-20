"""Exercise voice cancellation through real TCP and the HTTP body middleware."""
import asyncio
import json
import socket
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn

from claim_agent.config import Settings
from claim_agent.claude_assistant import ClaudeAssistant
from claim_agent.cloud_speech import CloudSpeech
from claim_agent.server import create_app


class SpeechDisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_socket_close_cancels_provider_releases_lock_and_never_replays(self):
        await self.exercise_disconnect("speech")

    async def test_assistant_disconnect_releases_lock_and_persisted_receipt_blocks_replay(self):
        await self.exercise_disconnect("assistant")

    async def exercise_disconnect(self, kind):
        # Regression: Request.is_disconnected() polling inside BaseHTTPMiddleware
        # consumed the raw disconnect without cancelling the provider. The old
        # implementation held the speech lock until its full provider deadline.
        # Awaiting request.receive() in connected_speech must observe socket loss.
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                data_dir=Path(directory), pair_token="test-pair",
                provider="anthropic", vision_model="claude-sonnet-5-5",
                speech_provider="elevenlabs", elevenlabs_api_key="fake-test-key",
                speech_timeout_s=3, assistant_timeout_s=3,
            )
            app = create_app(settings)
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.setblocking(False)
            port = listener.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(
                app, log_level="critical", lifespan="off", timeout_graceful_shutdown=0,
            ))
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            target = None
            writer = None
            try:
                async def ready():
                    while not server.started:
                        if serving.done():
                            await serving
                            raise AssertionError("Loopback server stopped before readiness")
                        await asyncio.sleep(.01)

                await asyncio.wait_for(ready(), 3)
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}",
                    headers={"Authorization": "Bearer test-pair"},
                    trust_env=False,
                ) as client:
                    response = await client.post("/api/sweeps", json={
                        "device": "disconnect regression", "synthetic": True,
                        "country": "India", "currency": "INR", "capture_mode": "camera",
                    })
                    self.assertEqual(response.status_code, 200)
                    sweep_id = response.json()["sweep"]["id"]
                    target = app.state.runners[sweep_id]
                    admitted = asyncio.Event()
                    cancelled = asyncio.Event()
                    provider_calls = []

                    async def provider(request):
                        provider_calls.append(request)
                        admitted.set()
                        try:
                            await asyncio.Event().wait()
                        except asyncio.CancelledError:
                            cancelled.set()
                            raise

                    async def assistant_provider(prompt, schema, **options):
                        # Admission can be billable even if no response or token
                        # count was received; retain an explicitly unknown row.
                        target.provider.usage.append({
                            "provider": "anthropic", "model": options["model"],
                            "request_id": options["request_id"], "usage": None,
                        })
                        return await provider(options["request_id"])

                    if kind == "speech":
                        await target.speech.client.aclose()
                        target.speech.client = httpx.AsyncClient(
                            transport=httpx.MockTransport(provider), trust_env=False,
                        )
                    else:
                        target.provider.structured_text = assistant_provider
                    service = getattr(target, kind)
                    request_id = str(uuid4())
                    fields = {"request_id": request_id, "text": "Hello"}
                    if kind == "assistant":
                        fields.update(mode="reply", allow_actions=False)
                    body = json.dumps(fields).encode()
                    path = f"/api/sweeps/{sweep_id}/local-voice/{kind}"
                    _, writer = await asyncio.open_connection("127.0.0.1", port)
                    headers = (
                        f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                        "Authorization: Bearer test-pair\r\nContent-Type: application/json\r\n"
                        f"Content-Length: {len(body)}\r\n\r\n"
                    )
                    writer.write(headers.encode() + body)
                    await writer.drain()
                    await asyncio.wait_for(admitted.wait(), 2)
                    writer.close()
                    await writer.wait_closed()
                    writer = None

                    # This must happen well before the 3-second provider deadline.
                    await asyncio.wait_for(cancelled.wait(), .75)

                    async def unlocked():
                        while service.lock.locked():
                            await asyncio.sleep(.01)

                    await asyncio.wait_for(unlocked(), .5)
                    # Reconstruct the service using only the disk-backed receipt,
                    # rather than letting an in-memory cache suppress the replay.
                    restored = app.state.store.load(sweep_id)
                    metrics = restored.metrics
                    if kind == "speech":
                        self.assertEqual(metrics["speech_requests"][request_id]["status"], "interrupted")
                        self.assertIsNone(metrics["speech_usage"][0]["usage"])
                        await target.speech.close()
                        target.speech = CloudSpeech(target, settings, service.save, client=httpx.AsyncClient(
                            transport=httpx.MockTransport(provider), trust_env=False,
                        ))
                    else:
                        self.assertEqual(metrics["assistant_requests"][request_id]["status_code"], 503)
                        self.assertIsNone(metrics["provider_usage"][0]["usage"])
                        target.assistant = ClaudeAssistant(target, settings, service.save)
                    target.state = restored
                    self.assertEqual(len(provider_calls), 1)
                    retry = await client.post(path, content=body, headers={"Content-Type": "application/json"})
                    self.assertEqual(retry.status_code, 503)
                    self.assertEqual(len(provider_calls), 1, "An uncertain billable request must never be replayed")
            finally:
                if writer is not None:
                    writer.close()
                    await writer.wait_closed()
                server.should_exit = True
                try:
                    await asyncio.wait_for(serving, 3)
                finally:
                    if not serving.done():
                        serving.cancel()
                    await asyncio.gather(serving, return_exceptions=True)
                    listener.close()
                    if target is not None:
                        await target.close()
                        await target.speech.close()
                        await target.provider.close()
