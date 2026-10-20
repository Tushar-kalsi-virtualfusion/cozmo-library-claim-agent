import tempfile
import unittest
from pathlib import Path

from claim_agent.inventory_repair import repair_inventory
from claim_agent.models import Book, Item, Dimensions, PriceRange
from claim_agent.pricing import pricing_fingerprint
from claim_agent.storage import EvidenceStore
from claim_agent.vision import Detection, Scan
from tests.test_hybrid import meta

LEFT = [[.05,.15],[.35,.15],[.35,.8],[.05,.8]]
RIGHT = [[.65,.15],[.95,.15],[.95,.8],[.65,.8]]


class InventoryRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = EvidenceStore(Path(self.tmp.name))
        self.state = self.store.create('test', 'India', 'INR', synthetic=True)
        self.state.ar_session_id = 'test'

    def observation(self, frame, entries):
        metadata = meta(frame).model_copy(update={'timestamp_s': len(self.state.frames)+1})
        self.state.frames[frame] = metadata
        scan = Scan(detections=[Detection(kind='item', category='chair', polygon=polygon, confidence=.9)
                                for _, polygon in entries])
        self.store.put_evidence(self.state, f'frames/{frame}.jpg', frame.encode())
        self.store.put_json(self.state, f'observations/meta_{frame}.json', metadata.model_dump())
        self.store.put_json(self.state, f'observations/vision_{frame}.json', {'scan': scan.model_dump()})
        self.store.put_json(self.state, f'observations/overlays_{frame}.json', {'frame_id': frame, 'detections': [
            {'id': id, 'kind': 'item', 'polygon': polygon} for id, polygon in entries]})
        for id, _ in entries:
            record = next((item for item in self.state.items if item.id == id), None)
            if record is None:
                record = Item(id=id, category='chair', frame_ref=f'frames/{frame}.jpg')
                self.state.items.append(record)
            record.evidence_refs.append(f'frames/{frame}.jpg')

    def test_repeated_views_merge_to_earliest_existing_id_without_writing(self):
        self.observation('f1', [('old-first', LEFT)])
        self.observation('f2', [('old-repeat', LEFT)])
        self.state.items[0].review_reasons = ['first review']
        self.state.items[1].review_reasons = ['second review']
        self.state.items[1].user_claims = ['This belongs to me']
        self.state.items[1].description = 'Owner-confirmed chair'
        self.state.items[1].excluded = True
        self.state.items.append(Item(id='unobserved', category='lamp', frame_ref='frames/absent.jpg'))
        self.state.books.append(Book(id='book', frame_ref='frames/f1.jpg', title='Keep this book'))
        self.state.metrics['inventory_aliases'] = {'older-alias': 'old-repeat'}
        self.state.pricing_jobs['old-repeat:new'] = {'id':'old-repeat', 'status':'queued'}
        before = self.state.model_dump_json()
        files_before = {str(p): p.read_bytes() for p in Path(self.tmp.name).rglob('*') if p.is_file()}
        repaired, report = repair_inventory(self.store, self.state)
        self.assertEqual([item.id for item in repaired.items], ['old-first', 'unobserved'])
        self.assertEqual(report['merged_items'], 1)
        self.assertEqual(repaired.items[0].description, 'Owner-confirmed chair')
        self.assertEqual(repaired.items[0].user_claims, ['This belongs to me'])
        self.assertTrue(repaired.items[0].excluded)
        self.assertTrue({'first review','second review'} <= set(repaired.items[0].review_reasons))
        self.assertTrue({'frames/f1.jpg','frames/f2.jpg'} <= set(repaired.items[0].evidence_refs))
        self.assertEqual(repaired.metrics['inventory_aliases'], {'older-alias':'old-first','old-repeat':'old-first'})
        self.assertEqual(repaired.pricing_jobs['old-repeat:new']['status'], 'unavailable')
        self.assertEqual(repaired.spatial_tracks['old-first']['frames'], ['f1','f2'])
        self.assertEqual(repaired.books, self.state.books)
        self.assertEqual(before, self.state.model_dump_json())
        self.assertEqual(files_before, {str(p):p.read_bytes() for p in Path(self.tmp.name).rglob('*') if p.is_file()})

    def test_two_same_category_objects_in_separate_positions_remain_distinct(self):
        self.observation('f1', [('left', LEFT), ('right', RIGHT)])
        self.observation('f2', [('left-repeat', LEFT), ('right-repeat', RIGHT)])
        repaired, report = repair_inventory(self.store, self.state)
        self.assertEqual([item.id for item in repaired.items], ['left','right'])
        self.assertEqual(repaired.metrics['inventory_aliases'], {'left-repeat':'left','right-repeat':'right'})
        self.assertEqual(report['merged_items'], 2)

    def test_ambiguous_old_identity_blocks_every_target_it_touches(self):
        self.observation('f1', [('ambiguous', LEFT)])
        self.observation('f2', [('ambiguous', RIGHT), ('repeat-a', LEFT)])
        self.observation('f3', [('repeat-b', LEFT)])
        repaired, report = repair_inventory(self.store, self.state)
        self.assertEqual([item.id for item in repaired.items], ['ambiguous','repeat-a','repeat-b'])
        self.assertIn('ambiguous', report['ambiguous_ids'])
        self.assertEqual(report['merged_items'], 0)

    def test_missing_observation_prevents_partial_history_merge(self):
        self.observation('f1', [('old-first', LEFT)])
        self.observation('f2', [('old-repeat', LEFT)])
        self.state.items[1].evidence_refs.append('frames/missing.jpg')
        repaired, report = repair_inventory(self.store, self.state)
        self.assertEqual(len(repaired.items), 2)
        self.assertIn('old-repeat', report['incomplete_ids'])

    def test_repeat_repair_is_idempotent_and_aliases_stay_direct(self):
        self.observation('f1', [('old-first', LEFT)])
        self.observation('f2', [('old-repeat', LEFT)])
        repaired, _ = repair_inventory(self.store, self.state)
        again, report = repair_inventory(self.store, repaired)
        self.assertEqual([item.id for item in again.items], ['old-first'])
        self.assertEqual(report['merged_items'], 0)
        self.assertEqual(again.metrics['inventory_aliases'], {'old-repeat':'old-first'})
        self.assertEqual(again.items[0], repaired.items[0])

    def test_owner_identity_change_clears_old_offer_without_requeue(self):
        self.observation('f1', [('old-first', LEFT)])
        self.observation('f2', [('old-repeat', LEFT)])
        self.state.items[0].replacement_cost = PriceRange(low='1000', high='1000', verified=True)
        self.state.items[0].status = 'priced'
        self.state.items[1].user_claims = ['This chair is solid wood']
        self.state.items[1].material = 'wood'
        self.state.pricing_jobs['old-first:new'] = {'id':'old-first', 'status':'completed'}
        repaired, _ = repair_inventory(self.store, self.state)
        self.assertEqual(repaired.items[0].material, 'wood')
        self.assertIsNone(repaired.items[0].replacement_cost.low)
        self.assertEqual(repaired.items[0].pricing_status, 'unavailable')
        self.assertEqual(repaired.pricing_jobs['old-first:new']['status'], 'unavailable')

    def test_raw_dimension_change_inside_price_bucket_clears_old_offer(self):
        self.observation('f1', [('old-first', LEFT)])
        item = self.state.items[0]
        item.dimensions_cm = Dimensions(w=62.1, h=130, d=50)
        item.replacement_cost = PriceRange(low='1000', high='1000', verified=True)
        original_fingerprint = pricing_fingerprint(item)
        geometry = {'ar_session_id':'test', 'floors':[], 'walls':[], 'objects':[
            {'id':'physical-chair', 'category':'chair', 'confidence':'high',
             'center':[-.6,.05,-2], 'dimensions':[.639,1.3,.5],
             'corners':[[x,y,z] for x in [-.9195,-.2805] for y in [-.6,.7] for z in [-2.25,-1.75]]}]}
        self.store.put_json(self.state, 'geometry/room.json', geometry)
        self.state.room.geometry_ref = 'geometry/room.json'
        self.state.geometry = geometry
        repaired, _ = repair_inventory(self.store, self.state)
        self.assertAlmostEqual(repaired.items[0].dimensions_cm.w, 63.9)
        self.assertEqual(pricing_fingerprint(repaired.items[0]), original_fingerprint)
        self.assertIsNone(repaired.items[0].replacement_cost.low)
        self.assertIn('geometry/room.json', repaired.items[0].evidence_refs)
