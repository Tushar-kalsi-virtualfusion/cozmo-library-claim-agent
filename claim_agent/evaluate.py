"""Offline evaluation. This module is never imported by the runtime service."""
import argparse
import json
import re
import statistics
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

HIGH_CONFIDENCE = Decimal("0.85")


def decimal(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (InvalidOperation,ValueError,TypeError):
        return None


def relative_error(predicted, actual):
    p,t = decimal(predicted),decimal(actual)
    return abs(p-t)/t if p is not None and t is not None and t>0 else None


def norm(text):
    return " ".join(re.findall(r"\w+",str(text).casefold()))


def error_metric(predicted,actual,threshold):
    error=relative_error(predicted,actual)
    return {"predicted":predicted,"truth":actual,"relative_error":float(error) if error is not None else None,
            "maximum_error":threshold,"passed":error<=Decimal(str(threshold)) if error is not None else None}


def evaluate(packet:dict,truth:dict) -> dict:
    books=[b for b in packet.get("books",[]) if not b.get("excluded")]
    items=[i for i in packet.get("items",[]) if not i.get("excluded")]
    if len({b["id"] for b in books})!=len(books) or len({i["id"] for i in items})!=len(items):
        raise ValueError("Duplicate predicted physical IDs make evaluation invalid")
    by_id={b["id"]:b for b in books}
    item_by_id={i["id"]:i for i in items}
    truth_books=truth.get("books",[])
    matches=[b["predicted_id"] for b in truth_books if b.get("predicted_id")]
    if len(set(matches))!=len(matches):
        raise ValueError("One predicted physical book cannot match two truth books")
    metrics={"book_count":error_metric(len(books),truth.get("book_count"),.05)}
    legible=[b for b in truth_books if b.get("legible")]
    identity={}
    correct=0
    for row in truth_books:
        p=by_id.get(row.get("predicted_id"))
        if p and row.get("title"):
            accepted={norm(row["title"])} | {norm(v) for v in row.get("acceptable_titles",[])}
            is_correct=bool(p.get("title")) and norm(p["title"]) in accepted
            identity[p["id"]]=is_correct
            if row.get("legible") and is_correct:
                correct+=1
    for row in truth.get("identity_checks",[]):
        if row.get("correct") in [True,False] and row.get("predicted_id"):
            identity[row["predicted_id"]]=row["correct"]
    high=[b for b in books if (decimal(b.get("id_confidence")) or 0)>=HIGH_CONFIDENCE and b.get("title")]
    wrong=sum(identity.get(b["id"]) is False for b in high)
    unverified=sum(b["id"] not in identity for b in high)
    recall=correct/len(legible) if legible else None
    all_fraction=wrong/len(books) if books else 0
    high_fraction=wrong/len(high) if high else 0
    metrics["title_identification"]={"correct_legible":correct,"legible_truth":len(legible),"recall":recall,
        "minimum_recall":.7,"high_confidence_threshold":str(HIGH_CONFIDENCE),"confidently_wrong":wrong,
        "high_confidence_predictions":len(high),"wrong_fraction_all":all_fraction,
        "wrong_fraction_high_confidence":high_fraction,"unverified_high_confidence":unverified,
        "maximum_wrong_fraction":.03,
        "passed":recall>=.7 and max(all_fraction,high_fraction)<=.03 and unverified==0 if recall is not None else None}
    spine_rows=[]
    for row in truth.get("spines",[]):
        p=by_id.get(row.get("predicted_id"),{})
        h=relative_error(p.get("spine_height_cm"),row.get("height_cm"))
        w=relative_error(p.get("spine_thickness_cm"),row.get("thickness_cm"))
        spine_rows.append({"predicted_id":row.get("predicted_id"),"height_error":float(h) if h is not None else None,
                           "thickness_error":float(w) if w is not None else None,
                           "passed":h is not None and w is not None and h<=Decimal(".15") and w<=Decimal(".15")})
    covered=sum(r["height_error"] is not None and r["thickness_error"] is not None for r in spine_rows)
    errors=[r[k] for r in spine_rows for k in ["height_error","thickness_error"] if r[k] is not None]
    metrics["spine_dimensions"]={"required":20,"selected":len(spine_rows),"covered":covered,"maximum_error":.15,
        "mean_error":statistics.mean(errors) if errors else None,"median_error":statistics.median(errors) if errors else None,
        "worst_error":max(errors) if errors else None,"passed":len(spine_rows)==20 and covered==20 and all(r["passed"] for r in spine_rows)}
    price_rows=[]
    for row in truth.get("prices",[]):
        p=by_id.get(row.get("predicted_id"),{}).get(row.get("kind", "replacement_cost"),{})
        same=all(p.get(k)==row.get(k) and bool(row.get(k)) for k in ["currency","condition_assumed","source_type"])
        same=same and packet.get("sweep",{}).get("country")==row.get("country")
        error=relative_error(p.get("amount"),row.get("amount")) if same else None
        price_rows.append({"predicted_id":row.get("predicted_id"),"kind":row.get("kind"),"comparable":same,
                           "relative_error":float(error) if error is not None else None,
                           "passed":error is not None and error<=Decimal(".25")})
    covered=sum(row["relative_error"] is not None for row in price_rows)
    metrics["book_prices"]={"required":15,"selected":len(price_rows),"covered":covered,"maximum_error":.25,
                            "passed":len(price_rows)==15 and covered==15 and all(row["passed"] for row in price_rows)}
    item_matches=[]
    for row in truth.get("items",[]):
        p=item_by_id.get(row.get("predicted_id"))
        if p and norm(p.get("category","")) in {norm(row["category"])} | {norm(a) for a in row.get("acceptable_categories",[])}:
            item_matches.append(p["id"])
    if len(set(item_matches))!=len(item_matches):
        raise ValueError("One predicted object cannot match two truth objects")
    actual_items=truth.get("items",[])
    object_recall=len(item_matches)/len(actual_items) if actual_items else None
    metrics["non_book_items"]={"found_correct_category":len(item_matches),"truth_count":len(actual_items),
        "recall":object_recall,"false_positives":len(items)-len(item_matches),"minimum_recall":.8,
        "passed":object_recall>=.8 if object_recall is not None else None}
    for key,field,threshold in [("floor_area","floor_area_m2",.1),("wall_area","wall_area_m2",.15)]:
        metrics[key]=error_metric(packet.get("room",{}).get(field),truth.get("room",{}).get(field),threshold)
    elapsed=None
    try:
        sweep=packet["sweep"]
        elapsed=(datetime.fromisoformat(sweep["packet_ready_at"].replace("Z","+00:00"))-
                 datetime.fromisoformat(sweep["ended_at"].replace("Z","+00:00"))).total_seconds()
        if elapsed<0: elapsed=None
    except (KeyError,TypeError,ValueError,AttributeError):
        pass
    metrics["time_to_packet"]={"seconds":elapsed,"strict_upper_limit_s":300,"passed":elapsed<300 if elapsed is not None else None}
    return {"synthetic":packet.get("sweep",{}).get("synthetic",False),"ground_truth_frozen_at":truth.get("frozen_at"),
            "ground_truth_frozen":bool(truth.get("frozen_at")),"metrics":metrics,"spine_rows":spine_rows,"price_rows":price_rows,
            "all_pass_bars_met":bool(truth.get("frozen_at")) and all(m["passed"] is True for m in metrics.values()),
            "notes":["All legible truth books remain in recall; all selected spine/price rows remain in coverage.",
                     "Conservative dimension/price gate: every selected row must be present and within tolerance.",
                     "Room wall comparison uses gross wall area. Evaluation truth is not loaded by the runtime.",
                     "Physical matches, legibility, acceptable aliases and independent identity checks must be established by a human."]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet",type=Path,required=True)
    parser.add_argument("--truth",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    output=evaluate(json.loads(args.packet.read_text()),json.loads(args.truth.read_text()))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(output,indent=2,allow_nan=False))
    rows=["# Assessment evaluation", "", "SYNTHETIC TEST — not real-room accuracy." if output["synthetic"] else "Results against independent ground truth.",
          "", "| Measure | Result | Details |", "|---|---|---|"]
    for name,value in output["metrics"].items():
        status="PASS" if value["passed"] is True else "FAIL" if value["passed"] is False else "UNVERIFIED"
        detail=json.dumps({k:v for k,v in value.items() if k!="passed"})
        rows.append(f"| {name} | {status} | {detail} |")
    args.output.with_suffix(".md").write_text("\n".join(rows)+"\n")
    print(f"Evaluation written to {args.output}; all bars met: {output['all_pass_bars_met']}")


if __name__=="__main__":
    main()
