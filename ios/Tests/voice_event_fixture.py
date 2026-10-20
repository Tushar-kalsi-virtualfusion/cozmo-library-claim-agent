"""Local protocol fixture for VoiceEventChecks; no provider or room data."""
import asyncio
import json
from pathlib import Path
from websockets.asyncio.server import serve


async def main():
    completed = asyncio.Event()

    async def handler(ws):
        assert json.loads(await ws.recv())["type"] == "start_voice"
        await ws.send(json.dumps({"type": "voice_ready"}))
        assert json.loads(await ws.recv())["type"] == "stop_voice"
        for kind in ["voice_stopped", "frame_overlays", "inventory"]:
            await ws.send(json.dumps({"type": kind}))
        await ws.wait_closed()
        completed.set()

    async with serve(handler, "127.0.0.1", 0) as server:
        Path("/tmp/library-voice-fixture-port").write_text(str(server.sockets[0].getsockname()[1]))
        await asyncio.wait_for(completed.wait(), 45)


asyncio.run(main())
