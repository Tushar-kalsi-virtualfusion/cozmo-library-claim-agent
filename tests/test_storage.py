import tempfile
import unittest
from pathlib import Path

from claim_agent.storage import EvidenceStore


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = EvidenceStore(Path(self.tmp.name))
        self.state = self.store.create("iPhone 17 Pro", "India", "INR")

    def test_retry_is_idempotent_but_cannot_replace_evidence(self):
        a = self.store.put_evidence(self.state, "frames/f1.jpg", b"original")
        b = self.store.put_evidence(self.state, "frames/f1.jpg", b"original")
        self.assertEqual(a, b)
        with self.assertRaises(ValueError):
            self.store.put_evidence(self.state, "frames/f1.jpg", b"replacement")
        self.assertEqual(self.store.read_evidence(self.state, "frames/f1.jpg"), b"original")

    def test_paths_cannot_escape_a_sweep(self):
        for path in ["../secret", "/tmp/test", "frames/../../secret", "frames/a?b", "frames/a\\b"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.store.put_evidence(self.state, path, b"bad")
        with self.assertRaises(ValueError):
            self.store.load("../../secret")

    def test_restart_retains_revision_and_evidence_manifest(self):
        self.store.put_evidence(self.state, "frames/f1.jpg", b"image")
        self.state.revision = 8
        self.store.save(self.state)
        recovered = EvidenceStore(Path(self.tmp.name)).load(self.state.sweep.id)
        self.assertEqual(recovered.revision, 8)
        self.assertEqual(recovered.evidence["frames/f1.jpg"].size, 5)
        self.assertEqual(recovered.sweep.currency, "INR")

    def test_corrupted_evidence_is_detected(self):
        self.store.put_evidence(self.state, "frames/f1.jpg", b"image")
        (self.store.directory(self.state.sweep.id) / "frames/f1.jpg").write_bytes(b"tamper")
        with self.assertRaises(ValueError):
            self.store.read_evidence(self.state, "frames/f1.jpg")

    def test_images_share_bytes_across_sweeps_but_keep_separate_manifests(self):
        other = self.store.create("phone", "India", "INR")
        self.store.put_evidence(self.state,"frames/f1.jpg",b"same jpeg")
        self.store.put_evidence(other,"frames/f2.jpg",b"same jpeg")
        first = self.store.evidence_path(self.state.sweep.id,"frames/f1.jpg")
        second = self.store.evidence_path(other.sweep.id,"frames/f2.jpg")
        self.assertEqual(first.stat().st_ino,second.stat().st_ino)
        self.assertNotIn("frames/f2.jpg",self.state.evidence)
