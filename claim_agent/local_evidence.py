"""Validate phone clues against separate OCR and detector evidence before routing.

The phone SLM supplies suggestions, never authority. Catalogue records establish
work candidates only; their edition identifiers never become observed ISBNs.
"""
import asyncio
import hashlib
import json
import re

from shapely.geometry import Point, Polygon

from .vision import Detection, Scan, catalogue_candidates, normalized
from .image_selection import recognition_view_changed


def text_in_outline(metadata, outline):
    """Only bounded, well-formed independent OCR inside this object is usable."""
    region = Polygon(outline).buffer(.012)
    result = []
    for span in metadata.ocr:
        text, center = span.get("text"), span.get("center")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            continue
        if not isinstance(center, list) or len(center) != 2:
            continue
        if any(isinstance(n, bool) or not isinstance(n, (float, int)) or not 0 <= n <= 1 for n in center):
            continue
        confidence = span.get("confidence", 1)
        if isinstance(confidence, bool) or not isinstance(confidence, (float, int)) or not .5 <= confidence <= 1:
            continue
        if region.covers(Point(center)):
            result.append(text.strip())
    return " ".join(result)[:12000]


def supported_text(value, text):
    words = normalized(value or "")
    return bool(words) and f" {words} " in f" {normalized(text)} "


def valid_isbn(value, text):
    isbn = re.sub(r"[\s-]", "", value or "").upper()
    observed = re.sub(r"[\s-]", "", text).upper()
    if not isbn or isbn not in observed:
        return ""
    if re.fullmatch(r"\d{13}", isbn) and sum(int(n)*(1 if i % 2 == 0 else 3) for i,n in enumerate(isbn)) % 10 == 0:
        return isbn
    if re.fullmatch(r"\d{9}[\dX]", isbn) and sum((10-i)*(10 if n == "X" else int(n)) for i,n in enumerate(isbn)) % 11 == 0:
        return isbn
    return ""


def overlap(first, second):
    a, b = Polygon(first), Polygon(second)
    return a.intersection(b).area / a.union(b).area


async def lookup_catalogue(title, author):
    try:
        async with asyncio.timeout(3):
            return await catalogue_candidates(title, author)
    except (TimeoutError, ValueError, OSError):
        return []


