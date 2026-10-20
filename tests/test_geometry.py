import unittest

import numpy as np

from claim_agent.geometry import measure_room, measure_spine, occupied_shelf_run
from claim_agent.models import FrameMetadata


def frame():
    return FrameMetadata(id="f1", timestamp_s=1, image_width=1000, image_height=1000,
                         intrinsics=[1000, 0, 0, 0, 1000, 0, 500, 500, 1],
                         camera_transform=np.eye(4).flatten(order="F").tolist(), tracking="normal",
                         depth_width=100, depth_height=100, ar_session_id="one-session")


class GeometryTests(unittest.TestCase):
    def test_calibrated_plane_measures_upright_and_stacked_spines(self):
        for polygon in [ [[.45,.45],[.465,.45],[.465,.55],[.45,.55]],
                         [[.45,.45],[.55,.45],[.55,.465],[.45,.465]] ]:
            result = measure_spine(frame(), np.full((100, 100), 2.), np.full((100,100), 2), polygon)
            self.assertAlmostEqual(result["height_cm"], 20, places=4)
            self.assertAlmostEqual(result["thickness_cm"], 3, places=4)
            self.assertAlmostEqual(result["world_corners"][0][2], -2, places=4)

    def test_depth_confidence_tracking_and_calibration_fail_closed(self):
        polygon = [[.45,.45],[.465,.45],[.465,.55],[.45,.55]]
        cases = [(frame(), np.zeros((100,100)), np.full((100,100),2)),
                 (frame(), np.full((100,100),2.), np.zeros((100,100))),
                 (frame().model_copy(update={"tracking":"limited"}), np.full((100,100),2.), np.full((100,100),2)),
                 (frame().model_copy(update={"intrinsics":[0]*9}), np.full((100,100),2.), np.full((100,100),2))]
        for meta, depth, confidence in cases:
            with self.subTest(meta=meta.tracking):
                result = measure_spine(meta, depth, confidence, polygon)
                self.assertIsNone(result["height_cm"])
                self.assertTrue(result["reason"])

    def test_concave_room_uses_polygon_not_bounding_rectangle(self):
        g = {"floors":[{"corners":[[0,0,0],[3,0,0],[3,0,1],[2,0,1],[2,0,3],[0,0,3]]}],
             "walls":[{"id":"w1","corners":[[0,0,0],[3,0,0],[3,2.5,0],[0,2.5,0]]}],
             "shelving":[{"wall_id":"w1","corners":[[0,0,0],[2,0,0],[2,2,0],[0,2,0]]},
                         {"wall_id":"w1","corners":[[1,0,0],[3,0,0],[3,2,0],[1,2,0]]}], "openings":[]}
        r = measure_room(g)
        self.assertAlmostEqual(r.floor_area_m2, 7)
        self.assertAlmostEqual(r.wall_area_m2, 7.5)
        self.assertAlmostEqual(r.shelved_wall_area_m2, 6)
        self.assertAlmostEqual(r.floor_area_ft2, 75.34737, places=4)
        self.assertEqual(r.length_m, 3)
        self.assertEqual(r.width_m, 3)

    def test_invalid_room_polygon_is_unknown_not_repaired_into_invented_area(self):
        r = measure_room({"floors":[{"corners":[[0,0,0],[2,0,2],[0,0,2],[2,0,0]]}],"walls":[]})
        self.assertIsNone(r.floor_area_m2)
        self.assertIsNone(r.wall_area_m2)
        self.assertTrue(r.review_reasons)

    def test_shelf_run_unions_overlap_per_shelf_and_stacks(self):
        observations = [{"shelf":"s1","start":0,"end":.2}, {"shelf":"s1","start":.1,"end":.3},
                        {"shelf":"s1","start":0,"end":.2}, {"shelf":"s2","start":0,"end":.2}]
        self.assertAlmostEqual(occupied_shelf_run(observations), .5)
        self.assertIsNone(occupied_shelf_run([]))
