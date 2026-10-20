import base64
import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from claim_agent.server import create_app
from claim_agent.config import Settings
from tests.test_geometry import frame


def upload_payload():
    out = io.BytesIO()
    Image.new("RGB", (1000,1000), color=(80,120,150)).save(out, format="JPEG")
    return {"metadata": frame().model_dump(), "jpeg_base64":base64.b64encode(out.getvalue()).decode(),
            "depth_base64":"", "confidence_base64":""}


class APITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = self.enterContext(TestClient(create_app(Settings(data_dir=Path(self.tmp.name),pair_token="test-token"))))
        self.headers = {"Authorization":"Bearer test-token"}

    def create(self):
        r = self.client.post("/api/sweeps", headers=self.headers,
                             json={"device":"test", "country":"India", "currency":"INR", "synthetic":True})
        self.assertEqual(r.status_code,200,r.text)
        return r.json()["sweep"]["id"]

    def test_pairing_required_and_health_does_not_leak_token(self):
        self.assertEqual(self.client.post("/api/sweeps",json={}).status_code,401)
        health = self.client.get("/api/health")
        self.assertNotIn("test-token",health.text)
        self.assertFalse(health.json()["model_ready"])

    def test_each_sweep_owns_its_provider_usage_and_budget(self):
        ids=[self.create(),self.create()]
        runners=self.client.app.state.runners
        first,second=[runners[id].provider for id in ids]
        self.assertIsNot(first,second)
        first.usage.append({"marker":"first only"})
        first.calls=3
        self.assertEqual(second.usage,[])
        self.assertEqual(second.calls,0)

    def test_queue_pressure_is_http_retry_for_older_phone_builds(self):
        from types import SimpleNamespace
        id = self.create()
        runner = self.client.app.state.runners[id]
        runner.queue = SimpleNamespace(full=lambda:True)
        response = self.client.post(f"/api/sweeps/{id}/frames",headers=self.headers,json=upload_payload())
        self.assertEqual(response.status_code,429)
        self.assertEqual(response.headers["retry-after"],"3")
        self.assertFalse(runner.state.frames)

    def test_no_key_session_exports_honest_partial_packet_and_exact_saved_frame(self):
        id = self.create()
        payload = upload_payload()
        r = self.client.post(f"/api/sweeps/{id}/frames",headers=self.headers,json=payload)
        self.assertEqual(r.status_code,200,r.text)
        retry = self.client.post(f"/api/sweeps/{id}/frames",headers=self.headers,json=payload)
        self.assertEqual(retry.status_code,200,retry.text)
        self.assertTrue(retry.json()["duplicate"])
        final = self.client.post(f"/api/sweeps/{id}/finish",headers=self.headers,json={"duration_s":20})
        self.assertEqual(final.status_code,200,final.text)
        p = final.json()
        self.assertEqual(p["completion"],"partial")
        self.assertTrue(any("API_KEY" in r["reason"] for r in p["review_queue"]))
        z = self.client.get(f"/api/sweeps/{id}/packet.zip",headers=self.headers)
        self.assertEqual(z.status_code,200)
        with zipfile.ZipFile(io.BytesIO(z.content)) as archive:
            self.assertEqual(archive.read("frames/f1.jpg"),base64.b64decode(payload["jpeg_base64"]))
        payload["metadata"]["id"] = "f2"
        self.assertEqual(self.client.post(f"/api/sweeps/{id}/frames",headers=self.headers,json=payload).status_code,409)

    def test_session_mismatch_and_malformed_depth_rejected(self):
        id=self.create()
        payload=upload_payload()
        self.assertEqual(self.client.post(f"/api/sweeps/{id}/frames",headers=self.headers,json=payload).status_code,200)
        payload["metadata"]["id"]="f2"
        payload["metadata"]["ar_session_id"]="different-session"
        self.assertEqual(self.client.post(f"/api/sweeps/{id}/frames",headers=self.headers,json=payload).status_code,422)

    def test_retry_cannot_add_depth_to_an_already_saved_frame(self):
        id=self.create()
        payload=upload_payload()
        self.assertEqual(self.client.post(f"/api/sweeps/{id}/frames",headers=self.headers,json=payload).status_code,200)
        payload["depth_base64"]=base64.b64encode(bytes(100*100*4)).decode()
        payload["confidence_base64"]=base64.b64encode(bytes(100*100)).decode()
        self.assertEqual(self.client.post(f"/api/sweeps/{id}/frames",headers=self.headers,json=payload).status_code,422)
        payload["metadata"]["ar_session_id"]="one-session"
        payload["depth_base64"]=base64.b64encode(b"short").decode()
        self.assertEqual(self.client.post(f"/api/sweeps/{id}/frames",headers=self.headers,json=payload).status_code,422)