async def resolve_local(metadata, catalogue_cache):
    analysis = metadata.local_analysis
    reasons, provisional, detections, catalogues, unresolved = [], [], [], {}, []
    if analysis is None or analysis.status != "complete":
        reasons.append("Local analysis missing" if analysis is None else f"Local analysis {analysis.status}")
    proposals = []
    for raw in metadata.local_recognition.detections if metadata.local_recognition else []:
        try:
            validated = Detection(kind="item",category=raw.label, polygon=raw.polygon, confidence=raw.confidence)
            proposals.append(validated)
        except ValueError:
            reasons.append("Invalid detector outline ignored")
    clues = analysis.books if analysis and analysis.status in {"complete","partial"} else []
    objects = analysis.objects if analysis and analysis.status in {"complete","partial"} else []
    if analysis and analysis.status != "complete":
        # A failed/missing local capability cannot inspect the scene at all.
        # This explicit coverage gap is eligible only after the independent
        # image quality/novelty checks and shared spending bounds below.
        unresolved.append("local_coverage_incomplete")
        reasons.append("Local coverage incomplete; scene requires independent inspection")
    used = set()
    lookups = 0
    unresolved_objects = False
    recapture_needed = False
    for proposal in proposals:
        if proposal.confidence < .65:
            continue
        provisional.append({"category":proposal.category, "polygon":proposal.polygon,
                            "confidence":proposal.confidence, "status":"provisional"})
        clue = next((c for i,c in enumerate(clues) if i not in used and overlap(c.polygon,proposal.polygon) >= .5), None)
        if clue:
            used.add(clues.index(clue))
        else:
            # The on-device semantic pass filters physical entities, without a
            # class whitelist. Its category must still match a real detector
            # proposal, and the cloud must adjudicate before inventory exists.
            physical = next((c for c in objects if c.confidence >= .65 and normalized(c.category) == normalized(proposal.category)
                             and overlap(c.polygon,proposal.polygon) >= .5), None)
            if physical:
                unresolved.append("object:" + normalized(physical.category)+":"+normalized(text_in_outline(metadata,proposal.polygon)))
                unresolved_objects = True
            continue
        text = text_in_outline(metadata, proposal.polygon)
        # A recognized shape without text merits a closer view, not a paid guess.
        if len(normalized(text)) < 3:
            reasons.append("Book outline has no readable independent OCR; request a closer view")
            recapture_needed = True
            continue
        title = clue.title if clue and supported_text(clue.title, text) and supported_text(clue.title, clue.visible_text) and clue.confidence >= .75 else ""
        author = clue.author if clue and supported_text(clue.author, text) and supported_text(clue.author, clue.visible_text) else ""
        isbn = valid_isbn(clue.isbn, text) if clue and supported_text(clue.visible_text, text) else ""
        key = normalized(title or text[:500]) + "|" + normalized(author or "")
        if key not in catalogue_cache and len(catalogue_cache) < 40 and lookups < 4:
            # Cache the in-flight task, not a false negative result. All workers
            # await the same bounded free lookup before considering paid vision.
            catalogue_cache[key] = asyncio.create_task(lookup_catalogue(title or text[:500],author or ""))
            lookups += 1
        pending = catalogue_cache.get(key)
        candidates = await asyncio.shield(pending) if pending is not None else []
        matching = [c for c in candidates if supported_text(c.get("title", ""), text)
                    and (not title or normalized(c.get("title", "")) == normalized(title))
                    and any(supported_text(a, text) for a in c.get("authors", []))]
        works = {(normalized(c["title"]), tuple(normalized(a) for a in c.get("authors", []) if supported_text(a,text))) for c in matching}
        if len(works) == 1:
            candidate = matching[0]
            title = candidate["title"]
            author = next((a for a in candidate.get("authors", []) if supported_text(a,text)), "")
        # Local model title-only guesses are not corroboration. Work identity
        # needs visible title+author, or a unique catalogue match to both.
        if title and author and (matching or (clue and clue.confidence >= .9)):
            # This is an uncalibrated server evidence score, not a probability
            # or the raw shape detector score. Exact independent title+author
            # corroboration by one catalogue work supports identity acceptance.
            evidence_confidence=max(clue.confidence if clue else 0,.85 if len(works)==1 else 0)
            d = Detection(kind="book", polygon=proposal.polygon, visible_text=text,
                          title=title, author=author, isbn=isbn, confidence=min(evidence_confidence,.95))
            detections.append(d)
            catalogues[normalized(title)+"|"+normalized(author)] = candidates
        else:
            unresolved.append("book:" + normalized(text))
            reasons.append("Book identity is unresolved by independent OCR and catalogue evidence")
    if analysis and analysis.books and not proposals:
        reasons.append("Local book clues lack supported detector polygons")
    if not detections and not unresolved:
        reasons.append("No independently supported inventory identity; local proposals remain provisional")
    if unresolved_objects:
        reasons.append("Local object categories are provisional; identities and valuations require review")
    signature = hashlib.sha256(json.dumps(sorted(set(unresolved))).encode()).hexdigest() if unresolved else ""
    return {"scan":Scan(detections=detections), "reasons":list(dict.fromkeys(reasons)),
            "provisional_objects":provisional, "unresolved":bool(unresolved), "signature":signature,
            "unresolved_objects":unresolved_objects,
            "catalogues":catalogues, "complete":bool(analysis and analysis.status == "complete" and not unresolved and detections and not recapture_needed)}


