import json
import tempfile
import unittest
import zipfile
from decimal import Decimal
from pathlib import Path

from claim_agent.models import Book, Item, Price
from claim_agent.packet import build_packet, write_bundle
from claim_agent.storage import EvidenceStore


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = EvidenceStore(Path(self.tmp.name))
        self.state = self.store.create("test", "India", "INR")
        self.store.put_evidence(self.state, "frames/f1.jpg", b"exact-frame")
        self.store.put_evidence(self.state, "sources/s1.json", b'{"price":"0.10 INR"}')

    def price(self, amount, **kw):
        return Price(amount=Decimal(amount), currency="INR", country="India", source="Shop", url="https://example.com/book",
                     retrieved_at="2026-10-17T10:00:00Z", condition_assumed="new",
                     source_ref="sources/s1.json", verified=True, **kw)

    def book(self, id, **kw):
        return Book(id=id, frame_ref="frames/f1.jpg", title="Visible title", status="identified", **kw)

    def test_decimal_totals_and_independent_missing_used_value(self):
        self.state.books = [self.book("b1", replacement_cost=self.price("0.10")),
                            self.book("b2", replacement_cost=self.price("0.20"))]
        p = build_packet(self.state)
        self.assertEqual(p.totals.books_replacement_cost, Decimal("0.30"))
        self.assertEqual(p.totals.books_used_value, Decimal("0"))
        self.assertEqual(p.totals.excluded_from_totals, 0)
        self.assertEqual(p.totals.used_prices_missing, 2)
        self.assertIsNone(p.books[0].spine_height_cm)

    def test_unbacked_foreign_and_appraisal_prices_never_contribute(self):
        unbacked = self.price("42").model_copy(update={"source_ref": "sources/missing.json"})
        foreign = self.price("99").model_copy(update={"currency": "GBP"})
        self.state.books = [self.book("b1", replacement_cost=unbacked), self.book("b2", replacement_cost=foreign),
                            Book(id="b3", frame_ref="frames/f1.jpg", status="needs_appraisal", replacement_cost=self.price("100"))]
        p = build_packet(self.state)
        self.assertEqual(p.totals.books_replacement_cost, 0)
        self.assertEqual(p.totals.excluded_from_totals, 3)
        self.assertEqual(p.totals.books_needs_appraisal, 1)
        self.assertIsNone(p.books[0].replacement_cost.amount)

    def test_unidentified_and_excluded_lines_cannot_contribute(self):
        self.state.books = [Book(id="b1", frame_ref="frames/f1.jpg", replacement_cost=self.price("30")),
                            self.book("b2", excluded=True, replacement_cost=self.price("40"))]
        p = build_packet(self.state)
        self.assertEqual(p.totals.books_replacement_cost, 0)
        self.assertEqual(p.totals.book_count, 1)
        self.assertEqual(p.totals.ownership_excluded, 1)

    def test_missing_frame_prevents_an_untraceable_packet(self):
        self.state.books = [Book(id="b1", frame_ref="frames/missing.jpg")]
        with self.assertRaisesRegex(ValueError, "frame"):
            build_packet(self.state)

    def test_bundle_uses_exact_json_escapes_html_and_includes_evidence(self):
        self.state.books = [self.book("b1", replacement_cost=self.price("0.10"))]
        self.state.books[0].title = '<script>alert("x")</script>'
        path = write_bundle(self.state, self.store)
        with zipfile.ZipFile(path) as z:
            data = json.loads(z.read("claim_packet.json"))
            html = z.read("report.html").decode()
            self.assertEqual(z.read("frames/f1.jpg"), b"exact-frame")
            self.assertEqual(data["totals"]["books_replacement_cost"], "0.10")
            self.assertIn("&lt;script&gt;", html)
            self.assertNotIn('<script>alert(', html)
            self.assertIn('href="frames/f1.jpg"', html)
            for key in ["sweep", "room", "books", "items", "totals", "review_queue"]:
                self.assertIn(key, data)

    def test_negative_price_is_rejected(self):
        with self.assertRaises(ValueError):
            Price(amount="-1")

    def test_source_market_and_date_must_be_valid(self):
        foreign=self.price("20").model_copy(update={"country":"United Kingdom"})
        bad_date=self.price("30").model_copy(update={"retrieved_at":"yesterday"})
        self.state.books=[self.book("b1",replacement_cost=foreign),self.book("b2",replacement_cost=bad_date)]
        self.assertEqual(build_packet(self.state).totals.books_replacement_cost,0)

    def test_report_labels_sourced_equivalent_and_escapes_match_basis(self):
        from claim_agent.models import Item, PriceRange
        from claim_agent.packet import render_report
        p=self.price('250').model_copy(update={'match':'equivalent','match_basis':'Matching <wood> material',
                                               'product_name':'Retailer chair'})
        self.state.items=[Item(id='i1',category='chair',frame_ref='frames/f1.jpg',status='range',
            replacement_cost=PriceRange(low='250',high='250',**p.model_dump(exclude={'amount'})))]
        report=render_report(build_packet(self.state))
        self.assertIn('Estimated equivalent',report)
        self.assertIn('Matching &lt;wood&gt; material',report)
        self.assertIn('Retailer chair',report)
        self.assertNotIn('250 – 250',report)
