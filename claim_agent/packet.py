"""Only this module computes claim totals. Models cannot write them."""
import html
import json
import zipfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from .models import ClaimPacket, Price, PriceRange, Review, SweepState, Totals
from .storage import EvidenceStore, atomic_write


def valid_source(price, state):
    try:
        dated=datetime.fromisoformat(price.retrieved_at.replace("Z","+00:00"))
        if dated.tzinfo is None: return False
    except (ValueError,TypeError):
        return False
    if price.converted and (price.fx_source_ref not in state.evidence or not price.fx_rate or not price.original_currency or price.original_amount is None):
        return False
    return (price.verified and price.currency == state.sweep.currency and price.country==state.sweep.country
            and price.source_ref in state.evidence and price.source and price.retrieved_at
            and urlsplit(price.url).scheme in {"http", "https"})


def sanitize_price(price: Price, state: SweepState, eligible: bool) -> Price:
    if not eligible or not valid_source(price, state) or not price.condition_assumed:
        return Price(exclusion_reason=price.exclusion_reason or "Price lacks eligible source evidence")
    if price.converted and (price.fx_source_ref not in state.evidence or not price.fx_rate or not price.original_currency or price.original_amount is None):
        return Price(exclusion_reason="Currency conversion lacks FX provenance")
    return price.model_copy(deep=True)


def build_packet(state: SweepState) -> ClaimPacket:
    packet = ClaimPacket(sweep=state.sweep.model_copy(deep=True), room=state.room.model_copy(deep=True),
                         books=[b.model_copy(deep=True) for b in state.books],
                         items=[i.model_copy(deep=True) for i in state.items],
                         revision=state.revision, completion=state.completion, metrics=state.metrics.copy())
    reviews = {(state.sweep.id, reason) for reason in state.review_reasons}
    reviews.update(("room", reason) for reason in state.room.review_reasons)
    if state.room.floor_area_m2 is None or state.room.wall_area_m2 is None:
        reviews.add(("room", "Same-sweep metric room geometry is incomplete"))
    t = Totals(currency=state.sweep.currency, shelf_run_m=state.shelf_run_m)
    for b in packet.books:
        if b.frame_ref not in state.evidence:
            raise ValueError(f"Missing frame evidence for {b.id}")
        eligible = not b.excluded and b.status == "identified"
        b.replacement_cost = sanitize_price(b.replacement_cost, state, eligible)
        b.used_value = sanitize_price(b.used_value, state, eligible)
        reviews.update((b.id, reason) for reason in b.review_reasons)
        if b.excluded:
            t.ownership_excluded += 1
            continue
        t.book_count += 1
        if b.status == "identified":
            t.books_identified += 1
        elif b.status == "unidentified":
            t.books_unidentified += 1
            reviews.add((b.id, "Spine identity is unknown"))
        else:
            t.books_needs_appraisal += 1
            reviews.add((b.id, "Human appraisal required"))
        if b.spine_height_cm is None or b.spine_thickness_cm is None:
            reviews.add((b.id, "Metric spine measurement missing"))
        replacement, used = b.replacement_cost.amount, b.used_value.amount
        if replacement is None:
            t.replacement_prices_missing += 1
            reviews.add((b.id, "Replacement: " + b.replacement_cost.exclusion_reason))
        else:
            t.books_replacement_cost += replacement
        if used is None:
            t.used_prices_missing += 1
            reviews.add((b.id, "Used: " + b.used_value.exclusion_reason))
        else:
            t.books_used_value += used
        if replacement is None and used is None:
            t.excluded_from_totals += 1
    for item in packet.items:
        if item.frame_ref not in state.evidence:
            raise ValueError(f"Missing frame evidence for {item.id}")
        p = item.replacement_cost
        if (item.excluded or item.status not in {"priced", "range"} or not valid_source(p, state)
                or p.low is None or p.high is None or p.low > p.high):
            item.replacement_cost = PriceRange(exclusion_reason=p.exclusion_reason)
        reviews.update((item.id, reason) for reason in item.review_reasons)
        if item.excluded:
            t.ownership_excluded += 1
        elif item.replacement_cost.low is None:
            t.excluded_from_totals += 1
            reviews.add((item.id, "Appraisal required" if item.status == "needs_appraisal" else p.exclusion_reason))
        else:
            t.items_replacement_cost_low += item.replacement_cost.low
            t.items_replacement_cost_high += item.replacement_cost.high
    packet.totals = t
    packet.review_queue = [Review(ref_id=ref, reason=reason) for ref, reason in sorted(reviews)]
    return packet


