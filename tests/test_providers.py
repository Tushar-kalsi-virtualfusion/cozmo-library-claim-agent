import unittest

from claim_agent.providers import public_url
from claim_agent.vision import Detection, supported_identity


class ProviderTests(unittest.TestCase):
    def test_unreadable_spine_does_not_acquire_a_catalogue_identity(self):
        d = Detection(kind="book", polygon=[[.1,.1],[.2,.1],[.2,.5],[.1,.5]],
                      title="Invisible Garden", visible_text="", confidence=.99)
        self.assertEqual(supported_identity(d)["title"], "")

    def test_edition_is_only_kept_when_visible(self):
        d = Detection(kind="book", polygon=[[.1,.1],[.2,.1],[.2,.5],[.1,.5]],
                      title="Hidden Garden", visible_text="Hidden Garden A Writer", author="A Writer",
                      edition="First edition", isbn="9780141234567", confidence=.9)
        result = supported_identity(d)
        self.assertEqual(result["title"], "Hidden Garden")
        self.assertEqual(result["edition"], "")
        self.assertEqual(result["isbn"], "")

    def test_independent_ocr_disagreement_prevents_confident_acceptance(self):
        d = Detection(kind="book", polygon=[[.1,.1],[.2,.1],[.2,.5],[.1,.5]],
                      title="Hidden Garden", visible_text="Hidden Garden", confidence=.99)
        result = supported_identity(d, "Different title")
        self.assertEqual(result["title"], "")

    def test_invalid_boxes_rejected(self):
        with self.assertRaises(ValueError):
            Detection(kind="book", polygon=[[-.1,.1],[.2,.1],[.2,.5],[.1,.5]])

    def test_private_and_credential_urls_rejected_before_network(self):
        for url in ["http://127.0.0.1/a", "http://192.168.1.2/a", "http://169.254.169.254/a",
                    "file:///etc/passwd", "https://name:secret@example.com/a", "http://[::1]/"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                public_url(url, resolve=False)
        self.assertEqual(public_url("https://shop.example.com/book", resolve=False), "shop.example.com")
