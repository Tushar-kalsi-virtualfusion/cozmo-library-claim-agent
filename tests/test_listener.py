"""Exercise the real entry point: mDNS may select either IP address family."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import httpx


class ListenerTests(unittest.TestCase):
    def start_server(self, host):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('CLAIM_', 'ANTHROPIC_', 'OPENAI_', 'GEMINI_'))}
        env.update(CLAIM_HOST=host, CLAIM_PORT=str(port), CLAIM_PAIR_TOKEN='listener-test',
                   CLAIM_DATA_DIR=temporary.name, CLAIM_PROVIDER='anthropic',
                   ANTHROPIC_API_KEY='', ANTHROPIC_WORKSPACE_ID='')
        process = subprocess.Popen([sys.executable, '-m', 'claim_agent'], env=env,
                                   cwd=Path(__file__).resolve().parents[1],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def stop():
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=5)
        self.addCleanup(stop)
        self.client = httpx.Client(timeout=1, trust_env=False)
        self.addCleanup(self.client.close)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.assertIsNone(process.poll(), 'App server exited during startup')
            try:
                if self.client.get(f'http://127.0.0.1:{port}/api/health').status_code == 200:
                    return port
            except httpx.RequestError:
                pass
            time.sleep(.05)
        self.fail('App server did not become ready')

    @unittest.skipUnless(socket.has_dualstack_ipv6(), 'IPv6 dual stack unavailable')
    def test_lan_bind_serves_ipv4_and_ipv6_with_the_same_pairing_boundary(self):
        port = self.start_server('0.0.0.0')
        for host in ['127.0.0.1', '[::1]']:
            with self.subTest(host=host):
                base = f'http://{host}:{port}'
                try:
                    health = self.client.get(base + '/api/health')
                except httpx.RequestError:
                    self.fail(f'Configured LAN listener refused {host}; mDNS clients can choose this family')
                self.assertTrue(health.json()['ok'])
                self.assertEqual(self.client.post(base + '/api/onboarding', json={}).status_code, 401)
                paired = self.client.post(base + '/api/onboarding',
                                          json={'device': 'listener-check', 'capture_supported': False},
                                          headers={'Authorization': 'Bearer listener-test'})
                self.assertEqual(paired.status_code, 200)

    def test_explicit_loopback_bind_is_not_widened_to_ipv6(self):
        port = self.start_server('127.0.0.1')
        with self.assertRaises(httpx.ConnectError):
            self.client.get(f'http://[::1]:{port}/api/health')