def render_report(packet: ClaimPacket) -> str:
    # Rendering consumes the same serialization as claim_packet.json.
    p = json.loads(packet.model_dump_json())
    esc = lambda value: html.escape(str(value if value is not None else "Unknown"), quote=True)
    rows = []
    for b in p["books"]:
        rows.append(f'<tr><td>{esc(b["id"])}<br>{esc(b["shelf"])}</td><td>{esc(b["title"] or "Book")}<br>{esc(b["author"])}</td>'
                    f'<td>{esc(b["status"])}{" · excluded by owner" if b["excluded"] else ""}</td>'
                    f'<td>{esc(b["spine_height_cm"])} × {esc(b["spine_thickness_cm"])}</td>'
                    f'<td>{price_html(b["replacement_cost"], esc)}</td><td>{price_html(b["used_value"], esc)}</td>'
                    f'<td><a href="{esc(b["frame_ref"])}">Frame</a></td></tr>')
    item_rows = []
    for i in p["items"]:
        v = i["replacement_cost"]
        item_rows.append(f'<tr><td>{esc(i["id"])}</td><td>{esc(i["category"])}: {esc(i["description"])}</td>'
                         f'<td>{esc(i["status"])}</td><td>{price_html(v, esc)}</td>'
                         f'<td><a href="{esc(i["frame_ref"])}">Frame</a></td></tr>')
    review = "".join(f'<li><b>{esc(r["ref_id"])}</b>: {esc(r["reason"])}</li>' for r in p["review_queue"])
    room = "".join(f'<dt>{esc(k)}</dt><dd>{esc(v)}</dd>' for k, v in p["room"].items() if k not in {"floor_polygon_xz", "review_reasons"})
    totals = "".join(f'<dt>{esc(k)}</dt><dd>{esc(v)}</dd>' for k, v in p["totals"].items())
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Library contents claim</title><style>body{{font:16px system-ui;max-width:1200px;margin:36px auto;padding:16px;color:#17242d}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{border:1px solid #ccd5dc;padding:10px;text-align:left}}dt{{font-weight:600}}dd{{margin-bottom:8px}}.banner{{background:#fff3cf;padding:14px}}a{{color:#07578c}}@media print{{body{{margin:0}}}}</style>
<h1>Library contents claim</h1><p class="banner">{"SYNTHETIC TEST — not a real-room claim. " if p["sweep"]["synthetic"] else ""}Status: {esc(p["completion"])} · {esc(p["sweep"]["country"])} / {esc(p["sweep"]["currency"])} · revision {p["revision"]}</p>
<p>Sweep {esc(p["sweep"]["id"])} · captured {esc(p["sweep"]["captured_at"])}</p><a href="claim_packet.json">Structured JSON</a>
<h2>Totals</h2><dl>{totals}</dl><h2>Room measurements</h2><dl>{room}</dl>
<h2>Books</h2><table><thead><tr><th>ID / shelf</th><th>Identity</th><th>Status</th><th>Height × thickness cm</th><th>Replacement</th><th>Used</th><th>Evidence</th></tr></thead><tbody>{"".join(rows)}</tbody></table>
<h2>Other contents</h2><table><tr><th>ID</th><th>Object</th><th>Status</th><th>Replacement range</th><th>Evidence</th></tr>{"".join(item_rows)}</table>
<h2>Review queue</h2><ul>{review}</ul><h2>Assumptions</h2><ul>{"".join("<li>"+esc(a)+"</li>" for a in p["assumptions"])}</ul>
<h2>Timing and provider usage</h2><pre>{esc(json.dumps(p["metrics"], indent=2))}</pre></html>'''


def source_html(p, esc):
    if not p["url"] or urlsplit(p["url"]).scheme not in {"http", "https"}:
        return ""
    return f'<br><a href="{esc(p["url"])}" rel="noreferrer">{esc(p["source"])}</a><br>{esc(p["retrieved_at"])}'


def price_html(p, esc):
    amount=p.get("amount",p.get("low"))
    if amount is None:
        return "Unknown — " + esc(p["exclusion_reason"])
    value=esc(amount)
    if p.get("high") is not None and p["high"]!=amount:
        value+=" – "+esc(p["high"])
    label="Exact identifier match" if p.get("match")=="exact" else "Estimated equivalent" if p.get("match")=="equivalent" else "Sourced estimate; identity unverified"
    basis="<br>"+esc(p["match_basis"]) if p.get("match_basis") else ""
    product="<br>"+esc(p["product_name"]) if p.get("product_name") else ""
    return f'{value} {esc(p["currency"])} · {label}{product}{basis}<br>{esc(p["condition_assumed"])}{" · converted" if p["converted"] else ""}{source_html(p, esc)}'


def write_bundle(state: SweepState, store: EvidenceStore) -> Path:
    packet = build_packet(state)
    directory = store.directory(state.sweep.id)
    payload = packet.model_dump_json(indent=2).encode()
    report = render_report(packet).encode()
    atomic_write(directory / "claim_packet.json", payload)
    atomic_write(directory / "report.html", report)
    manifest = json.dumps({k: v.model_dump() for k, v in state.evidence.items()}, indent=2).encode()
    archive = directory / ".packet.zip.pending"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("claim_packet.json", payload)
        z.writestr("report.html", report)
        z.writestr("manifest.json", manifest)
        for ref in sorted(state.evidence):
            z.writestr(ref, store.read_evidence(state, ref))
    archive.replace(directory / "packet.zip")
    return directory / "packet.zip"
