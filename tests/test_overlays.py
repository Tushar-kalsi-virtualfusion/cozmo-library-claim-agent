import tempfile
import unittest
from pathlib import Path

import numpy as np

from claim_agent.config import Settings
from claim_agent.pipeline import SweepRunner
from claim_agent.storage import EvidenceStore
from claim_agent.vision import Detection, Scan
from tests.test_geometry import frame


class OverlayTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = EvidenceStore(Path(temp.name))
        self.state = self.store.create('test', 'India', 'INR', synthetic=True)
        self.runner = SweepRunner(self.state, self.store, Settings(), None, None)

    def scan(self, *, depth=False, title=''):
        meta = frame()
        meta.ocr = [{"text":title,"center":[.457,.5]}] if title else []
        if depth:
            self.store.put_evidence(self.state, 'depth/f1.bin', np.full((100, 100), 2, dtype='<f4').tobytes())
            self.store.put_evidence(self.state, 'depth/f1.confidence', bytes([2]) * 10000)
        polygon = [[.45, .45], [.465, .45], [.465, .55], [.45, .55]]
        return self.runner.apply_scan(meta, Scan(detections=[
            Detection(kind='book', polygon=polygon, title=title, visible_text=title, confidence=.9),
            Detection(kind='item', category='Desk lamp', polygon=[[.3,.3],[.4,.3],[.4,.5],[.3,.5]]),
            Detection(kind='shelf', polygon=[[.2,.2],[.8,.2],[.8,.8],[.2,.8]])]))

    def test_frame_overlay_labels_include_book_category_without_inventing_identity(self):
        event = self.scan()
        self.assertIsInstance(event, dict)
        self.assertEqual(event['type'], 'frame_overlays')
        self.assertEqual(event['frame_id'], 'f1')
        self.assertEqual(event['ar_session_id'], 'one-session')
        self.assertEqual(event['timestamp_s'], 1)
        self.assertEqual([d['label'] for d in event['detections']], ['Book', 'Desk lamp'])
        self.assertEqual(event['detections'][0]['category'], 'Book')
        self.assertEqual(len(event['detections'][1]['polygon']), 4)

    def test_depth_dimensions_are_explicitly_spine_or_front_face_not_object_depth(self):
        event = self.scan(depth=True, title='Dune')
        self.assertIsInstance(event, dict)
        book, lamp = event['detections']
        self.assertEqual(book['label'], 'Dune')
        self.assertAlmostEqual(book['dimensions']['spine_length_cm'], 20)
        self.assertAlmostEqual(book['dimensions']['spine_thickness_cm'], 3)
        self.assertEqual(len(book['world_corners']), 4)
        self.assertAlmostEqual(lamp['dimensions']['face_long_cm'], 40)
        self.assertAlmostEqual(lamp['dimensions']['face_short_cm'], 20)
        self.assertNotIn('d', lamp['dimensions'])
        self.assertIn('LiDAR', lamp['measurement_source'])
        self.assertIsNone(self.state.items[0].dimensions_cm.d)

    def test_camera_scale_is_missing_instead_of_estimated_from_identity(self):
        event = self.scan(title='Dune')
        self.assertIsInstance(event, dict)
        for detection in event['detections']:
            self.assertEqual(detection['world_corners'], [])
            self.assertTrue(all(v is None for v in detection['dimensions'].values()))
            self.assertEqual(detection['measurement_source'], '')

    def test_repeat_scan_uses_current_polygon_even_when_best_identity_is_older(self):
        self.scan(depth=True, title='Dune')
        polygon = [[.451,.45],[.466,.45],[.466,.55],[.451,.55]]
        self.store.put_evidence(self.state, 'depth/f2.bin', self.store.read_evidence(self.state, 'depth/f1.bin'))
        self.store.put_evidence(self.state, 'depth/f2.confidence', bytes([2]) * 10000)
        event = self.runner.apply_scan(frame().model_copy(update={'id':'f2'}), Scan(detections=[Detection(kind='book', polygon=polygon)]))
        self.assertIsInstance(event, dict)
        self.assertEqual(event['detections'][0]['polygon'], polygon)
        self.assertEqual(event['detections'][0]['label'], 'Dune')
