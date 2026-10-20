import unittest

from claim_agent.tracking import TrackRegistry


def observation(x, frame_id="f1", text="Same title"):
    return {"kind":"book", "frame_id":frame_id, "visible_text":text,
            "polygon":[[.1,.1],[.2,.1],[.2,.5],[.1,.5]],
            "world_corners":[[x-.015,0,-2],[x+.015,0,-2],[x+.015,.2,-2],[x-.015,.2,-2]]}


class TrackingTests(unittest.TestCase):
    def test_adjacent_identical_copies_remain_distinct_and_repeat_view_matches(self):
        r = TrackRegistry()
        a = r.associate(observation(0))
        b = r.associate(observation(.04))
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(r.associate(observation(.002, "f2"))["id"], a["id"])
        self.assertEqual(r.associate(observation(.042, "f2"))["id"], b["id"])

    def test_two_detections_in_same_frame_cannot_match_one_track(self):
        r = TrackRegistry()
        a = r.associate(observation(0))
        b = r.associate(observation(.001))
        self.assertNotEqual(a["id"], b["id"])

    def test_unknown_geometry_does_not_merge_using_title(self):
        r = TrackRegistry()
        o = observation(0)
        o["world_corners"] = []
        a = r.associate(o)
        o["frame_id"] = "f2"
        b = r.associate(o)
        self.assertNotEqual(a["id"], b["id"])
        self.assertIn("association", b["reason"])

    def test_distinct_shelves_with_identical_titles_do_not_merge(self):
        r = TrackRegistry()
        a = r.associate(observation(0))
        o = observation(0, "f2")
        for point in o["world_corners"]:
            point[1] += .3
        self.assertNotEqual(a["id"], r.associate(o)["id"])
