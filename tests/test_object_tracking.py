import unittest
import tempfile
import numpy as np
from pathlib import Path

from claim_agent.tracking import TrackRegistry, roomplan_anchor
from claim_agent.config import Settings
from claim_agent.models import FrameMetadata
from claim_agent.pipeline import SweepRunner
from claim_agent.storage import EvidenceStore
from claim_agent.vision import Detection, Scan


def camera(x=0, session="room"):
    return {"ar_session_id":session,"tracking":"normal","image_width":100,"image_height":100,
            "intrinsics":[100,0,0,0,100,0,50,50,1],
            "camera_transform":[1,0,0,0,0,1,0,0,0,0,1,0,x,0,0,1]}


def observation(frame="f1", x=0, depth=True, category="office chair", anchor=""):
    return {"kind":"item","category":category,"frame_id":frame,"metadata":camera(x),
            "polygon":[[.3-x/2,.3],[.7-x/2,.3],[.7-x/2,.7],[.3-x/2,.7]],
            "world_corners":[[-.4,.4,-2],[.4,.4,-2],[.4,-.4,-2],[-.4,-.4,-2]] if depth else [],
            "roomplan_id":anchor}


class ObjectTrackingTests(unittest.TestCase):
    def test_conflicting_measured_depth_never_falls_back_to_overlap(self):
        registry=TrackRegistry();first=registry.associate(observation())
        other=observation("f2")
        other["world_corners"]=[[x,y,z-.3] for x,y,z in other["world_corners"]]
        self.assertNotEqual(first["id"],registry.associate(other)["id"])

    def test_observed_support_overrides_bad_plane_but_rejects_separate_object(self):
        def support(x): return {"center":[x,0,-2],"radius_m":.2}
        registry=TrackRegistry()
        first=observation();first["depth_anchor"]=support(0)
        identifier=registry.associate(first)["id"]
        repeated=observation("f2");repeated["depth_anchor"]=support(.03)
        repeated["world_corners"]=[[x,y,z-.8] for x,y,z in repeated["world_corners"]]
        self.assertEqual(identifier,registry.associate(repeated)["id"])
        different=observation("f3");different["depth_anchor"]=support(.3)
        self.assertNotEqual(identifier,registry.associate(different)["id"])

    def test_verified_correspondence_can_link_camera_only_motion(self):
        class Correspondence:
            def match_polygon(self,*args): return .9
        registry=TrackRegistry(correspondence=Correspondence())
        identifier=registry.associate(observation(depth=False))["id"]
        self.assertEqual(identifier,registry.associate(observation("f2",x=.4,depth=False))["id"])
        self.assertNotEqual(identifier,registry.associate(observation("f3",x=2,depth=False))["id"])

    def test_pipeline_retains_one_inventory_and_price_job_across_repeated_chair_views(self):
        with tempfile.TemporaryDirectory() as root:
            store=EvidenceStore(Path(root));state=store.create("test","India","INR",synthetic=True)
            state.geometry={"ar_session_id":"room","objects":[{"id":"chair","category":"chair","confidence":"high",
                "center":[0,0,-2],"dimensions":[.8,.8,.4],
                "corners":[[x,y,z] for x in [-.4,.4] for y in [-.4,.4] for z in [-2.2,-1.8]]}]}
            runner=SweepRunner(state,store,Settings(paid_pricing=True),None,None)
            for index in range(4):
                o=observation(f"f{index}",x=index*.05,depth=False)
                meta=FrameMetadata(id=o["frame_id"],timestamp_s=index,**o["metadata"])
                runner.apply_scan(meta,Scan(detections=[Detection(kind="item",category="office chair",
                    material="upholstery" if index%2 else "black upholstery",polygon=o["polygon"],confidence=.9)]))
                if index==0: initial_jobs=runner.price_queue.qsize()
                else: self.assertEqual(runner.price_queue.qsize(),initial_jobs)
            self.assertEqual(len(state.items),1)
            self.assertEqual(sum(ref.startswith("frames/") for ref in state.items[0].evidence_refs),4)
            self.assertEqual(state.items[0].dimensions_cm.w,80)
            self.assertEqual(len(state.pricing_jobs),1)

    def test_reprojected_item_survives_missing_depth_and_camera_motion(self):
        tracks=TrackRegistry()
        first=tracks.associate(observation())
        again=tracks.associate(observation("f2",x=.25,depth=False))
        self.assertEqual(first["id"],again["id"])
        self.assertFalse(again["new"])

    def test_roomplan_identity_survives_different_visible_sides(self):
        tracks=TrackRegistry()
        first=tracks.associate(observation(anchor="physical-chair"))
        other=observation("f2",x=.5,depth=False,anchor="physical-chair")
        other["polygon"]=[[.1,.4],[.45,.4],[.45,.9],[.1,.9]]
        self.assertEqual(first["id"],tracks.associate(other)["id"])

    def test_same_class_separate_chairs_do_not_merge(self):
        tracks=TrackRegistry()
        a=tracks.associate(observation(anchor="chair-1"))
        b=tracks.associate(observation(anchor="chair-2"))
        self.assertNotEqual(a["id"],b["id"])
        self.assertEqual(a["id"],tracks.associate(observation("f2",depth=False,anchor="chair-1"))["id"])
        self.assertEqual(b["id"],tracks.associate(observation("f2",depth=False,anchor="chair-2"))["id"])

    def test_camera_only_match_requires_pose_and_overlap_not_category_alone(self):
        tracks=TrackRegistry()
        a=tracks.associate(observation(depth=False))
        self.assertEqual(a["id"],tracks.associate(observation("f2",x=.005,depth=False))["id"])
        self.assertNotEqual(a["id"],tracks.associate(observation("f3",x=1,depth=False))["id"])

    def test_conflicting_category_session_or_anchor_rejects_match(self):
        for change in [{"category":"table"}, {"metadata":camera(session="other")}, {"roomplan_id":"other"}]:
            with self.subTest(change=change):
                tracks=TrackRegistry();a=tracks.associate(observation(anchor="chair"))
                other={**observation("f2",depth=False,anchor="chair"),**change}
                self.assertNotEqual(a["id"],tracks.associate(other)["id"])

    def test_ambiguous_projected_objects_are_not_arbitrarily_merged(self):
        tracks=TrackRegistry()
        tracks.associate(observation())
        tracks.associate(observation())
        self.assertTrue(tracks.associate(observation("f2",depth=False))["new"])

    def test_roomplan_anchor_uses_projected_box_category_and_session(self):
        obj={"id":"chair","category":"chair","confidence":"high","center":[0,0,-2],
             "dimensions":[.8,.8,.4],"corners":[[x,y,z] for x in [-.4,.4] for y in [-.4,.4] for z in [-2.2,-1.8]]}
        geometry={"ar_session_id":"room","objects":[obj]}
        o=observation()
        self.assertEqual(roomplan_anchor(o,geometry),"chair")
        self.assertEqual(roomplan_anchor({**o,"depth_anchor":{"center":[0,0,-1],"radius_m":.2}},geometry),"")
        rotation=np.array([[2**-.5,0,2**-.5],[0,1,0],[-2**-.5,0,2**-.5]])
        rotated={**obj,"corners":((np.array(obj["corners"])-[0,0,-2])@rotation.T+[0,0,-2]).tolist()}
        self.assertEqual(roomplan_anchor({**o,"depth_anchor":{"center":[.3,0,-1.7],"radius_m":.04}},
                                        {**geometry,"objects":[rotated]}),"")
        self.assertEqual(roomplan_anchor({**o,"category":"table"},geometry),"")
        self.assertEqual(roomplan_anchor({**o,"metadata":camera(session="other")},geometry),"")
        self.assertEqual(roomplan_anchor(o,{**geometry,"objects":[obj,{**obj,"id":"second-chair"}]}),"")
