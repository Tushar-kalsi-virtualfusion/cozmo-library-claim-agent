import unittest

import numpy as np

from claim_agent.models import FrameMetadata
from claim_agent.object_anchor import object_depth_anchor


def frame():
    return FrameMetadata(id="f1", timestamp_s=1, image_width=1000, image_height=1000,
                         intrinsics=[1000, 0, 0, 0, 1000, 0, 500, 500, 1],
                         camera_transform=np.eye(4).flatten(order="F").tolist(), tracking="normal",
                         depth_width=100, depth_height=100, ar_session_id="session-a")


class ObjectAnchorTests(unittest.TestCase):
    polygon = [[.3, .3], [.7, .3], [.7, .7], [.3, .7]]

    def test_nonplanar_surface_keeps_identity_evidence_without_dimensions(self):
        yy, xx = np.mgrid[:100, :100]
        depth = 2 + .3 * ((xx - 49.5) / 20) ** 2 + .08 * np.sin(yy / 5)
        result = object_depth_anchor(frame(), depth, np.full((100, 100), 2), self.polygon)
        self.assertIsNotNone(result)
        self.assertLess(np.linalg.norm(np.asarray(result["center"]) - [0, 0, -2.06]), .1)
        self.assertTrue(result["identity_only"])
        self.assertGreater(result["samples"], 100)
        for forbidden in ("height_cm", "width_cm", "dimensions", "world_corners"):
            self.assertNotIn(forbidden, result)

    def test_calibrated_pose_maps_anchor_into_world_coordinates(self):
        meta = frame()
        pose = np.eye(4)
        pose[:3, 3] = [.4, .1, .3]
        meta = meta.model_copy(update={"camera_transform": pose.flatten(order="F").tolist()})
        result = object_depth_anchor(meta, np.full((100, 100), 2.), np.full((100, 100), 2), self.polygon)
        np.testing.assert_allclose(result["center"], [.4, .1, -1.7], atol=.005)

    def test_camera_translation_preserves_same_physical_anchor(self):
        depth, confidence = np.full((100, 100), 2.), np.full((100, 100), 2)
        first = object_depth_anchor(frame(), depth, confidence, self.polygon)
        pose = np.eye(4)
        pose[0, 3] = .1
        meta = frame().model_copy(update={"id": "f2", "camera_transform": pose.flatten(order="F").tolist()})
        moved = (np.asarray(self.polygon) - [.05, 0]).tolist()
        second = object_depth_anchor(meta, depth, confidence, moved)
        np.testing.assert_allclose(first["center"], second["center"], atol=.01)

    def test_adjacent_identical_objects_keep_distinct_anchor_centers(self):
        left = [[.2, .3], [.4, .3], [.4, .7], [.2, .7]]
        right = [[.6, .3], [.8, .3], [.8, .7], [.6, .7]]
        depth, confidence = np.full((100, 100), 2.), np.full((100, 100), 2)
        a = object_depth_anchor(frame(), depth, confidence, left)
        b = object_depth_anchor(frame(), depth, confidence, right)
        self.assertGreater(np.linalg.norm(np.asarray(a["center"]) - b["center"]), .75)

    def test_sparse_but_spatially_distributed_reliable_patch_is_usable(self):
        confidence = np.zeros((100, 100), dtype=np.uint8)
        confidence[45:50, 44:54] = 2
        result = object_depth_anchor(frame(), np.full((100, 100), 2.), confidence, self.polygon)
        self.assertIsNotNone(result)
        self.assertEqual(result["samples"], 50)
        self.assertLess(result["coverage"], .1)

    def test_eroded_interior_ignores_polygon_boundary_background(self):
        depth = np.full((100, 100), 4.)
        depth[33:67, 33:67] = 2.
        result = object_depth_anchor(frame(), depth, np.full((100, 100), 2), self.polygon)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["center"][2], -2, places=5)

    def test_balanced_or_background_dominated_depth_is_rejected(self):
        for foreground_rows in (12, 20):
            depth = np.full((100, 100), 4.)
            depth[30:30 + foreground_rows, 30:70] = 2.
            with self.subTest(foreground_rows=foreground_rows):
                self.assertIsNone(object_depth_anchor(frame(), depth, np.full((100, 100), 2), self.polygon))

    def test_dominant_near_surface_rejects_small_background_component(self):
        depth = np.full((100, 100), 2.)
        depth[30:38, 30:70] = 4.
        result = object_depth_anchor(frame(), depth, np.full((100, 100), 2), self.polygon)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["center"][2], -2, places=5)

    def test_large_continuous_depth_spread_is_ambiguous(self):
        depth = np.tile(np.linspace(.5, 6.5, 100), (100, 1))
        self.assertIsNone(object_depth_anchor(frame(), depth, np.full((100, 100), 2), self.polygon))

    def test_low_confidence_too_few_and_collinear_samples_are_rejected(self):
        depth = np.full((100, 100), 2.)
        sparse = np.zeros((100, 100), dtype=np.uint8)
        sparse[45:49, 45:49] = 2
        line = np.zeros((100, 100), dtype=np.uint8)
        line[50, 33:67] = 2
        for confidence in (np.ones((100, 100)), sparse, line):
            self.assertIsNone(object_depth_anchor(frame(), depth, confidence, self.polygon))

    def test_invalid_tracking_pose_calibration_and_polygon_are_rejected(self):
        depth, confidence = np.full((100, 100), 2.), np.full((100, 100), 2)
        reflected = np.eye(4)
        reflected[0, 0] = -1
        cases = [frame().model_copy(update={"tracking": "limited"}),
                 frame().model_copy(update={"intrinsics": [0] * 9}),
                 frame().model_copy(update={"intrinsics": [1000, 0, 0, 0, 1000, 0, -500, 500, 1]}),
                 frame().model_copy(update={"intrinsics": [1000, 0, 0, 50, 1000, 0, 500, 500, 1]}),
                 frame().model_copy(update={"ar_session_id": ""}),
                 frame().model_copy(update={"camera_transform": reflected.flatten(order="F").tolist()}),
                 frame().model_copy(update={"camera_transform": [float("nan")] * 16})]
        for meta in cases:
            self.assertIsNone(object_depth_anchor(meta, depth, confidence, self.polygon))
        for polygon in ([[0, 0]], [[0, 0], [1, 1], [1, 0], [0, 1]],
                        [[-.1, 0], [1, 0], [1, 1], [0, 1]]):
            self.assertIsNone(object_depth_anchor(frame(), depth, confidence, polygon))
        self.assertIsNone(object_depth_anchor(frame(), depth[:20], confidence, self.polygon))
