import asyncio
import tempfile
import unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path

from claim_agent.config import Settings
from claim_agent.models import Book, Item, Locale, Price
from claim_agent.pipeline import SweepRunner
from claim_agent.storage import EvidenceStore


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=EvidenceStore(Path(self.tmp.name))
        state=self.store.create("test","India","INR",synthetic=True)
        self.store.put_evidence(state,"frames/f1.jpg",b"frame")
        self.runner=SweepRunner(state,self.store,Settings(data_dir=Path(self.tmp.name)),None,None)
        self.addAsyncCleanup(self.runner.close)

    async def test_correction_invalidates_prices_and_rejects_stale_worker_result(self):
        self.runner.state.books=[Book(id="b1",frame_ref="frames/f1.jpg",status="identified",title="Old title")]
        await self.runner.correct({"action":"claim", "ref_id":"b1", "value":"This is a first edition"})
        applied=await self.runner.apply_prices("b1",0,Price(amount="10"),Price(amount="5"),[])
        self.assertFalse(applied)
        self.assertIsNone(self.runner.state.books[0].replacement_cost.amount)
        self.assertEqual(self.runner.state.books[0].user_claims,["This is a first edition"])

    async def test_excluding_shelf_applies_to_future_and_existing_books(self):
        self.runner.state.books=[Book(id="b1",frame_ref="frames/f1.jpg",shelf="s1")]
        await self.runner.correct({"action":"exclude_shelf","ref_id":"s1","value":"not mine"})
        self.assertTrue(self.runner.state.books[0].excluded)
        self.assertIn("s1",self.runner.state.excluded_shelves)

    async def test_second_locale_preserves_selected_physical_ids(self):
        self.runner.state.books=[Book(id=f"b{i}",frame_ref="frames/f1.jpg",title=f"Title {i}",status="identified") for i in range(12)]
        result=await self.runner.second_locale(Locale(country="United Kingdom",currency="GBP"))
        self.assertEqual([b["id"] for b in result["books"]],[f"b{i}" for i in range(10)])
        self.assertEqual(result["currency"],"GBP")
        self.assertTrue(all(b["replacement_cost"]["amount"] is None for b in result["books"]))

    async def test_unfinished_work_marks_partial_instead_of_success(self):
        self.runner.settings.finish_timeout=.01
        await self.runner.queue.put("unfinished")
        result=await self.runner.finish(10)
        self.assertEqual(result.completion,"partial")
        self.assertTrue(any("deadline" in r.reason for r in result.review_queue))

    async def test_final_geometry_reconciles_shelving_identified_after_geometry_arrives(self):
        self.runner.state.ar_session_id="session"
        obj={"id":"rp1","category":"storage","center":[1,1,.2],"dimensions":[2,2,.4],
             "corners":[[x,y,z] for x in [0,2] for y in [0,2] for z in [0,.4]]}
        geometry={"ar_session_id":"session","floors":[{"corners":[[0,0,0],[3,0,0],[3,0,3],[0,0,3]]}],
                  "walls":[{"id":"w","corners":[[0,0,0],[3,0,0],[3,2.5,0],[0,2.5,0]]}],"objects":[obj],"shelving":[],"openings":[]}
        await self.runner.set_geometry(geometry)
        self.runner.state.items=[Item(id="shelf1",category="bookshelf",frame_ref="frames/f1.jpg",
                                     world_corners=[[0,0,.4],[2,0,.4],[2,2,.4],[0,2,.4]])]
        result=await self.runner.finish(10)
        self.assertEqual(result.room.shelved_wall_area_m2,4)
        self.assertEqual(result.items[0].dimensions_cm.d,40)
        self.assertTrue(any(ref.startswith("geometry/") for ref in result.items[0].evidence_refs))

    async def test_capture_interruption_does_not_disappear_from_packet(self):
        self.runner.state.ar_session_id="session"
        await self.runner.set_geometry({"ar_session_id":"session","floors":[],"walls":[],"capture_issues":["App left foreground during sweep"]})
        result=await self.runner.finish(10)
        self.assertTrue(any("foreground" in r.reason for r in result.review_queue))

    async def test_live_geometry_reaches_voice_without_an_image_model_result(self):
        from types import SimpleNamespace
        messages=[]
        self.runner.live=SimpleNamespace(offer_context=messages.append)
        self.runner.state.ar_session_id="session"
        await self.runner.set_geometry({"ar_session_id":"session","floors":[{"corners":[[0,0,0],[4,0,0],[4,0,3],[0,0,3]]}],"walls":[]})
        self.assertTrue(messages[-1]["measurement_update"])
        self.assertEqual(messages[-1]["inventory"]["room"]["floor_area_m2"],12)
        self.runner.live=None

    async def test_item_conversion_retains_fx_and_market_evidence(self):
        self.runner.state.items=[Item(id="i1",category="lamp",description="Lamp",frame_ref="frames/f1.jpg")]
        p=Price(amount="100",currency="INR",country="India",source="Shop",url="https://shop.example/lamp",
                retrieved_at="2026-10-17T10:00:00Z",source_ref="sources/item.json",condition_assumed="new",verified=True,
                converted=True,original_amount="1",original_currency="GBP",fx_rate="100",fx_source_ref="sources/fx.json")
        await self.runner.apply_prices("i1",0,p,Price(),[("sources/item.json",{}),("sources/fx.json",{})])
        result=self.runner.snapshot().items[0].replacement_cost
        self.assertTrue(result.converted)
        self.assertEqual(result.original_currency,"GBP")
        self.assertEqual(result.fx_source_ref,"sources/fx.json")

    async def test_finish_retry_accepts_only_identical_saved_geometry(self):
        self.runner.state.ar_session_id="session"
        geometry={"ar_session_id":"session","floors":[],"walls":[]}
        await self.runner.set_geometry(geometry)
        original_ref=self.runner.state.room.geometry_ref
        await self.runner.finish(10)
        await self.runner.set_geometry(geometry)
        retried=await self.runner.finish(10)
        self.assertEqual(retried.room.geometry_ref,original_ref)
        with self.assertRaisesRegex(RuntimeError,"ended"):
            await self.runner.set_geometry({**geometry,"capture_issues":["different payload"]})

    async def test_later_rare_evidence_invalidates_prices_regardless_of_confidence(self):
        import numpy as np
        from claim_agent.vision import Scan,Detection
        from tests.test_geometry import frame
        revisions=[]
        for number,confidence,signals in [(1,.95,[]),(2,.90,["signed"]),(3,.99,[])]:
            meta=frame().model_copy(update={"id":f"f{number}"})
            meta.ocr=[{"text":"Dune","center":[.457,.5]}]
            if number>1: self.store.put_evidence(self.runner.state,f"frames/{meta.id}.jpg",b"frame")
            self.store.put_evidence(self.runner.state,f"depth/{meta.id}.bin",np.full((100,100),2,dtype='<f4').tobytes())
            self.store.put_evidence(self.runner.state,f"depth/{meta.id}.confidence",bytes([2])*10000)
            d=Detection(kind="book",polygon=[[.45,.45],[.465,.45],[.465,.55],[.45,.55]],title="Dune",visible_text="Dune",confidence=confidence,rare_signals=signals)
            self.runner.apply_scan(meta,Scan(detections=[d]))
            book=self.runner.state.books[0]
            revisions.append(book.revision)
            if number==1:
                book.replacement_cost=Price(amount="500")
            else:
                self.assertEqual(book.status,"needs_appraisal")
                self.assertIn("signed",book.rare_signals)
                self.assertIsNone(book.replacement_cost.amount)
        self.assertGreater(revisions[1],revisions[0])
        self.assertFalse(await self.runner.apply_prices(book.id,revisions[0],Price(amount="500"),Price(),[]))

    async def test_capture_quality_failure_persists_into_partial_packet(self):
        from claim_agent.vision import Scan
        from tests.test_geometry import frame
        self.runner.apply_scan(frame(),Scan(quality_issues=["Shelf fully obscured by glare"]))
        packet=await self.runner.finish(5)
        self.assertEqual(packet.completion,"partial")
        self.assertTrue(any("glare" in row.reason for row in packet.review_queue))

    async def test_packet_timing_includes_delay_after_capture_before_finish(self):
        self.runner.state.sweep.captured_at=(datetime.now(timezone.utc)-timedelta(seconds=100)).isoformat()
        packet=await self.runner.finish(10)
        duration=(datetime.fromisoformat(packet.sweep.packet_ready_at)-datetime.fromisoformat(packet.sweep.ended_at)).total_seconds()
        self.assertGreaterEqual(duration,89)
        self.assertGreaterEqual(packet.metrics["time_to_packet_s"],89)
