import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image

from claim_agent.config import Settings
from claim_agent.models import FrameMetadata
from claim_agent.pipeline import SweepRunner
from claim_agent.storage import EvidenceStore
from claim_agent.costing import usage_cost
from claim_agent.image_selection import duplicate_of, fingerprint
from claim_agent.vision import ProposedScan, DetectionFields, Scan, Detection, accept_proposals, raw_point, upright_point


def jpeg(seed=1, quality=90):
    image = Image.fromarray(np.random.default_rng(seed).integers(0, 255, (100, 100, 3), dtype=np.uint8))
    out = io.BytesIO(); image.save(out, format="JPEG", quality=quality)
    return out.getvalue()


def meta(id="f1", x=0):
    return FrameMetadata(id=id, timestamp_s=1, image_width=100, image_height=100,
        intrinsics=[100,0,0,0,100,0,50,50,1], camera_transform=[1,0,0,0,0,1,0,0,0,0,1,0,x,0,0,1],
        ar_session_id="test", tracking="normal")


class HybridUploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = EvidenceStore(Path(self.tmp.name))
        self.state = self.store.create("test", "India", "INR", synthetic=True)
        self.runner = SweepRunner(self.state, self.store, Settings(queue_size=1), None, None)

    async def test_same_photo_new_id_shares_bytes_and_is_not_recognized_twice(self):
        photo = jpeg()
        await self.runner.upload(meta(), photo, b"", b"")
        response = await self.runner.upload(meta("f2"), photo, b"", b"")
        self.assertTrue(response["duplicate"])
        self.assertEqual(response["duplicate_of"], "f1")
        self.assertEqual(self.runner.queue.qsize(), 1)
        photos = list(self.store.directory(self.state.sweep.id).glob("frames/*.jpg"))
        self.assertEqual(len(photos), 2)  # Independent frame provenance, shared JPEG bytes.
        self.assertEqual(len({file.stat().st_ino for file in photos}), 1)
        reloaded = SweepRunner(self.store.load(self.state.sweep.id), self.store, Settings(), None, None)
        self.assertTrue((await reloaded.upload(meta("f3"), photo, b"", b""))["duplicate"])
        with self.assertRaises(ValueError):
            await self.runner.upload(meta("f2"), jpeg(3), b"", b"")

    async def test_full_queue_does_not_acknowledge_unprocessed_frame(self):
        await self.runner.upload(meta(), jpeg(), b"", b"")
        response = await self.runner.upload(meta("f2", x=1), jpeg(2), b"", b"")
        self.assertFalse(response["accepted"])
        self.assertGreater(response["retry_after_s"], 0)
        self.assertNotIn("f2", self.state.frames)
        self.runner.queue.get_nowait(); self.runner.queue.task_done()
        self.assertTrue((await self.runner.upload(meta("f2", x=1), jpeg(2), b"", b""))["accepted"])

    async def test_duplicate_retry_cannot_change_calibration(self):
        photo = jpeg(); await self.runner.upload(meta(), photo, b"", b"")
        with self.assertRaises(ValueError):
            await self.runner.upload(meta(x=1), photo, b"", b"")

    async def test_duplicate_rgb_retains_new_depth_and_refines_without_model_call(self):
        self.runner.settings.local_first = False  # Exercise explicit legacy cloud recognition.
        calls = []
        async def inspect(photo, metadata):
            calls.append(metadata.id)
            return Scan(detections=[Detection(kind="book",polygon=[[.1,.1],[.2,.1],[.2,.4],[.1,.4]])])
        self.runner.vision = SimpleNamespace(provider=SimpleNamespace(usage=[]),inspect=inspect)
        self.runner.start()
        self.addAsyncCleanup(self.runner.close)
        photo = jpeg()
        await self.runner.upload(meta(),photo,b"",b"")
        await asyncio.wait_for(self.runner.queue.join(),2)
        self.assertIsNone(self.state.books[0].spine_height_cm)
        second = meta("f2").model_copy(update={"depth_width":100,"depth_height":100,"timestamp_s":2})
        depth = np.full((100,100),2,dtype="<f4").tobytes()
        response = await self.runner.upload(second,photo,depth,bytes([0])*10000)
        self.assertEqual(response["processing"],"queued_geometry")
        await asyncio.wait_for(self.runner.queue.join(),2)
        self.assertIsNone(self.state.books[0].spine_height_cm)  # Low confidence cannot invent a measurement.
        third = second.model_copy(update={"id":"f3","timestamp_s":3})
        await self.runner.upload(third,photo,depth,bytes([2])*10000)
        await asyncio.wait_for(self.runner.queue.join(),2)
        self.assertEqual(calls,["f1"])
        self.assertEqual(len(self.state.books),1)
        self.assertEqual(self.state.frame_status["f3"],"duplicate_geometry_processed")
        self.assertAlmostEqual(self.state.books[0].spine_height_cm,60)
        self.assertEqual(self.state.books[0].measurement["frame_id"],"f3")
        self.assertIn("depth/f3.bin",self.state.books[0].evidence_refs)
        overlay = json.loads(self.store.read_evidence(self.state,"observations/overlays_f3.json"))
        self.assertEqual(overlay["timestamp_s"],3)
        self.assertAlmostEqual(overlay["detections"][0]["dimensions"]["spine_length_cm"],60)
        photos = list(self.store.directory(self.state.sweep.id).glob("frames/*.jpg"))
        self.assertEqual(len(photos),3)
        self.assertEqual(len({file.stat().st_ino for file in photos}),1)


