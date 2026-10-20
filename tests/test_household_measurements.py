import unittest
from types import SimpleNamespace
from claim_agent.geometry import reconcile_room
from claim_agent.models import Item, Room


class HouseholdMeasurements(unittest.TestCase):
    def state(self):
        obj={"id":"table-rp","category":"table","center":[0,.4,0],"dimensions":[1.4,.8,.7],
             "confidence":"high","corners":[[x,y,z] for x in [-.7,.7] for y in [0,.8] for z in [-.35,.35]]}
        return SimpleNamespace(geometry={"objects":[obj],
            "floors":[{"corners":[[0,0,0],[4,0,0],[4,0,3],[0,0,3]]}],
            "walls":[{"id":"w","corners":[[0,0,0],[4,0,0],[4,2.6,0],[0,2.6,0]]}]},
            room=Room(geometry_ref="geometry/room.json"),items=[Item(id="t",category="coffee table",frame_ref="frames/f.jpg",
                world_corners=[[-.7,.8,-.35],[.7,.8,-.35],[.7,.8,.35],[-.7,.8,.35]])])

    def test_table_and_room_measurements_are_available_without_model_guesses(self):
        s=self.state(); r=reconcile_room(s)
        self.assertEqual((r.length_m,r.width_m,r.height_m),(4,3,2.6))
        self.assertEqual(r.floor_area_m2,12)
        self.assertEqual(s.items[0].dimensions_cm.w,140)
        self.assertEqual(r.furniture[0].dimensions_cm.h,80)
        self.assertEqual(r.furniture[0].geometry_ref,"geometry/room.json")

    def test_low_confidence_and_invalid_boxes_cannot_supply_dimensions(self):
        for change in ({"confidence":"low"},{"dimensions":[1,-2,3]},{"center":[0,float('nan'),0]}):
            s=self.state(); s.geometry["objects"][0].update(change)
            r=reconcile_room(s)
            self.assertEqual(r.furniture,[])
            self.assertIsNone(s.items[0].dimensions_cm.w)

    def test_revised_geometry_clears_stale_table_dimensions(self):
        s=self.state(); s.room=reconcile_room(s)
        s.geometry["objects"]=[]
        reconcile_room(s)
        self.assertIsNone(s.items[0].dimensions_cm.w)

    def test_no_depth_anchor_does_not_match_table_identity_by_category_alone(self):
        s=self.state(); s.items[0].world_corners=[]
        r=reconcile_room(s)
        self.assertEqual(len(r.furniture),1)
        self.assertIsNone(s.items[0].dimensions_cm.w)
