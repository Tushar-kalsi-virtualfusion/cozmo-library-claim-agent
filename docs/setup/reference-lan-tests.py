import os
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient
from live_demo.server import app


class LanPreviewTests(unittest.TestCase):
    def test_phone_on_configured_subnet_can_load_page(self):
        with patch.dict(os.environ, {'FNOL_LAN_HOST': '192.168.1.101', 'FNOL_LAN_NETWORK': '192.168.1.0/24'}):
            with TestClient(app, base_url='http://192.168.1.101:4177', client=('192.168.1.50', 50000)) as client:
                self.assertEqual(client.get('/').status_code, 200)

    def test_unconfigured_lan_is_denied(self):
        with patch.dict(os.environ, {'FNOL_LAN_HOST': '', 'FNOL_LAN_NETWORK': ''}):
            with TestClient(app, base_url='http://192.168.1.101:4177', client=('192.168.1.50', 50000)) as client:
                self.assertEqual(client.get('/').status_code, 403)

    def test_outside_subnet_and_foreign_host_are_denied(self):
        with patch.dict(os.environ, {'FNOL_LAN_HOST': '192.168.1.101', 'FNOL_LAN_NETWORK': '192.168.1.0/24'}):
            for host, peer in [('192.168.1.101','192.168.2.50'), ('192.168.1.101','8.8.8.8'), ('foreign.example','192.168.1.50')]:
                with self.subTest(host=host,peer=peer), TestClient(app, base_url=f'http://{host}:4177',client=(peer,50000)) as client:
                    self.assertEqual(client.get('/').status_code,403)

    def test_lan_access_preserves_origin_protection(self):
        with patch.dict(os.environ, {'FNOL_LAN_HOST': '192.168.1.101', 'FNOL_LAN_NETWORK': '192.168.1.0/24'}):
            with TestClient(app,base_url='http://192.168.1.101:4177',client=('192.168.1.50',50000)) as client:
                self.assertEqual(client.post('/api/sessions',headers={'Origin':'https://foreign.example'}).status_code,403)
                self.assertEqual(client.post('/api/sessions',headers={'Origin':'http://192.168.1.101:4177'}).status_code,200)

    def test_invalid_or_public_lan_configuration_is_denied(self):
        for host, network in [('8.8.8.8','8.8.8.0/24'),('192.168.1.101','invalid')]:
            with self.subTest(host=host), patch.dict(os.environ,{'FNOL_LAN_HOST':host,'FNOL_LAN_NETWORK':network}):
                with TestClient(app,base_url=f'http://{host}:4177',client=(host,50000)) as client:
                    self.assertEqual(client.get('/').status_code,403)
