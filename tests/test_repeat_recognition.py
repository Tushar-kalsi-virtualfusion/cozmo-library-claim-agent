import json
import math
import unittest
from types import SimpleNamespace

from claim_agent.local_evidence import cloud_decision
from claim_agent.vision import Scan
from tests.test_hybrid import meta


LEFT = [[.05, .1], [.35, .1], [.35, .8], [.05, .8]]
RIGHT = [[.65, .1], [.95, .1], [.95, .8], [.65, .8]]


def picture(offset=0):
    # A spatially varying thumbnail leaves room for realistic exposure shifts.
    return {"pixels": [40 + (row * 11 + col * 7) % 140 + offset
                       for row in range(16) for col in range(16)], "sha256": str(offset)}


class RepeatRecognitionTests(unittest.TestCase):
    def setUp(self):
        self.metrics = {}
        self.settings = SimpleNamespace(cloud_max_calls=20, cloud_cooldown_s=0)
        self.result = {"unresolved": True, "signature": "same-unresolved-evidence",
                       "scan": Scan(), "complete": False, "provisional_objects": []}
        self.quality = {"sharpness": 100, "dark_fraction": 0, "bright_fraction": 0}
        self.now = 100

    def admit(self, metadata=None, image=None, sharpness=100, objects=None):
        self.now += 30
        result = dict(self.result)
        if objects is not None:
            result["provisional_objects"] = [{"polygon": p, "confidence": .9, "category": "same category"}
                                              for p in objects]
        return cloud_decision(result, {**self.quality, "sharpness": sharpness}, self.metrics,
                              self.settings, True, self.now, metadata or meta(), image or picture())[0]

    def test_small_camera_translation_does_not_reinspect_same_evidence(self):
        self.assertTrue(self.admit())
        self.assertFalse(self.admit(meta("f2", x=.03), {**picture(), "sha256": "reencoded"}))
        self.assertEqual(self.metrics["cloud_routing"]["attempts"], 1)

    def test_small_camera_rotation_does_not_reinspect_same_evidence(self):
        self.assertTrue(self.admit())
        second = meta("f2")
        angle = math.radians(3)
        second.camera_transform[:3] = [math.cos(angle), 0, -math.sin(angle)]
        second.camera_transform[8:11] = [math.sin(angle), 0, math.cos(angle)]
        self.assertFalse(self.admit(second))

    def test_exposure_change_is_not_a_new_scene(self):
        self.assertTrue(self.admit())
        self.assertFalse(self.admit(meta("f2", x=.03), picture(offset=12)))

    def test_small_image_shift_with_camera_drift_is_not_a_new_scene(self):
        self.assertTrue(self.admit())
        pixels = picture()["pixels"]
        shifted = [pixels[row * 16 + max(0, col - 1)] for row in range(16) for col in range(16)]
        self.assertFalse(self.admit(meta("f2", x=.03), {"pixels": shifted, "sha256": "shifted"}))

    def test_distinct_object_position_remains_eligible_with_similar_appearance(self):
        self.assertTrue(self.admit())
        self.assertTrue(self.admit(meta("f2", x=1), {**picture(), "sha256": "other-position"}))

    def test_new_angle_for_coverage_remains_eligible(self):
        self.assertTrue(self.admit())
        second = meta("f2")
        angle = math.radians(25)
        second.camera_transform[:3] = [math.cos(angle), 0, -math.sin(angle)]
        second.camera_transform[8:11] = [math.sin(angle), 0, math.cos(angle)]
        self.assertTrue(self.admit(second, {**picture(), "sha256": "new-angle"}))

    def test_changed_scene_without_camera_motion_remains_eligible(self):
        self.assertTrue(self.admit())
        changed = {"pixels": [220 if (i // 16) % 2 else 20 for i in range(256)], "sha256": "new-scene"}
        self.assertTrue(self.admit(meta("f2"), changed))

    def test_new_nonoverlapping_object_of_same_category_remains_eligible(self):
        self.assertTrue(self.admit(objects=[LEFT]))
        self.assertTrue(self.admit(meta("f2"), {**picture(), "sha256": "added-object"}, objects=[LEFT, RIGHT]))

    def test_new_readable_ocr_remains_eligible_with_same_coverage_signature(self):
        self.assertTrue(self.admit())
        second = meta("f2").model_copy(update={"ocr": [{"text": "Dune Frank Herbert", "center": [.5, .5], "confidence": .95}]})
        self.assertTrue(self.admit(second, {**picture(), "sha256": "readable-label"}))

    def test_low_confidence_text_does_not_unlock_repeated_recognition(self):
        self.assertTrue(self.admit())
        second = meta("f2").model_copy(update={"ocr": [{"text": "Dune Frank Herbert", "center": [.5, .5], "confidence": .1}]})
        self.assertFalse(self.admit(second))

    def test_one_clarity_retry_cannot_become_endless_sharpness_retries(self):
        self.assertTrue(self.admit(sharpness=100))
        self.assertTrue(self.admit(meta("f2"), sharpness=150))
        self.assertFalse(self.admit(meta("f3"), sharpness=225))
        self.assertEqual(self.metrics["cloud_routing"]["attempts"], 2)

    def test_failed_attempt_reservation_survives_reload_and_camera_jitter(self):
        self.assertTrue(self.admit())
        # A timeout does not provide a result to cache; the reservation itself
        # must still prevent the next captures from endlessly calling again.
        self.metrics = json.loads(json.dumps(self.metrics))
        for index in range(10):
            self.assertFalse(self.admit(meta(f"retry{index}", x=.03 + index * .001), picture(offset=index)))
        self.assertEqual(self.metrics["cloud_routing"]["attempts"], 1)

    def test_readable_evidence_is_reserved_even_without_image_fingerprint(self):
        metadata = meta().model_copy(update={"ocr": [{"text": "Readable object label", "center": [.5, .5]}]})
        first = cloud_decision(self.result, self.quality, self.metrics, self.settings, True, 100, metadata)
        second = cloud_decision(self.result, self.quality, self.metrics, self.settings, True, 130, metadata)
        self.assertTrue(first[0])
        self.assertFalse(second[0])
        self.assertEqual(self.metrics["cloud_routing"]["attempts"], 1)
