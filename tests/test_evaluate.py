import unittest
from copy import deepcopy

from claim_agent.evaluate import evaluate


def fixture():
    books=[{"id":f"b{i}","title":f"Title {i}","status":"identified","id_confidence":.9,
            "spine_height_cm":23,"spine_thickness_cm":2.3,
            "replacement_cost":{"amount":"125","currency":"INR","condition_assumed":"new","source_type":"retailer_offer"},
            "excluded":False} for i in range(20)]
    packet={"sweep":{"country":"India","currency":"INR","ended_at":"2026-10-17T10:00:00Z","packet_ready_at":"2026-10-17T10:04:59Z"},
            "books":books,"items":[{"id":"i1","category":"chair","excluded":False}],
            "room":{"floor_area_m2":11,"wall_area_m2":23},"metrics":{}}
    truth={"frozen_at":"2026-10-17T09:00:00Z","book_count":20,
           "books":[{"id":f"t{i}","predicted_id":f"b{i}","legible":True,"title":f"Title {i}"} for i in range(20)],
           "identity_checks":[],
           "spines":[{"predicted_id":f"b{i}","height_cm":20,"thickness_cm":2} for i in range(20)],
           "prices":[{"predicted_id":f"b{i}","kind":"replacement_cost","amount":"100","currency":"INR","country":"India","condition_assumed":"new","source_type":"retailer_offer"} for i in range(15)],
           "items":[{"id":"t1","predicted_id":"i1","category":"chair"}],
           "room":{"floor_area_m2":10,"wall_area_m2":20}}
    return packet,truth


class EvaluationTests(unittest.TestCase):
    def test_exact_error_boundaries_pass_but_latency_is_strictly_under_300(self):
        p,t=fixture()
        result=evaluate(p,t)
        for key in ["book_count","title_identification","spine_dimensions","book_prices","non_book_items","floor_area","wall_area","time_to_packet"]:
            self.assertTrue(result["metrics"][key]["passed"],key)
        p["sweep"]["packet_ready_at"]="2026-10-17T10:05:00Z"
        self.assertFalse(evaluate(p,t)["metrics"]["time_to_packet"]["passed"])

    def test_missing_sample_values_fail_coverage_instead_of_disappearing(self):
        p,t=fixture()
        p["books"][0]["spine_height_cm"]=None
        p["books"][1]["replacement_cost"]["amount"]=None
        metrics=evaluate(p,t)["metrics"]
        self.assertFalse(metrics["spine_dimensions"]["passed"])
        self.assertEqual(metrics["spine_dimensions"]["covered"],19)
        self.assertFalse(metrics["book_prices"]["passed"])
        self.assertEqual(metrics["book_prices"]["covered"],14)

    def test_all_legible_truth_stays_in_recall_denominator(self):
        p,t=fixture()
        p["books"]=p["books"][:10]
        metric=evaluate(p,t)["metrics"]["title_identification"]
        self.assertEqual(metric["recall"],.5)
        self.assertFalse(metric["passed"])

    def test_confidently_wrong_uses_stricter_high_confidence_denominator(self):
        p,t=fixture()
        p["books"][0]["title"]="Confidently wrong"
        for b in p["books"][10:]: b["id_confidence"] = .8
        metric=evaluate(p,t)["metrics"]["title_identification"]
        self.assertEqual(metric["wrong_fraction_all"],.05)
        self.assertEqual(metric["wrong_fraction_high_confidence"],.1)
        self.assertFalse(metric["passed"])

    def test_unverified_confident_prediction_does_not_count_as_correct(self):
        p,t=fixture()
        t["books"][0]["legible"]=False
        t["books"][0]["title"]=""
        metric=evaluate(p,t)["metrics"]["title_identification"]
        self.assertEqual(metric["unverified_high_confidence"],1)
        self.assertFalse(metric["passed"])

    def test_skipped_objects_remain_in_ground_truth_denominator(self):
        p,t=fixture()
        p["items"][0]["excluded"]=True
        metric=evaluate(p,t)["metrics"]["non_book_items"]
        self.assertEqual(metric["recall"],0)
        self.assertFalse(metric["passed"])

    def test_missing_timing_is_unverified_and_mismatched_prices_not_compared(self):
        p,t=fixture()
        p["sweep"]["packet_ready_at"]=None
        t["prices"][0]["condition_assumed"]="used"
        result=evaluate(p,t)["metrics"]
        self.assertIsNone(result["time_to_packet"]["passed"])
        self.assertEqual(result["book_prices"]["covered"],14)
        self.assertFalse(result["book_prices"]["passed"])
