"""Runs native local voice wire checks without opening a paid provider connection."""
import asyncio
import json
import sys
from websockets.asyncio.server import serve

async def main():
    completed = asyncio.Event()
    failures = []
    async def handler(ws):
        try:
            # No start_voice, audio, or other messages may be sent by local subscription.
            try:
                message = await asyncio.wait_for(ws.recv(), 0.3)
                failures.append(f"Unexpected client message: {message}")
            except TimeoutError:
                pass
            for kind in ["frame_overlays", "inventory"]:
                await ws.send(json.dumps({"type": kind}))
            await ws.wait_closed()
        finally:
            completed.set()
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        child = await asyncio.create_subprocess_exec(sys.argv[1], f"http://127.0.0.1:{port}")
        code = await asyncio.wait_for(child.wait(), 15)
        await asyncio.wait_for(completed.wait(), 5)
        assert code == 0 and not failures, failures

asyncio.run(main())
