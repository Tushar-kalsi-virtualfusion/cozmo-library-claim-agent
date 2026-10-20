import os
import socket
import uvicorn

from .config import Settings
from .server import create_app

settings=Settings.from_env()
host=os.getenv("CLAIM_HOST","127.0.0.1")
port=int(os.getenv("CLAIM_PORT","4180"))
print(f"Library Claim: http://{host}:{port}",flush=True)
print(f"Temporary pairing code: {settings.pair_token}",flush=True)
print(f"Model provider: {settings.provider}; key configured: {bool(settings.api_key)} (billing/model access requires a live request)",flush=True)
app=create_app(settings)
options=dict(host=host,port=port,proxy_headers=False,access_log=False,ws_max_size=131072)
if host == "0.0.0.0" and socket.has_dualstack_ipv6():
    # Bonjour advertises both address families. asyncio's host-based bind makes
    # IPv6 sockets v6-only, so pass an explicitly dual-stack listening socket.
    with socket.create_server(("::",port),family=socket.AF_INET6,dualstack_ipv6=True) as listener:
        print("Local network listener: IPv4 and IPv6",flush=True)
        uvicorn.Server(uvicorn.Config(app,**options)).run(sockets=[listener])
else:
    # Preserve explicit/loopback binds and platforms without IPv6 support.
    uvicorn.run(app,**options)
