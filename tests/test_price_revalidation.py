import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from claim_agent.config import Settings
from claim_agent.geometry import reconcile_room
from claim_agent.models import Dimensions, Item, Locale
from claim_agent.pipeline import SweepRunner
from claim_agent.pricing import extract_offers, pricing_fingerprint, pricing_query
from claim_agent.storage import EvidenceStore
from tests import test_household_measurements as geometry_fixtures


class RawDimensionRevalidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store=EvidenceStore(Path(self.tmp.name))
        state=self.store.create("test","India","INR",synthetic=True)
        self.store.put_evidence(state,"frames/f.jpg",b"frame")
        self.item=Item(id="chair",frame_ref="frames/f.jpg",category="chair",dimensions_cm=Dimensions(w=62.1))
        state.items=[self.item]
        self.runner=SweepRunner(state,self.store,Settings(paid_pricing=True),None,None)
        self.addAsyncCleanup(self.runner.close)
        self.runner.enqueue_price(self.item)

    def quote(self, width=53.8):
        product={"@type":"Product","name":"Office chair","category":"chair",
                 "offers":{"@type":"Offer","price":"3999","priceCurrency":"INR","eligibleRegion":"IN",
                           "itemCondition":"https://schema.org/NewCondition","availability":"https://schema.org/InStock"}}
        if width is not None: product["width"]={"value":width,"unitCode":"CMT"}
        doc='<script type="application/ld+json">'+json.dumps(product)+'</script>'
        p,e=extract_offers(doc,"https://shop.in/chair",pricing_query(self.item),Locale(country="India",currency="INR"),"new")[0]
        p.source_ref="sources/offer_test.json"
        return p,[(p.source_ref,e)]

    async def test_late_quote_rechecks_raw_dimensions_inside_same_fingerprint_bucket(self):
        quote,sources=self.quote(); fingerprint=pricing_fingerprint(self.item)
        self.item.dimensions_cm.w=63.9
        self.assertEqual(pricing_fingerprint(self.item),fingerprint)
        await self.runner.apply_quote(self.item.id,0,"new",quote,sources,fingerprint)
        self.assertIsNone(self.item.replacement_cost.low)
        self.assertEqual(self.item.pricing_status,"unavailable")
        self.assertIn("current",self.item.pricing_reason)
        self.assertEqual(self.runner.price_queue.qsize(),1)

    async def test_supported_small_change_keeps_quote_without_requeue(self):
        quote,sources=self.quote(); fingerprint=pricing_fingerprint(self.item)
        self.item.dimensions_cm.w=62.2
        await self.runner.apply_quote(self.item.id,0,"new",quote,sources,fingerprint)
        self.assertEqual(self.item.replacement_cost.low,Decimal("3999"))
        self.assertEqual(self.item.revision,0)
        self.assertEqual(self.runner.price_queue.qsize(),1)

    async def test_geometry_update_clears_completed_offer_that_no_longer_matches(self):
        quote,sources=self.quote(); fingerprint=pricing_fingerprint(self.item)
        await self.runner.apply_quote(self.item.id,0,"new",quote,sources,fingerprint)
        state=self.runner.state
        state.ar_session_id="room"
        state.spatial_tracks[self.item.id]={"roomplan_id":"chair-rp"}
        # Other dimensions already existed when the search was admitted, so only
        # width changes inside its price-identity bucket in this fixture.
        self.item.dimensions_cm=Dimensions(w=62.1,h=100,d=50)
        state.pricing_jobs["chair:new"]["fingerprint"]=pricing_fingerprint(self.item)
        await self.runner.set_geometry({"ar_session_id":"room","objects":[{"id":"chair-rp","category":"chair",
            "center":[0,.5,-2],"dimensions":[.639,1,.5],"confidence":"high"}],"floors":[],"walls":[]})
        self.assertEqual(self.item.dimensions_cm.w,63.9)
        self.assertIsNone(self.item.replacement_cost.low)
        self.assertEqual(self.item.status,"unpriced")
        self.assertEqual(self.item.pricing_status,"unavailable")
        self.assertEqual(self.item.revision,0)
        self.assertEqual(self.runner.price_queue.qsize(),1)

    async def test_missing_retailer_dimensions_remain_explicitly_unverified(self):
        quote,sources=self.quote(width=None); fingerprint=pricing_fingerprint(self.item)
        self.item.dimensions_cm.w=63.9
        await self.runner.apply_quote(self.item.id,0,"new",quote,sources,fingerprint)
        self.assertEqual(self.item.replacement_cost.low,Decimal("3999"))
        self.assertEqual(self.item.replacement_cost.match,"equivalent")
        self.assertIn("width unverified",self.item.replacement_cost.match_basis)


class AnchoredGeometryCategoryTests(unittest.TestCase):
    def test_changed_roomplan_category_cannot_supply_old_category_dimensions(self):
        state=geometry_fixtures.HouseholdMeasurements().state()
        state.spatial_tracks={"t":{"roomplan_id":"table-rp"}}
        reconcile_room(state)
        self.assertEqual(state.items[0].dimensions_cm.w,140)
        state.geometry["objects"][0]["category"]="chair"
        reconcile_room(state)
        self.assertIsNone(state.items[0].dimensions_cm.w)
