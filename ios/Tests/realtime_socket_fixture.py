"""Local-only socket fixture: no app server or external provider is started."""
import asyncio
import json
import sys
from websockets.asyncio.server import serve

async def connection(socket):
    assert socket.request.headers['Authorization'] == 'Bearer realtime-test'
    async for raw in socket:
        value = json.loads(raw)
        assert value['type'] != 'old_audio', 'An old generation reached the replacement socket'
        reply = {'type': 'stopped' if value['type'] == 'stop_voice' else 'echo', 'path': socket.request.path}
        await socket.send(json.dumps(reply))

async def main():
    async with serve(connection, '127.0.0.1', 0) as server:
        port = server.sockets[0].getsockname()[1]
        process = await asyncio.create_subprocess_exec(sys.argv[1], f'http://127.0.0.1:{port}')
        try:
            async with asyncio.timeout(10):
                code = await process.wait()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        if code:
            raise SystemExit(code)

if __name__ == '__main__':
    asyncio.run(main())
