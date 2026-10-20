"""One sweep's mutable state, independent bounded queues, and explicit corrections."""
import asyncio
import base64
import io
import json
import time
from datetime import datetime,timezone,timedelta
from uuid import uuid4

import numpy as np
from PIL import Image

from .config import Settings
from .geometry import measure_spine, measure_room, occupied_shelf_run, reconcile_room
from .models import Book, Item, Dimensions, FrameMetadata, Locale, Price, PriceRange, SweepState, utcnow
from .packet import build_packet, write_bundle, sanitize_price
from .pricing import can_price_record, pricing_query, pricing_fingerprint
from .providers import ProviderUnavailable
from .storage import EvidenceStore, atomic_write
from .tracking import TrackRegistry, roomplan_anchor
from .object_anchor import object_depth_anchor
from .visual_correspondence import FrameCorrespondence
from .vision import supported_identity, image_quality, catalogue_candidates, normalized
from .local_evidence import resolve_local, cloud_decision, latch_credit_error, text_in_outline, merge_supported_scan
from .image_selection import fingerprint, receipt, duplicate_of, stationary
from .costing import pending_assistant_usage, pending_speech_usage, usage_cost


class SweepRunner:
    def __init__(self, state: SweepState, store: EvidenceStore, settings: Settings, vision, prices):
        self.state, self.store, self.settings = state, store, settings
        self.vision, self.prices = vision, prices
        self.lock = asyncio.Lock()
        self.finish_lock = asyncio.Lock()
        self.queue = asyncio.Queue(maxsize=settings.queue_size)
        self.price_queue = asyncio.Queue(maxsize=250)
        self.workers = []
        self.listeners = set()
        self.live = None
        self.saved_provider_usage = list(state.metrics.get("provider_usage") or [])
        self.frame_done = {}
        self.catalogue_cache = {}
        self.pricing_workers = []
        self.pricing_resumed = False
        self.correspondence = FrameCorrespondence(self._tracking_image)

    def _tracking_image(self, frame_id):
        ref=f"frames/{frame_id}.jpg"
        return self.store.read_evidence(self.state,ref) if ref in self.state.evidence else None

    def start(self):
        if not self.workers:
            self.workers = [asyncio.create_task(self.frame_worker()) for _ in range(self.settings.workers)]
        self._start_pricing_workers()

    def _start_pricing_workers(self):
        if not self.pricing_workers:
            self.pricing_workers=[asyncio.create_task(self.price_worker()) for _ in range(4)]
            self.workers.extend(self.pricing_workers)

    def resume_pricing(self):
        """Resume only unstarted durable work; an in-flight billable request is ambiguous."""
        if self.pricing_resumed:
            return
        self.pricing_resumed=True
        queued=[]
        for job in self.state.pricing_jobs.values():
            if job.get("status")=="searching":
                job.update(status="interrupted",reason="Server interrupted valuation; no automatic retry",unknown_cost=True)
            record=next((r for r in self.state.books+self.state.items if r.id==job.get("id")),None)
            if not record or record.revision!=job.get("revision"):
                continue
            if job.get("status")=="queued":
                if can_price_record(record) and pricing_fingerprint(record)==job.get("fingerprint"):
                    queued.append((record.id,record.revision,job["kind"],job["fingerprint"]))
                else:
                    job.update(status="unavailable",reason="Inventory changed before valuation started")
            self._refresh_pricing_status(record)
        if queued:
            self._start_pricing_workers()
            for work in queued:
                if not self.price_queue.full(): self.price_queue.put_nowait(work)
        self._admit_legacy_prices()
        self.store.save(self.state)

    def _admit_legacy_prices(self):
        """Migrate saved pre-job inventories once, without replaying old paid work."""
        if (not self.settings.paid_pricing or self.state.sweep.synthetic
                or self.state.completion not in {"ready","partial"}
                or self.state.metrics.get("legacy_pricing_admitted")):
            return
        attempted={row.get("ref_id") for row in self.state.metrics.get("pricing_stages",[])}
        recorded={job.get("id") for job in self.state.pricing_jobs.values()}
        # Old provider rows do not identify the record searched. Conservatively
        # preserve those outcomes instead of risking a second billable search.
        ambiguous=any(row.get("request_kind") not in {"assistant","image","vision"}
                      or row.get("web_search_calls")
                      for row in self.state.metrics.get("provider_usage",[]))
        for record in self.state.books+self.state.items:
            if record.id in recorded or record.id in attempted:
                continue
            prices=[record.replacement_cost]+([record.used_value] if isinstance(record,Book) else [])
            if any(price.verified for price in prices):
                continue
            reason=""
            if not can_price_record(record):
                reason="Object identity is unknown, excluded or requires appraisal"
            elif ambiguous:
                reason="Earlier provider work has no valuation attribution; no automatic retry"
            elif record.frame_ref not in self.state.evidence:
                reason="Saved image evidence is missing"
            if reason:
                record.pricing_status="unavailable"
                record.pricing_reason=reason
            else:
                self.enqueue_price(record)
        self.state.metrics["legacy_pricing_admitted"]=utcnow()

    def publish(self, event: dict):
        for queue in tuple(self.listeners):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)

    def snapshot(self):
        provider = getattr(self, "provider", None) or (self.vision.provider if self.vision else None)
        if provider:
            # Text and images share this provider ledger. Never add text usage a
            # second time merely because it also has a dedicated request receipt.
            self.state.metrics["provider_usage"] = self.saved_provider_usage + provider.usage
        self.state.metrics["assistant_pending_usage"] = pending_assistant_usage(self.state.metrics)
        self.state.metrics["speech_pending_usage"] = pending_speech_usage(self.state.metrics)
        interrupted={j.get("fingerprint", "")+":"+j.get("kind", ""):j for j in self.state.pricing_jobs.values() if j.get("unknown_cost")}
        self.state.metrics["pricing_interrupted_usage"]=[{"request_kind":"pricing_interrupted","usage":None} for _ in interrupted]
        rows = sum((self.state.metrics.get(key, []) for key in (
            "provider_usage", "live_usage", "onboarding_live_usage", "onboarding_assistant_usage", "assistant_pending_usage", "pricing_interrupted_usage", "speech_usage", "onboarding_speech_usage", "speech_pending_usage")), [])
        if self.vision and not self.state.metrics.get("provider_usage") and (
                self.state.metrics.get("cloud_routing",{}).get("attempts",0) or
                (not self.settings.local_first and any(v in {"processed","failed"} for v in self.state.frame_status.values()))):
            rows.append({"usage":None})
        self.state.metrics.update(usage_cost(rows))
        return build_packet(self.state)

    def save_publish(self):
        self.state.revision += 1
        packet = self.snapshot().model_dump(mode="json")
        self.store.save(self.state)
        self.publish({"type":"inventory", "packet":packet})

    def guide_event(self, stage, evidence_id, **facts):
        if guide := getattr(self, "guide", None):
            guide.record(stage, evidence_id, **facts)

    async def upload(self, meta: FrameMetadata, jpeg: bytes, depth: bytes, confidence: bytes):
        if self.state.sweep.capture_mode == "camera" and (depth or confidence or meta.depth_width or meta.depth_height):
            raise ValueError("Camera-only capture cannot contain LiDAR depth or confidence")
        if len(jpeg)>10_000_000:
            raise ValueError("Frame exceeds 10 MB")
        with Image.open(io.BytesIO(jpeg)) as im:
            if im.format != "JPEG" or im.size != (meta.image_width,meta.image_height):
                raise ValueError("JPEG size/orientation disagrees with calibration")
            im.verify()
        n = meta.depth_width*meta.depth_height
        if (depth or confidence) and (len(depth)!=n*4 or len(confidence)!=n or n==0):
            raise ValueError("Depth/confidence byte lengths do not match aligned dimensions")
        signature = receipt(meta, jpeg, depth, confidence)
        candidate = await asyncio.to_thread(fingerprint, jpeg)
        async with self.lock:
            if self.state.ar_session_id and meta.ar_session_id != self.state.ar_session_id:
                raise ValueError("Evidence must come from the same AR session")
            if prior := self.state.image_receipts.get(meta.id):
                if prior["signature"] != signature:
                    raise ValueError("Retry cannot change evidence payload or calibration")
                return {**prior["response"], "duplicate":True}
            duplicate = meta.id in self.state.frames
            if duplicate and bool(depth) != (f"depth/{meta.id}.bin" in self.state.evidence):
                raise ValueError("Retry cannot change evidence payload")
            if not duplicate and self.state.completion != "capturing":
                raise RuntimeError("Capture already ended")
            if not duplicate and len(self.state.frames)>=self.settings.max_frames:
                raise ValueError("Frame budget reached; end the sweep")
            if not duplicate:
                same = duplicate_of(candidate, meta, self.state.image_fingerprints, self.state.frames)
                exact = same and self.state.image_fingerprints[same]["sha256"] == candidate["sha256"]
                # A merely similar RGB view cannot stand in for a newly measured depth frame.
                if same and not exact and depth:
                    same = None
                if same:
                    # RGB equality does not imply equal depth, confidence, pose,
                    # or calibration. Re-evaluate geometry without another model call.
                    refine = bool(exact and depth)
                    if refine and self.queue.full():
                        return {"accepted":False,"duplicate":False,"processing":"retry_backpressure","retry_after_s":3}
                    response = {"accepted":True,"duplicate":True,"duplicate_of":same,"processing":"duplicate_suppressed"}
                    self.store.put_json(self.state,f"observations/meta_{meta.id}.json",meta.model_dump())
                    if exact:
                        # The JPEG is a hard link to the same content-addressed bytes;
                        # calibration and aligned depth always belong to this frame.
                        self.store.put_evidence(self.state,f"frames/{meta.id}.jpg",jpeg)
                        if depth:
                            self.store.put_evidence(self.state,f"depth/{meta.id}.bin",depth)
                            self.store.put_evidence(self.state,f"depth/{meta.id}.confidence",confidence)
                        self.state.frames[meta.id] = meta
                        self.state.frame_status[meta.id] = "queued_geometry" if refine else "duplicate_suppressed"
                        if refine:
                            response["processing"] = "queued_geometry"
                            self.frame_done[meta.id] = asyncio.Event()
                            self.queue.put_nowait(meta.id)
                    self.state.image_receipts[meta.id] = {"signature":signature,"response":response}
                    self.state.metrics["duplicate_images_suppressed"] = self.state.metrics.get("duplicate_images_suppressed",0)+1
                    self.store.save(self.state)
                    self.guide_event("duplicate", meta.id, source_frame_id=same)
                    return response
                if self.queue.full():
                    return {"accepted":False,"duplicate":False,"processing":"retry_backpressure","retry_after_s":3}
            # Immutable metadata also rejects changed calibration on a retry.
            self.store.put_json(self.state, f"observations/meta_{meta.id}.json", meta.model_dump())
            self.store.put_evidence(self.state, f"frames/{meta.id}.jpg", jpeg)
            if depth:
                self.store.put_evidence(self.state, f"depth/{meta.id}.bin", depth)
                self.store.put_evidence(self.state, f"depth/{meta.id}.confidence", confidence)
            if duplicate:
                return {"accepted":True,"duplicate":True,"processing":self.state.frame_status.get(meta.id)}
            self.state.ar_session_id = meta.ar_session_id
            self.state.frames[meta.id] = meta
            self.state.frame_status[meta.id] = "queued"
            self.frame_done[meta.id] = asyncio.Event()
            self.queue.put_nowait(meta.id)
            response = {"accepted":True,"duplicate":False,"processing":"queued","queue_depth":self.queue.qsize()}
            self.state.image_fingerprints[meta.id] = candidate
            self.state.image_receipts[meta.id] = {"signature":signature,"response":response}
            self.store.save(self.state)
            self.guide_event("uploaded", meta.id)
        # Voice gets structured observations, not a second billable copy of every image.
        return response

    async def frame_worker(self):
        while True:
            id = await self.queue.get()
            start = time.monotonic()
            try:
                meta = self.state.frames[id]
                reused = self.state.image_receipts.get(id,{}).get("response",{}).get("duplicate_of")
                if reused:
                    done = self.frame_done.get(reused)
                    if done: await done.wait()
                    async with self.lock:
                        refined = self.refine_duplicate_geometry(meta,reused)
                        self.state.frame_status[id] = "duplicate_geometry_processed" if refined else "duplicate_geometry_unavailable"
                        self.save_publish()
                    continue
                jpeg = self.store.read_evidence(self.state, f"frames/{id}.jpg")
                quality = await asyncio.to_thread(image_quality,jpeg)
                if quality["sharpness"] < 35:
                    self.publish({"type":"guidance","text":"Hold steady and move closer to the spines while staying in this pass."})
                local = None
                route = None
                if self.settings.local_first:
                    local = await resolve_local(meta, self.catalogue_cache)
                    scan = local["scan"]
                    async with self.lock:
                        use_cloud, reason = cloud_decision(local, quality, self.state.metrics, self.settings, bool(self.vision), time.time(),
                                                          meta,self.state.image_fingerprints.get(id))
                        route = {"frame_id":id,"route":"cloud" if use_cloud else "local" if local["complete"] else "deferred",
                                 "reasons":local["reasons"]+[reason], "provisional_objects":local["provisional_objects"]}
                        self.state.metrics.setdefault("routing",[]).append(route)
                        # Persist reservation before starting the potentially billable call.
                        self.store.save(self.state)
                    if use_cloud:
                        try:
                            cloud_scan = await self.vision.inspect(jpeg,meta)
                            scan = merge_supported_scan(scan,cloud_scan)
                        except asyncio.CancelledError:
                            raise
                        except Exception as error:
                            latch_credit_error(self.state.metrics,error)
                            route["route"] = "deferred"
                            route["reasons"].append(str(error) if isinstance(error,ProviderUnavailable) else f"Cloud recognition failed ({type(error).__name__})")
                    if route["route"] == "deferred":
                        scan.quality_issues.extend(route["reasons"])
                        scan.guidance = "Please hold steady and show a closer readable view. Local proposals remain unverified."
                else:
                    if not self.vision:
                        raise ProviderUnavailable("Provider API_KEY is not configured; frame saved without identification")
                    scan = await self.vision.inspect(jpeg,meta)
                async with self.lock:
                    self.store.put_json(self.state,f"observations/vision_{id}.json", {"scan":scan.model_dump(),"quality":quality,"routing":route})
                    prior_ids = {record.id for record in self.state.books + self.state.items}
                    overlays = self.apply_scan(meta, scan)
                    if local:
                        for book in self.state.books:
                            if book.frame_ref == f"frames/{id}.jpg":
                                candidates = local["catalogues"].get(normalized(book.title)+"|"+normalized(book.author))
                                if candidates is not None:
                                    book.catalogue_candidates = candidates
                                book.evidence_refs = list(dict.fromkeys(book.evidence_refs+[f"observations/meta_{id}.json",f"observations/vision_{id}.json"]))
                    self.store.put_json(self.state,f"observations/overlays_{id}.json",overlays)
                    self.state.frame_status[id] = "local_pending" if route and route["route"] == "deferred" else "processed"
                    self.state.metrics.setdefault("identification_stages",[]).append({"frame_id":id,"elapsed_s":time.monotonic()-start})
                    self.save_publish()
                    guide_issues = list(scan.quality_issues)
                    if quality["sharpness"] < 35: guide_issues.append("The view is blurred; hold steady during this pass")
                    if quality["dark_fraction"] > .65: guide_issues.append("The view is too dark for clear recognition")
                    if quality["bright_fraction"] > .65: guide_issues.append("The view is too bright for clear recognition")
                    if meta.tracking != "normal": guide_issues.append("Camera tracking is limited; keep the sweep slow and steady")
                    self.guide_event("processed", id,
                        new_ids={record.id for record in self.state.books + self.state.items} - prior_ids,
                        issues=guide_issues)
                    self.publish(overlays)
                    self.publish({"type":"recognition_status","status":"pending" if self.state.frame_status[id]=="local_pending" else "ready",
                                  "text":"Local evidence needs a clearer view or review" if self.state.frame_status[id]=="local_pending" else "Server recognition updated",
                                  "cost":self.state.metrics.get("cost_usd"), "routing":route})
                if scan.guidance:
                    self.publish({"type":"guidance","text":scan.guidance})
                if self.live:
                    self.live.offer_context({"quality_issues":scan.quality_issues,"guidance":scan.guidance,
                                             "inventory":self.voice_context()})
            except asyncio.CancelledError:
                raise
            except Exception as error:
                async with self.lock:
                    reason = str(error) if isinstance(error,ProviderUnavailable) else f"Identification failed ({type(error).__name__})"
                    self.state.frame_status[id] = "failed"
                    if reason not in self.state.review_reasons:
                        self.state.review_reasons.append(reason)
                    self.save_publish()
                    self.guide_event("failed", id, issues=[reason])
                    self.publish({"type":"recognition_status","status":"unavailable","text":reason+". On-phone detection remains available."})
                    if self.live:
                        self.live.offer_context({"recognition_unavailable":reason})
            finally:
                if id in self.frame_done: self.frame_done[id].set()
                self.queue.task_done()

    def refine_duplicate_geometry(self, meta, source):
        """Reuse RGB polygons only; compute dimensions from this frame's calibration/depth."""
        reference = f"observations/overlays_{source}.json"
        if reference not in self.state.evidence:
            self.state.review_reasons.append(f"Frame {meta.id}: depth retained; source recognition unavailable for measurement")
            return False
        original = json.loads(self.store.read_evidence(self.state,reference))
        depth = np.frombuffer(self.store.read_evidence(self.state,f"depth/{meta.id}.bin"),dtype="<f4").reshape(meta.depth_height,meta.depth_width)
        confidence = np.frombuffer(self.store.read_evidence(self.state,f"depth/{meta.id}.confidence"),dtype="u1").reshape(depth.shape)
        same_pose = stationary(meta.camera_transform,self.state.frames[source].camera_transform)
        if not same_pose:
            self.state.review_reasons.append(f"Frame {meta.id}: identical image has a different pose; retained geometry is not merged into inventory")
        result = []
        for entry in original["detections"]:
            metric = measure_spine(meta,depth,confidence,entry["polygon"],max_height_cm=100 if entry["kind"]=="book" else 700,
                                   max_thickness_cm=30 if entry["kind"]=="book" else 400)
            dimensions = {"spine_length_cm":metric["height_cm"],"spine_thickness_cm":metric["thickness_cm"]} if entry["kind"]=="book" else {"face_long_cm":metric["height_cm"],"face_short_cm":metric["thickness_cm"]}
            result.append({**entry,"world_corners":metric["world_corners"],"dimensions":dimensions,"measurement_source":metric.get("method","")})
            record = next((record for record in self.state.books+self.state.items if record.id==entry["id"]),None)
            if record and same_pose:
                record.evidence_refs = list(dict.fromkeys(record.evidence_refs+[f"frames/{meta.id}.jpg",f"depth/{meta.id}.bin",f"depth/{meta.id}.confidence",f"observations/meta_{meta.id}.json"]))
                if metric["height_cm"] is not None and not record.world_corners:
                    record.world_corners = metric["world_corners"]
                    if isinstance(record,Book):
                        record.spine_height_cm = metric["height_cm"]; record.spine_thickness_cm = metric["thickness_cm"]
                        record.measurement = metric
                    if record.id in self.state.spatial_tracks:
                        self.state.spatial_tracks[record.id]["corners"] = metric["world_corners"]
                        self.state.spatial_tracks[record.id]["frames"].append(meta.id)
        event = {"type":"frame_overlays","frame_id":meta.id,"timestamp_s":meta.timestamp_s,"ar_session_id":meta.ar_session_id,"detections":result}
        self.store.put_json(self.state,f"observations/overlays_{meta.id}.json",event)
        self.publish(event)
        return True

    def apply_scan(self, meta, scan):
        for issue in scan.quality_issues:
            reason=f"Frame {meta.id}: unresolved capture quality — {issue}"
            if issue.strip() and reason not in self.state.review_reasons:
                self.state.review_reasons.append(reason)
        depth_ref = f"depth/{meta.id}.bin"
        if depth_ref in self.state.evidence:
            depth=np.frombuffer(self.store.read_evidence(self.state,depth_ref),dtype="<f4").reshape(meta.depth_height,meta.depth_width)
            confidence=np.frombuffer(self.store.read_evidence(self.state,f"depth/{meta.id}.confidence"),dtype="u1").reshape(depth.shape)
        else:
            depth=confidence=[]
        registry=TrackRegistry(self.state.spatial_tracks,self.correspondence)
        measured={}
        shelves={}
        overlays=[]
        for n,d in enumerate(scan.detections):
            metric=measure_spine(meta,depth,confidence,d.polygon,max_height_cm=100 if d.kind=="book" else 700,
                                 max_thickness_cm=30 if d.kind=="book" else 400)
            measured[n]=metric
            if d.kind=="shelf":
                track=registry.associate({"kind":"shelf","frame_id":meta.id,"world_corners":metric["world_corners"]})
                shelves[n]=track["id"]
                if metric["world_corners"]:
                    self.state.shelf_geometry[track["id"]]=metric["world_corners"]
        for n,d in enumerate(scan.detections):
            if d.kind=="shelf":
                continue
            metric=measured[n]
            observation={"kind":d.kind,"frame_id":meta.id,"world_corners":metric["world_corners"],
                         "category":d.category,"polygon":d.polygon,"metadata":meta.model_dump()}
            if d.kind=="item":
                observation["depth_anchor"]=object_depth_anchor(meta,depth,confidence,d.polygon)
                observation["roomplan_id"]=roomplan_anchor(observation,self.state.geometry)
            track=registry.associate(observation)
            reasons=[reason for reason in [metric["reason"],track["reason"]] if reason]
            frame_ref=f"frames/{meta.id}.jpg"
            if d.kind=="book":
                independent=text_in_outline(meta,d.polygon)
                identity=supported_identity(d,independent if self.settings.local_first or independent else None)
                existing=next((b for b in self.state.books if b.id==track["id"]),None)
                shelf=shelves.get(d.shelf_index, existing.shelf if existing else "")
                if not shelf:
                    reasons.append("Shelf-level association unavailable; shelf run incomplete")
                b=existing or Book(id=track["id"], frame_ref=frame_ref)
                changed = not existing or (not b.user_claims and identity["id_confidence"]>b.id_confidence)
                old_fingerprint=pricing_fingerprint(b)
                old_status=b.status
                price_changed=not existing
                if changed:
                    for key,value in identity.items():
                        setattr(b,key,value)
                    b.visible_text=d.visible_text
                    b.frame_ref=frame_ref
                    b.polygon=d.polygon
                    price_changed=price_changed or old_fingerprint!=pricing_fingerprint(b) or old_status!=b.status
                    if price_changed:
                        b.replacement_cost=Price()
                        b.used_value=Price()
                        b.revision+=1
                new_signals=set(d.rare_signals)-set(b.rare_signals)
                if new_signals:
                    b.rare_signals=list(dict.fromkeys(b.rare_signals+d.rare_signals))
                    b.replacement_cost=Price()
                    b.used_value=Price()
                    b.revision+=1
                if b.rare_signals:
                    b.status="needs_appraisal"
                    self.invalidate_pricing_jobs(b,"Possible rare or original item; appraisal required")
                if metric["height_cm"] is not None and not b.spine_height_cm:
                    b.spine_height_cm=metric["height_cm"]
                    b.spine_thickness_cm=metric["thickness_cm"]
                    b.world_corners=metric["world_corners"]
                    b.measurement=metric
                b.shelf=shelf
                b.excluded=b.excluded or shelf in self.state.excluded_shelves
                b.evidence_refs=list(dict.fromkeys(b.evidence_refs+[frame_ref]))
                b.review_reasons=list(dict.fromkeys(b.review_reasons+reasons))
                if not existing:
                    self.state.books.append(b)
                if price_changed and can_price_record(b):
                    self.enqueue_price(b)
                elif not can_price_record(b):
                    self.invalidate_pricing_jobs(b,"Inventory is excluded, unidentified or requires appraisal")
                label=b.title.strip() or "Book"
                category="Book"
                dimensions={"spine_length_cm":metric["height_cm"], "spine_thickness_cm":metric["thickness_cm"]}
            else:
                existing=next((i for i in self.state.items if i.id==track["id"]),None)
                independent=normalized(text_in_outline(meta,d.polygon))
                observed_model=normalized(d.brand_model)
                supported_model=d.brand_model if (observed_model and observed_model in normalized(d.visible_text)
                    and (not independent or observed_model in independent)) else ""
                if existing:
                    before=pricing_fingerprint(existing)
                    if not existing.user_claims and d.confidence>existing.confidence:
                        if d.category.strip(): existing.category=d.category.strip()
                        if d.material.strip(): existing.material=d.material.strip()
                        if supported_model: existing.brand_model=supported_model
                        if d.description.strip(): existing.description=d.description.strip()
                        existing.confidence=d.confidence
                    if before!=pricing_fingerprint(existing):
                        existing.replacement_cost=PriceRange()
                        existing.revision+=1
                        existing.frame_ref=frame_ref
                        if existing.status!="needs_appraisal": existing.status="unpriced"
                        self.enqueue_price(existing)
                    existing.evidence_refs=list(dict.fromkeys(existing.evidence_refs+[frame_ref]))
                    if metric["world_corners"]:
                        existing.world_corners=metric["world_corners"]
                    item=existing
                else:
                    art=any(w in d.category.casefold() for w in ["art","portrait","painting"])
                    item=Item(id=track["id"], category=d.category.strip() or "Object", description=d.description,
                              material=d.material,brand_model=supported_model,frame_ref=frame_ref,
                              confidence=d.confidence,world_corners=metric["world_corners"],evidence_refs=[frame_ref],
                              status="needs_appraisal" if art else "unpriced", review_reasons=reasons)
                    if art:
                        item.review_reasons.append("Ask whether this is an original or a print")
                    self.state.items.append(item)
                    if can_price_record(item):
                        self.enqueue_price(item)
                if not can_price_record(item):
                    self.invalidate_pricing_jobs(item,"Inventory is excluded or requires appraisal")
                category=item.category.strip() or "Object"
                label=category
                # These two edges describe the visible plane, not a 3D bounding box.
                dimensions={"face_long_cm":metric["height_cm"], "face_short_cm":metric["thickness_cm"]}
            overlays.append({"id":track["id"], "kind":d.kind, "label":label, "category":category,
                             "polygon":d.polygon, "world_corners":metric["world_corners"],
                             "dimensions":dimensions, "measurement_source":metric.get("method", "")})
        self.state.spatial_tracks=registry.tracks
        self.recompute_shelves()
        if self.state.geometry:
            self.state.room=reconcile_room(self.state)
            self.refresh_pricing_queries()
        return {"type":"frame_overlays", "frame_id":meta.id, "timestamp_s":meta.timestamp_s,
                "ar_session_id":meta.ar_session_id, "detections":overlays}

    def recompute_shelves(self):
        spans=[]
        for b in self.state.books:
            if b.excluded:
                continue
            corners=self.state.shelf_geometry.get(b.shelf)
            if not corners or not b.world_corners:
                self.state.shelf_run_m=None
                return
            shelf=np.asarray(corners)
            # World horizontal direction: the widest horizontal edge of the shelf.
            edges=np.roll(shelf,-1,axis=0)-shelf
            edges[:,1]=0
            axis=edges[np.argmax(np.linalg.norm(edges,axis=1))]
            length=np.linalg.norm(axis)
            if length<.01:
                self.state.shelf_run_m=None
                return
            axis/=length
            occupied=np.asarray(b.world_corners)@axis
            spans.append({"shelf":b.shelf,"start":float(min(occupied)),"end":float(max(occupied))})
        self.state.shelf_run_m=occupied_shelf_run(spans)
        for shelf in self.state.shelf_geometry:
            books=[b for b in self.state.books if b.shelf==shelf]
            books.sort(key=lambda b: np.mean(np.asarray(b.world_corners)[:,0]) if b.world_corners else 0)
            for n,b in enumerate(books,1):
                b.position=n

    def invalidate_pricing_jobs(self,record,reason):
        for job in self.state.pricing_jobs.values():
            if job.get("id")==record.id and job.get("status") in {"queued","searching"}:
                job.update(status="unavailable",reason=reason)
        record.pricing_status="unavailable"
        record.pricing_reason=reason

    def _refresh_pricing_status(self,record):
        jobs=[j for j in self.state.pricing_jobs.values() if j.get("id")==record.id and j.get("revision")==record.revision]
        if not jobs:
            return
        statuses={j["status"] for j in jobs}
        record.pricing_status=next((status for status in ("searching","queued","interrupted") if status in statuses),
                                  "completed" if "completed" in statuses else "unavailable")
        record.pricing_reason="; ".join(dict.fromkeys(j.get("reason","") for j in jobs if j.get("reason")))[:1000]

    def enqueue_price(self, record):
        if not can_price_record(record):
            record.pricing_status="unavailable"
            record.pricing_reason="Excluded or requiring appraisal; no automatic valuation"
            return
        if not self.settings.paid_pricing:
            record.pricing_status="unavailable"
            record.pricing_reason="Retailer search is disabled; supported prices remain unknown"
            return
        fingerprint=pricing_fingerprint(record)
        for kind in (("new","used") if isinstance(record,Book) else ("new",)):
            key=f"{record.id}:{kind}"
            previous=self.state.pricing_jobs.get(key)
            if previous and previous.get("revision")==record.revision and previous.get("fingerprint")==fingerprint:
                continue  # Includes unavailable/interrupted: never silently retry paid work.
            job={"id":record.id,"revision":record.revision,"kind":kind,"fingerprint":fingerprint,
                 "status":"queued","reason":"","queued_at":utcnow()}
            self.state.pricing_jobs[key]=job
            if not self.price_queue.full():
                self.price_queue.put_nowait((record.id,record.revision,kind,fingerprint))
            else:
                job.update(status="unavailable",reason="Pricing queue budget reached")
        self._refresh_pricing_status(record)
        if record.pricing_status=="queued" and self.state.completion in {"ready","partial"}:
            self._start_pricing_workers()

    def refresh_pricing_queries(self):
        for record in self.state.books+self.state.items:
            job=self.state.pricing_jobs.get(f"{record.id}:new")
            if job and job.get("fingerprint")!=pricing_fingerprint(record) and job.get("revision")==record.revision:
                record.revision+=1
                record.replacement_cost=Price() if isinstance(record,Book) else PriceRange()
                if isinstance(record,Book): record.used_value=Price()
                self.enqueue_price(record)
            elif isinstance(record,Item) and record.replacement_cost.low is not None:
                record.replacement_cost=self._revalidate_item_quote(record,record.replacement_cost)
                if record.replacement_cost.low is None:
                    reason=record.replacement_cost.exclusion_reason
                    record.status="unpriced"
                    record.pricing_status="unavailable"
                    record.pricing_reason=reason
                    if job and job.get("revision")==record.revision:
                        job.update(status="unavailable",reason=reason)

    def _revalidate_item_quote(self,record,quote):
        """Fingerprint bins control scheduling, never raw attribute acceptance."""
        from .pricing import item_match
        if not isinstance(record,Item) or (getattr(quote,"amount",None) is None and getattr(quote,"low",None) is None):
            return quote
        # Legacy/manual values without dimensional evidence are left to the
        # ordinary source validator; measured items require inspectable evidence.
        if quote.source_type!="retailer_offer" and not record.dimensions_cm.model_dump(exclude_none=True):
            return quote
        reason="Retailer offer no longer supports current object attributes; no automatic retry"
        try:
            evidence=json.loads(self.store.read_evidence(self.state,quote.source_ref))
            if not isinstance(evidence,dict) or evidence.get("query",{}).get("kind")!="item":
                return type(quote)(exclusion_reason=reason)
            attributes=evidence.get("product_attributes",{})
            if not isinstance(attributes,dict):
                return type(quote)(exclusion_reason=reason)
            matching=item_match({**attributes,"name":evidence.get("product_name","")},pricing_query(record))
        except (ValueError,TypeError,AttributeError,OSError):
            matching=None
        if matching is None:
            return type(quote)(exclusion_reason=reason)
        match,basis=matching
        return quote.model_copy(update={"match":match,"match_basis":basis})

    async def price_worker(self):
        while True:
            id,revision,kind,fingerprint=await self.price_queue.get()
            start=time.monotonic()
            key=f"{id}:{kind}"
            try:
                async with self.lock:
                    record=next((r for r in self.state.books+self.state.items if r.id==id),None)
                    job=self.state.pricing_jobs.get(key,{})
                    if (record is None or record.revision!=revision or not can_price_record(record)
                            or pricing_fingerprint(record)!=fingerprint or job.get("status")!="queued"
                            or job.get("revision")!=revision):
                        continue
                    query=pricing_query(record)
                    locale=Locale(country=self.state.sweep.country,currency=self.state.sweep.currency)
                    job.update(status="searching",started_at=utcnow())
                    self._refresh_pricing_status(record)
                    # Persist inventory, job admission and usage reservation before any network await.
                    self.save_publish()
                if not self.prices or not self.settings.paid_pricing or self.state.metrics.get("cloud_routing",{}).get("disabled_reason"):
                    quote,sources=Price(exclusion_reason="Retailer search unavailable or disabled"),[]
                else:
                    quote,sources=await self.prices.quote(query,locale,kind)
                await self.apply_quote(id,revision,kind,quote,sources,fingerprint)
            except asyncio.CancelledError:
                async with self.lock:
                    job=self.state.pricing_jobs.get(key,{})
                    if job.get("revision")==revision and job.get("status")=="searching":
                        job.update(status="interrupted",reason="Valuation interrupted; no automatic retry")
                        record=next((r for r in self.state.books+self.state.items if r.id==id),None)
                        if record and record.revision==revision: self._refresh_pricing_status(record)
                        self.save_publish()
                raise
            except Exception as error:
                async with self.lock:
                    job=self.state.pricing_jobs.get(key,{})
                    if job.get("revision")==revision:
                        job.update(status="unavailable",reason=f"Valuation failed ({type(error).__name__}); no estimate substituted")
                        record=next((r for r in self.state.books+self.state.items if r.id==id),None)
                        if record and record.revision==revision: self._refresh_pricing_status(record)
                        self.save_publish()
            finally:
                self.state.metrics.setdefault("pricing_stages",[]).append({"ref_id":id,"kind":kind,"elapsed_s":time.monotonic()-start})
                self.price_queue.task_done()

    async def apply_quote(self,id,revision,kind,quote,sources,fingerprint=None):
        async with self.lock:
            record=next((r for r in self.state.books+self.state.items if r.id==id),None)
            if (not record or record.revision!=revision or not can_price_record(record)
                    or (fingerprint and pricing_fingerprint(record)!=fingerprint)):
                return False
            for ref,evidence in sources:
                self.store.put_json(self.state,ref,evidence)
            quote=sanitize_price(quote,self.state,True)
            quote=self._revalidate_item_quote(record,quote)
            if "appraisal threshold" in quote.exclusion_reason:
                record.status="needs_appraisal"
                record.review_reasons=list(dict.fromkeys(record.review_reasons+["Retrieved offer above configured threshold; human appraisal required"]))
                # An appraisal decision invalidates both values, including an earlier completed quote.
                record.replacement_cost=Price() if isinstance(record,Book) else PriceRange()
                if isinstance(record,Book): record.used_value=Price()
                self.invalidate_pricing_jobs(record,"Retrieved offer above threshold; appraisal required")
            elif isinstance(record,Book):
                if kind=="new": record.replacement_cost=quote
                else: record.used_value=quote
            else:
                values=quote.model_dump(exclude={"amount"})
                record.replacement_cost=PriceRange(low=quote.amount,high=quote.amount,**values)
                record.status=("priced" if quote.match=="exact" else "range") if quote.amount is not None else "unpriced"
            job=self.state.pricing_jobs.get(f"{id}:{kind}")
            if job and job.get("revision")==revision:
                job.update(status="completed" if quote.amount is not None else "unavailable",reason=quote.exclusion_reason,
                           finished_at=utcnow())
                self._refresh_pricing_status(record)
            else:
                record.pricing_status="completed" if quote.amount is not None else "unavailable"
                record.pricing_reason=quote.exclusion_reason
            self.save_publish()
            if self.state.completion in {"ready","partial"}:
                write_bundle(self.state,self.store)
            if self.live:
                self.live.offer_context({"valuation_updated":True})
            return True

    async def apply_prices(self,id,revision,replacement,used,sources):
        # Kept for callers applying a pair; workers publish each quote independently.
        applied=await self.apply_quote(id,revision,"new",replacement,sources)
        if applied and any(b.id==id for b in self.state.books):
            await self.apply_quote(id,revision,"used",used,[])
        return applied

    async def set_geometry(self, geometry: dict):
        if self.state.sweep.capture_mode == "camera":
            raise ValueError("Camera-only capture cannot contain RoomPlan geometry")
        async with self.lock:
            if geometry==self.state.geometry and self.state.room.geometry_ref:
                return  # Idempotent retry after a lost finish/download response.
            if self.state.completion != "capturing":
                raise RuntimeError("Capture already ended")
            if not self.state.ar_session_id or geometry.get("ar_session_id")!=self.state.ar_session_id:
                raise ValueError("Geometry must come from the same captured AR session")
            ref=f"geometry/room_{uuid4().hex[:12]}.json"
            self.store.put_json(self.state,ref,geometry)
            self.state.geometry=geometry
            self.state.room=measure_room(geometry)
            self.state.room.geometry_ref=ref
            self.state.room=reconcile_room(self.state)
            self.refresh_pricing_queries()
            self.save_publish()
            if self.live:
                self.live.offer_context({"measurement_update":True,"inventory":self.voice_context()})

    async def correct(self, correction: dict):
        async with self.lock:
            action,ref,value=correction.get("action"),correction.get("ref_id"),str(correction.get("value",""))[:1000]
            ref=self.state.metrics.get("inventory_aliases",{}).get(ref,ref)
            records=[r for r in self.state.books+self.state.items if r.id==ref]
            if action=="exclude_shelf":
                if not any(b.shelf==ref for b in self.state.books):
                    raise ValueError("Unknown shelf; choose an explicit visible shelf id")
                self.state.excluded_shelves=list(dict.fromkeys(self.state.excluded_shelves+[ref]))
                records=[b for b in self.state.books if b.shelf==ref]
            elif not records:
                raise ValueError("Unknown inventory id")
            if action not in {"exclude","exclude_shelf","claim","confirm_print","mark_unidentified"}:
                raise ValueError("Unsupported correction")
            for record in records:
                self.invalidate_pricing_jobs(record,"User correction invalidated prior valuation")
                record.revision+=1
                record.user_claims.append(value or action)
                if isinstance(record,Book):
                    record.replacement_cost=Price(exclusion_reason="User correction invalidated prior valuation")
                    record.used_value=Price(exclusion_reason="User correction invalidated prior valuation")
                else:
                    record.replacement_cost=PriceRange(exclusion_reason="User correction invalidated prior valuation")
                if action in {"exclude","exclude_shelf"}:
                    record.excluded=True
                elif action=="confirm_print" and isinstance(record,Item):
                    record.is_print=True
                    record.status="unpriced"
                    record.review_reasons=[r for r in record.review_reasons if "original or a print" not in r]
                    self.enqueue_price(record)
                elif action=="mark_unidentified" and isinstance(record,Book):
                    record.status="unidentified"
                    record.title=record.author=record.edition=record.isbn=""
                    record.id_confidence=0
                else:
                    record.status="needs_appraisal"
                    record.review_reasons.append("User statement recorded; evidence/valuation needs human review")
            self.state.events.append({"at":utcnow(),"type":"correction","action":action,"ref_id":ref,"value":value})
            self.recompute_shelves()
            self.save_publish()
            if self.state.completion in {"ready","partial"}:
                write_bundle(self.state,self.store)
            return self.snapshot()

    async def finish(self,duration_s:float):
        async with self.finish_lock:
            if self.state.completion in {"ready","partial"}:
                return self.snapshot()
            started=time.monotonic()
            async with self.lock:
                self.state.completion="processing"
                self.state.sweep.duration_s=duration_s
                # Server creation precedes native capture. This conservative end
                # estimate includes native processing/upload delay, without trusting
                # a phone wall clock or resetting the end time on a retry.
                end=datetime.fromisoformat(self.state.sweep.captured_at)+timedelta(seconds=duration_s)
                end=min(end,datetime.now(timezone.utc))
                self.state.sweep.ended_at=end.isoformat()
                self.state.metrics["timing_basis"]="server sweep creation + native monotonic capture duration; conservatively includes native startup, finalization and upload delay"
                self.save_publish()
            try:
                async with asyncio.timeout(self.settings.finish_timeout):
                    await self.queue.join()
                    await self.price_queue.join()
            except TimeoutError:
                self.state.review_reasons.append("Processing deadline reached; unfinished work is excluded and requires review")
                for worker in self.workers:
                    worker.cancel()
                await asyncio.gather(*self.workers,return_exceptions=True)
                self.workers=[]
                self.pricing_workers=[]
                while not self.price_queue.empty():
                    self.price_queue.get_nowait()
                    self.price_queue.task_done()
                for job in self.state.pricing_jobs.values():
                    if job.get("status")=="queued":
                        job.update(status="unavailable",reason="Processing deadline reached before valuation started")
                for record in self.state.books+self.state.items: self._refresh_pricing_status(record)
            async with self.lock:
                if self.state.geometry:
                    self.state.room=reconcile_room(self.state)
                if not self.state.frames:
                    self.state.review_reasons.append("No camera frames captured")
                if not self.state.books and not self.state.items:
                    self.state.review_reasons.append("No inventory identified; room coverage needs review")
                self.state.completion="partial" if build_packet(self.state).review_queue else "ready"
                self.state.sweep.packet_ready_at=utcnow()
                self.state.metrics["time_to_packet_s"]=(datetime.now(timezone.utc)-end).total_seconds()
                self.state.metrics["frames_saved"]=len(self.state.frames)
                self.state.metrics["frames_processed"]=sum(v=="processed" for v in self.state.frame_status.values())
                if self.vision:
                    self.state.metrics["provider_usage"]=self.vision.provider.usage
                self.save_publish()
                write_bundle(self.state,self.store)
                self.state.metrics["backend_finalize_s"]=time.monotonic()-started
                self.state.metrics["time_to_packet_s"]=(datetime.now(timezone.utc)-end).total_seconds()
                self.state.sweep.packet_ready_at=utcnow()
                self.store.save(self.state)
                write_bundle(self.state,self.store)
                self.guide_event("complete", self.state.sweep.id)
            if self.live:
                self.live.offer_context({"capture_ended":True,"summary":self.voice_context(),"time_to_packet_s":self.state.metrics["time_to_packet_s"]})
            self.publish({"type":"packet_ready","packet":self.snapshot().model_dump(mode="json")})
            return self.snapshot()

    def voice_context(self):
        p=self.snapshot()
        guide_cue=self.state.metrics.get("sweep_guide",{}).get("active_cue")
        return {**({"guide_cue":guide_cue} if guide_cue else {}), "capture_mode":p.sweep.capture_mode,
                "count_caveat":"Camera-only tracks may include repeat views of the same item; physical count is unverified" if p.sweep.capture_mode == "camera" else "Counts require ground-truth validation",
                "totals":p.totals.model_dump(mode="json"),"room":p.room.model_dump(mode="json"),
                "processing":{"frames_saved":len(self.state.frames),
                    "frames_processed":sum(v=="processed" for v in self.state.frame_status.values()),
                    "duplicate_images_suppressed":self.state.metrics.get("duplicate_images_suppressed",0),
                    "pricing_queued":sum(j.get("status")=="queued" for j in self.state.pricing_jobs.values()),
                    "pricing_searching":sum(j.get("status")=="searching" for j in self.state.pricing_jobs.values())},
                "books":[{"id":b.id,"title":b.title,"shelf":b.shelf,"status":b.status,"excluded":b.excluded,
                    "frame_ref":b.frame_ref,"pricing_status":b.pricing_status,"pricing_reason":b.pricing_reason,
                    "replacement_cost":b.replacement_cost.model_dump(mode="json"),
                    "used_value":b.used_value.model_dump(mode="json")} for b in p.books],
                "items":[{"id":i.id,"category":i.category,"status":i.status,"excluded":i.excluded,
                    "frame_ref":i.frame_ref,"brand_model":i.brand_model,"pricing_status":i.pricing_status,"pricing_reason":i.pricing_reason,
                    "replacement_cost":i.replacement_cost.model_dump(mode="json"),
                    "dimensions_cm":i.dimensions_cm.model_dump()} for i in p.items],
                "review_count":len(p.review_queue),"completion":p.completion}

    async def second_locale(self,locale:Locale):
        selected=[b.model_copy(deep=True) for b in self.state.books if b.status=="identified" and not b.excluded][:10]
        results=[]
        for b in selected:
            if not self.prices or not self.settings.paid_pricing or self.state.metrics.get("cloud_routing",{}).get("disabled_reason"):
                reason="Paid retailer search disabled by cost policy or provider availability"
                replacement,used,sources=Price(exclusion_reason=reason),Price(exclusion_reason=reason),[]
            else:
                query={"title":b.title,"author":b.author,"isbn":b.isbn}
                replacement,sources=await self.prices.quote(query,locale,"new")
                used,more=await self.prices.quote(query,locale,"used")
                sources+=more
            async with self.lock:
                for ref,evidence in sources:
                    self.store.put_json(self.state,ref,evidence)
            results.append({"id":b.id,"title":b.title,"frame_ref":b.frame_ref,
                            "replacement_cost":replacement.model_dump(mode="json"),"used_value":used.model_dump(mode="json")})
        output={"country":locale.country,"currency":locale.currency,"books":results,
                "coverage":len(results),"required":10,"same_physical_ids":True}
        async with self.lock:
            self.store.put_json(self.state,f"observations/second_locale_{uuid4().hex[:12]}.json",output)
            self.store.save(self.state)
        return output

    async def close(self):
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers,return_exceptions=True)
        self.workers=[]
        self.pricing_workers=[]
        for task in self.catalogue_cache.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.catalogue_cache.values(),return_exceptions=True)