class CostTests(unittest.TestCase):
    def test_astra_rates_include_cache_writes_and_long_context(self):
        row={"model":"gpt-6-astra","usage":{"input_tokens":1000,"output_tokens":100,
             "input_tokens_details":{"cached_tokens":200,"cache_write_tokens":300}}}
        self.assertAlmostEqual(usage_cost([row])["cost_usd"],.01395)
        row["usage"]["input_tokens"]=300000
        self.assertAlmostEqual(usage_cost([row])["cost_usd"],6.0054)

    def test_responses_cache_and_search_fees(self):
        row = {"model":"gpt-5.4-mini", "response_id":"r1", "web_search_calls":2,
               "usage":{"input_tokens":1000,"input_tokens_details":{"cached_tokens":200},"output_tokens":100}}
        result = usage_cost([row, row])
        self.assertAlmostEqual(result["cost_usd"], .021065)
        self.assertEqual(result["cost_status"], "estimated")
        self.assertEqual(result["costed_requests"], 1)

    def test_realtime_modalities_and_cached_tokens(self):
        row = {"model":"gpt-realtime-2.1","usage":{"input_tokens":1600,"output_tokens":120,
            "input_token_details":{"text_tokens":1000,"audio_tokens":500,"image_tokens":100,
                "cached_tokens":400,"cached_tokens_details":{"text_tokens":200,"audio_tokens":200,"image_tokens":0}},
            "output_token_details":{"text_tokens":20,"audio_tokens":100}}}
        self.assertAlmostEqual(usage_cost([row])["cost_usd"], .02034)

    def test_unknown_usage_is_partial_not_free(self):
        result = usage_cost([{"model":"gpt-5.4-mini","usage":None}])
        self.assertIsNone(result["cost_usd"])
        self.assertEqual(result["cost_status"], "unavailable")
        self.assertEqual(result["uncosted_requests"], 1)


class ImageSelectionTests(unittest.TestCase):
    def test_near_duplicates_require_same_pose_and_no_clarity_improvement(self):
        old = fingerprint(jpeg())
        changed = {**old,"sha256":"different bytes"}
        self.assertEqual(duplicate_of(changed, meta("f2"), {"f1":old}, {"f1":meta()}), "f1")
        self.assertIsNone(duplicate_of(changed, meta("f2",x=1), {"f1":old}, {"f1":meta()}))
        changed["sharpness"] = old["sharpness"]*1.4
        self.assertIsNone(duplicate_of(changed, meta("f2"), {"f1":old}, {"f1":meta()}))

    def test_one_bad_polygon_does_not_discard_valid_objects(self):
        bad = DetectionFields(kind="item",polygon=[[0,0],[0,0],[0,0],[0,0]])
        good = DetectionFields(kind="item",category="chair",polygon=[[.1,.2],[.8,.2],[.8,.9],[.1,.9]])
        result = accept_proposals(ProposedScan(detections=[bad,good]),90)
        self.assertEqual(len(result.detections),1)
        self.assertEqual(result.detections[0].polygon[0],[.2,.9])
        self.assertTrue(result.quality_issues)

    def test_rotation_round_trip_preserves_calibration_coordinates(self):
        for rotation in (0,90,180,270):
            point = raw_point(upright_point([.2,.3],rotation),rotation)
            self.assertAlmostEqual(point[0],.2); self.assertAlmostEqual(point[1],.3)

    def test_missing_realtime_split_is_not_guessed(self):
        result = usage_cost([{"model":"gpt-realtime-2.1","usage":{"input_tokens":100,"output_tokens":10}}])
        self.assertIsNone(result["cost_usd"])