def cloud_decision(result, quality, metrics, settings, provider_available, now, metadata=None, image=None):
    """Reserve budget synchronously before any await; the sweep persists it."""
    state = metrics.setdefault("cloud_routing", {"attempts":0,"last_attempt_at":0,"signatures":[],"disabled_reason":""})
    if state["disabled_reason"]:
        return False, "Cloud disabled after provider billing/access error"
    if not result["unresolved"]:
        if result["scan"].detections:
            return False, "Local evidence is sufficient" if result["complete"] else "Supported local identities retained; remaining coverage needs clearer evidence"
        return False, "No clear unresolved object evidence for cloud escalation" + ("; provider API_KEY is not configured" if not provider_available else "")
    if not provider_available:
        return False, "Provider API_KEY is not configured; local evidence remains available"
    if quality["sharpness"] < 35 or quality["dark_fraction"] > .65 or quality["bright_fraction"] > .65:
        return False, "Image quality is insufficient; request a clearer view before cloud escalation"
    if state["attempts"] >= settings.cloud_max_calls:
        return False, "Per-sweep cloud vision budget reached"
    readable = set(normalized(text_in_outline(metadata, [[0,0],[1,0],[1,1],[0,1]])).split()) if metadata else set()
    readable = {word for word in readable if len(word) >= 3}
    outlines = [proposal["polygon"] for proposal in result.get("provisional_objects", [])]
    if result["signature"] in state["signatures"]:
        previous = [e for e in state.get("evidence",[]) if e["signature"] == result["signature"]]
        same_view = []
        for old in previous or [{}]:
            if recognition_view_changed(metadata, image, old):
                continue
            prior_text = set(old.get("readable_tokens", []))
            # Additional readable content can resolve an identity even when the
            # unavailable-local-model signature and whole-image thumbnail agree.
            readable_gain = bool(readable-prior_text) and sum(map(len, readable)) >= max(4+sum(map(len, prior_text)), 1.2*sum(map(len, prior_text)))
            prior_outlines = old.get("outlines", [])
            added_object = len(outlines) > len(prior_outlines) and any(
                all(overlap(outline, prior) < .25 for prior in prior_outlines) for outline in outlines)
            clearer = quality["sharpness"] > old.get("sharpness",float("inf"))*1.3
            same_view.append(readable_gain or added_object)
            if not clearer and not readable_gain and not added_object:
                return False, "Unresolved evidence already attempted; request new readable evidence"
        # Sharpness alone earns one retry, including after timeouts. All attempts
        # are reserved before the provider call, so failures cannot reset this.
        if len(same_view) >= 2 and not all(same_view):
            return False, "Unresolved view already retried; request new readable evidence"
    if now - state["last_attempt_at"] < settings.cloud_cooldown_s:
        return False, "Cloud vision cooldown active"
    state["attempts"] += 1
    state["last_attempt_at"] = now
    state["signatures"].append(result["signature"])
    state.setdefault("evidence",[]).append({"signature":result["signature"],
                                           "pose":metadata.camera_transform if metadata else None,
                                           "tracking":metadata.tracking if metadata else "unavailable",
                                           "ar_session_id":metadata.ar_session_id if metadata else "",
                                           "pixels":image["pixels"] if image else [],"sharpness":quality["sharpness"],
                                           "readable_tokens":sorted(readable),"outlines":outlines})
    return True, "Clear novel unresolved evidence requires cloud vision"


def latch_credit_error(metrics, error):
    parts=[]
    while error:
        parts.append(str(error).casefold())
        error=error.__cause__
    if any(word in " ".join(parts) for word in ("credit_balance_exhausted", "insufficient_quota", "invalid_api_key", "billing", "credit balance", "permissiondenied", "unauthenticated", "authentication_error", "permission_error", "workspace access")):
        metrics.setdefault("cloud_routing", {})["disabled_reason"] = "Provider billing/access error; no further cloud vision requests in this sweep"


def merge_supported_scan(local, cloud):
    """Preserve independently supported identities when cloud misses/conflicts."""
    detections, mapping, matched = [], {}, {}
    for index, detection in enumerate(cloud.detections):
        match = next((n for n,d in enumerate(local.detections) if detection.kind != "shelf" and overlap(d.polygon,detection.polygon) >= .5),None)
        if match is None:
            mapping[index] = len(detections)
            detections.append(detection.model_copy(deep=True))
        elif match in matched:
            mapping[index] = matched[match]
            existing = detections[matched[match]]
            existing.rare_signals=list(dict.fromkeys(existing.rare_signals+detection.rare_signals))
        else:
            mapping[index] = matched[match] = len(detections)
            kept=local.detections[match].model_copy(deep=True)
            kept.shelf_index=detection.shelf_index
            kept.rare_signals=list(dict.fromkeys(kept.rare_signals+detection.rare_signals))
            detections.append(kept)
    for detection in detections:
        detection.shelf_index=mapping.get(detection.shelf_index)
    protected=set(matched.values())
    for n,d in enumerate(local.detections):
        if n not in matched:
            protected.add(len(detections))
            detections.append(d.model_copy(deep=True))
    issues=list(dict.fromkeys(local.quality_issues+cloud.quality_issues))
    if len(detections)>250:
        # Keep all supported local books, and their shelf references, before
        # spending the remaining result capacity on cloud-only detections.
        protected.update(detections[i].shelf_index for i in tuple(protected) if detections[i].shelf_index is not None)
        remaining=[i for i in range(len(detections)) if i not in protected][:250-len(protected)]
        retained=sorted(protected.union(remaining))
        remap={old:new for new,old in enumerate(retained)}
        detections=[detections[i] for i in retained]
        for detection in detections:
            detection.shelf_index=remap.get(detection.shelf_index)
        issues.append("Detection limit reached; additional cloud-only objects require another view")
    return Scan(detections=detections,quality_issues=issues,
                guidance=cloud.guidance or local.guidance)
