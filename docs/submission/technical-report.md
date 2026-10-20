# Library Contents Claim Agent — technical report

**As-built snapshot: 20 October 2026.** This report explains the native iPhone and paired Python service in this repository. It expands the [one-page architecture note](../architecture.md); the main diagram is also available as [Mermaid source](architecture.mmd). Requirements come from [the assessment brief](https://docs.google.com/document/d/1U-Q1EBCZl9hDmOzVfPOmk453Qe5UAM_AZKcmISPbcec/edit) (reviewer access required). Fresh software checks are recorded in [submission validation](validation.md).

The phone handles camera/depth acquisition, local proposals, OCR, preview tracking, capture selection and audio I/O. The Mac coordinates server vision, physical identity, deterministic geometry, sourced valuation, voice actions, persistence and packet generation. Models propose observations; validated server state controls claim numbers.

This is not a completed measured-accuracy submission. A saved physical sweep and repair evidence exist, but the required full library ground truth and pass-bar evaluation have not been established. The most recent pricing run encountered `credit_balance_exhausted`. Configuration and successful earlier provider probes do not establish present funded access.

## 1. Execution boundaries and model responsibilities

| Component | Runs where | Input and responsibility | Authority limit |
|---|---|---|---|
| ARKit and RoomPlan | iPhone | Shared RGB/depth/pose stream; surfaces, furniture boxes and capture coaching | Live measurements are provisional; accuracy needs physical validation |
| YOLOE-26s prompt-free INT8 Core ML | iPhone | 640 × 640 inference on full image and two overlapping detail regions; complete 4,585-label vocabulary from metadata | Proposals, not accepted inventory, dimensions or prices |
| Apple Vision OCR | iPhone | Accurate text recognition; position/confidence mapped to calibrated image axes | Independent observed text, with possible omissions/errors |
| Apple Vision object tracking | iPhone | Carries dotted preview outlines between detections | Presentation only; does not create inventory or claim geometry |
| GPT-6 Astra | OpenAI, called by Mac | Structured inspection of upright full-resolution evidence: individual book spines, shelf levels and other objects | Output is validated; no generated dimensions, prices or totals |
| GPT-5.4 Mini web search | OpenAI, called by Mac | Discovers relevant product-page URLs | Generated prose is never price evidence |
| GPT-Realtime-2.1 | OpenAI through Mac WebSocket | Streaming conversation, speech and guarded tool requests; low reasoning effort, `marin` voice | Reads server facts; validated tools control mutations |
| `gpt-4o-mini-transcribe` | Realtime input transcription | Asynchronous input transcript for display and action authorization | Ordinary output audio does not wait for transcription |
| Python / NumPy / Shapely / Decimal | Mac | Geometry, tracking rules, offer validation, revisions, totals, JSON/HTML/ZIP | Deterministic calculations and explicit rejection paths |

The bundled model is approximately 12.5 MB. It is a compact vision detector, not a generative small language model. Its export preserves all 4,585 class IDs through an INT32-to-FP32 path; an all-FP16 cast had rounded IDs above 2,048. Provenance, hashes and the AGPL-3.0 or enterprise license are recorded in [model documentation](../../ios/LibraryClaim/Models/README.md). Model confidence is an uncalibrated score, not measured accuracy.

**Active versus historical paths.** Current `DeviceIntelligence.swift` returns `source=cloud_requested`, `status=unavailable`, empty semantic clues and `needs_cloud=true`. No Apple Foundation Models inference runs in this path. `local_first=true` means local proposals/OCR, capture quality, novelty and bounded escalation; it does not mean cloud-free semantic recognition. The server retains a validated local-clue/Open Library corroboration path for compatible inputs, but the current phone does not produce those semantic clues. Catalogue editions never become observed editions. Older descriptions of an on-device text model are historical.

Claude, ElevenLabs and Gemini adapters remain for earlier configurations and tests. Some shared classes retain historical names such as `ClaudeAssistant`. Active OpenAI routing uses Realtime for speech/conversation and Responses for vision/search; it does not silently fail over to those providers. Apple Speech recovery code is not the active Realtime voice route.

## 2. One sweep: timing and coordination

The camera preview may start before setup, but evidence saving and the sweep clock wait for readiness. Spoken setup collects language and a supported market: India/INR, United Kingdom/GBP or United States/USD. Sensor capability selects LiDAR where available; camera-only is the fallback on supported non-LiDAR hardware. A temporary depth gap never silently changes a confirmed LiDAR session into camera-only capture.

The setup gate associates the complete readback with its Realtime response and the phone's actual playback acknowledgment. A distinct intelligible owner turn confirms setup; a later distinct turn answers readiness. Interrupted prompts, noise, missing transcripts and stale responses cannot satisfy these gates. The simulator supports conversation checks but cannot supply real capture evidence.

```mermaid
sequenceDiagram
  actor Owner
  participant Phone as iPhone capture and audio
  participant API as Paired Mac / SweepRunner
  participant RT as OpenAI Realtime
  participant Workers as Vision / geometry / pricing
  Owner->>Phone: Start; answer language and market
  Phone->>API: Authenticated onboarding + PCM
  API->>RT: Persistent voice session
  RT-->>Phone: Spoken full readback via Mac
  Phone->>API: Playback acknowledgment
  Owner->>Phone: Confirm, then answer separate readiness prompt
  API-->>Phone: Validated capture handoff
  Phone->>Phone: Start same-session RGB/depth/RoomPlan capture
  par Audio conversation
    Phone->>API: 16 kHz PCM chunks
    API->>RT: Stateful resampling to 24 kHz
    RT-->>Phone: 24 kHz audio through Mac; transcript events
    Phone->>API: Played-audio acknowledgments / interruption
  and Selected evidence
    Phone->>Phone: Detect, OCR, quality/select, atomically save outbox
    Phone->>API: JPEG + calibration + aligned depth/confidence
    API-->>Phone: Immutable receipt or backpressure
    API->>Workers: Bounded frame queue; independent pricing queue
    Workers-->>API: Validated identities, geometry, sourced offers
    API-->>Phone: Inventory revisions and dotted overlays
  and Room geometry
    Phone->>API: Latest RoomPlan estimate, at most every 3 s
    API->>Workers: Validate session and reconcile geometry
    Workers-->>API: Provisional dimensions and area
  end
  API->>RT: Fresh bounded facts and capture guidance
  Owner->>Phone: End sweep
  Phone->>Phone: Finish RoomBuilder; drain evidence uploads
  Phone->>API: Final same-session geometry, then finish
  API->>Workers: Wait within processing deadline
  API-->>Phone: JSON / HTML / evidence ZIP; explicit review queue
  API->>RT: Final current summary and unknowns
```

These branches have separate queues and lifecycles. Recognition and pricing need not finish before the user hears a reply. The active upload path supplies structured observations to voice rather than a second billable copy of every JPEG. Although the shared bridge supports image messages, routine sweep uploads do not call that path.

Audio uses AVAudioEngine voice processing, a dedicated dispatch queue, 16-bit mono 16 kHz input, stateful server resampling to 24 kHz and streamed 24 kHz output. Semantic VAD supports automatic barge-in; **Interrupt & speak** explicitly cancels playback. The server truncates conversation audio to the acknowledged played duration. Generation completion alone does not mean the owner heard it. Backgrounding, route interruptions, stop and reconnect invalidate stale callbacks and queued audio. Passive silence does not request periodic paid check-ins.

The cue ledger distinguishes upload accepted, recognition processed, existing physical records, missing clarity, sourced value and final processing. Before speech, it rechecks the current view and inventory revision. Duplicate image receipt alone never proves that an object was counted, and move-on guidance requires usable saved evidence. Owner corrections target exact IDs; ambiguous references require clarification. Claims about a first edition are stored as owner statements for review, not promoted to verified visual facts.

## 3. Phone capture, overlays and evidence

One ARSession supplies camera pixels, intrinsics, pose and LiDAR coordinates; RoomCaptureSession uses that session. Capture chooses the highest-resolution supported camera format at no more than 30 fps. The preview recognition timer requests work every 0.25 seconds, but in-flight work is guarded; this is not a claimed four-inference-per-second benchmark.

The detector runs a full-frame view and two overlapping detail crops. All native classes are eligible. Generic geometry maps proposals back to the original calibrated image, rejects crop-edge truncation and preserves full-image labels over conflicting detail predictions. At most 300 proposals enter metadata. There is no room-specific class list or hardcoded catalogue.

Capture requires normal AR tracking, adequate sharpness/exposure, fewer than four pending uploads and a minimum interval: four seconds with detections or ten seconds for an otherwise clear overview. Conservative visual-hash and camera-motion checks suppress near repeats. Improved sharpness or depth readiness can admit another view. Clear changed-scene overviews remain eligible even if the detector misses a class. Clarity requests are throttled to avoid repeatedly interrupting the owner.

The current UI draws dotted object borders with labels and available measurements. It does not display filled segmentation masks or projected mask textures; retained mask decoder fixtures support model validation/history. Local screen tracking uses resized frames and a bounded short in-memory history to handle detector delay; it expires lost/stale tracks. Those thumbnails are not evidence and are not uploaded. Measured world overlays have a separate supported-geometry path. A border on screen is not a counted or priced claim line.

`FrameEncoder` retains original-size JPEG evidence at quality 0.92, unrotated image dimensions, normalized polygons, column-major camera matrices, capture-relative time and AR-session ID. Depth is aligned little-endian float32 metres; confidence is one byte per depth sample. Server vision sees an upright image and returned polygons are mapped back before geometry. Camera-only serialization removes depth/confidence; the server rejects either depth or RoomPlan geometry in that mode.

The disk outbox writes each complete payload atomically before upload. A frame ID cannot later contain different bytes. Pending uploads survive connection failures and retry with backpressure delays; acknowledgments move files to retained uploaded storage. The outbox has a 1 GB storage bound. Preserving payloads does not establish automatic restoration of a live conversation or continuous AR coordinates after app termination.

## 4. Metric scale and geometry mathematics

Scale comes from LiDAR/ARKit metric observations, not a model's knowledge of a typical book or room. A four-corner spine polygon alone has no physical scale.

For pixel `(u,v)`, focal lengths `(fx,fy)` and principal point `(cx,cy)`, the camera ray in the implementation's ARKit convention is:

```text
r(u,v) = [(u-cx)/fx, -(v-cy)/fy, -1]
p(u,v,z) = z r(u,v)                # measured forward depth z, metres
n · (p-c) = 0                      # fitted plane, center c, normal n
t = (n · c) / (n · r)
q = t r                            # corner/plane intersection
q_world = T_camera_to_world [q,1]
```

Intrinsics and transforms are reconstructed from column-major arrays. Normalized polygon corners become raw JPEG pixels. The server expands the polygon's depth neighborhood by four depth pixels, keeps high-confidence depths between 0.15 and 5 m, retains samples within 8 cm of the median and requires at least 16 samples. SVD fits a plane; residual RMS above 1.5 cm, insufficient spread or an oblique ray denominator below 0.15 rejects the result. The four ray intersections yield edge lengths. The two opposite-edge averages become the longer spine side and shorter thickness, multiplied by 100 for centimetres. Implausible sizes and invalid calibration return `null` plus a review reason. These thresholds are engineering gates, not a calibrated uncertainty model.

For non-book objects, a planar visible face can provide two face-edge measurements. Those are not a full three-dimensional bounding box. Full width/height/depth comes from valid medium/high-confidence RoomPlan furniture boxes, linked conservatively to a visual item. Revised geometry clears obsolete dimensions before reassociation. Robust depth support used for tracking is identity-only evidence and never becomes object width or depth.

Floor area is the union of captured horizontal XZ polygons, preserving concavity. Length/width are the longer/shorter sides of the minimum rotated bounding rectangle; their product is not substituted for polygon floor area. Wall area sums valid planar wall polygons. Height is the median wall height, with variation over 10 cm flagged. Openings and shelving are projected into each associated wall plane, unioned and clipped to the wall. Gross wall, openings, net wall and shelving coverage are separate outputs. Missing/ambiguous shelving association leaves coverage unknown. Curved surfaces are flagged rather than assigned a planar substitute. Square feet use `m² × 10.76391041671`.

Occupied shelf run sums unioned horizontal book-footprint intervals within each supported shelf level, avoiding double-counting stacks. Missing required book/shelf geometry leaves the run unknown. Camera-only mode has no implemented monocular reconstruction, reference-object scale or reliable general metric association; room areas and dimensions remain unknown.

## 5. Recognition and three duplicate safeguards

Server admission validates JPEG type/calibrated dimensions, depth/confidence byte lengths, session continuity and immutable signatures. Quality checks run independently of phone claims. In active local-first mode, clear novel unresolved views may reserve a vision attempt; the reservation is saved before the provider call. Low-quality views, exhausted budgets, cooldowns and already attempted evidence remain explicit routing outcomes.

GPT-6 Astra receives the whole image and untrusted local hints. Its structured result separates individual book spines, shelf levels and other objects. Each quadrilateral is validated independently; one malformed polygon does not discard all valid detections. Books require visible text supported by independent OCR; unreadable titles stay blank and unidentified. Edition and ISBN guesses are not filled from catalogue familiarity. Model omission does not erase previously supported local identities.

1. **Image duplication:** SHA-256 identifies exact JPEG bytes; perceptual similarity additionally requires a nearly stationary pose. Content-addressed JPEG blobs are hard-linked to per-frame references. Exact repeats retain their own metadata, depth and confidence. Existing RGB polygons may be remeasured against newly useful depth without a second vision call. A different pose cannot reuse a dimension just because JPEG bytes match. Near-repeat frames carrying depth are retained conservatively.
2. **Physical identity:** `TrackRegistry` checks kind, compatible category, AR session and one-to-one frame membership. It prefers a matching projected RoomPlan object, then reliable observed depth support or measured corners. For supported non-book cases it can use calibrated visual overlap and bounded SIFT scene correspondence with local geometric support. Contradictory measured depth blocks a visual merge. Competing candidates are rejected as ambiguous; name equality never merges physical copies. Tracks retain at most eight views. This reduces duplication but does not prove all physical counts.
3. **Repeated work:** Vision reservations and signatures survive failures. Pricing has durable status, record revision and query fingerprints; equivalent concurrent lookups are coalesced. Stale/interrupted work cannot silently restart a paid request or overwrite a correction. Provider response IDs suppress duplicate usage accounting.

The conservative repair function replays hash-verified observations in a read-only preview; it neither calls providers nor saves by itself. It keeps uncertain mappings, original observation IDs, aliases and prior invalidated prices auditable. The recorded application to a saved 21-frame sweep reduced **87 to 76 item records**, including **four chair records to one**, with three books unchanged. Four ambiguous identities and one related target were untouched; original state was backed up. This demonstrates repair of saved evidence, not a ground-truth assertion of 76 physical items or a new full library capture.

## 6. Sourced prices, revisions and output

Identification, measurement and pricing are separate stages. Pricing admits identified eligible records only. Books request new replacement and used value separately; used price is never inferred by discounting new price. Other objects request replacement offers using an exact supported product or an explicitly labelled equivalent/range when sufficient attributes support it. Art/portraits default to appraisal until explicit owner print confirmation; rare/signed signals and retrieved values above INR 10,000 equivalent also require appraisal.

Search returns at most eight public product URLs from citations/source metadata. The server independently fetches pages and parses structured JSON-LD products/offers. Acceptance checks product/variant identity, observed brand/model or supported equivalent, material/dimensions where available, condition evidence, stock, currency and market. Search answer prose never supplies a price. No retrievable supported offer means unknown and exclusion from totals. Blocked pages and unstructured retailers remain a coverage limitation.

Each accepted price preserves amount/range, market/currency, condition assumption, exact/equivalent match basis, product/source URL, retrieval timestamp and saved extraction reference. Search traces are retained. If a local offer is unavailable, foreign fallback searches the UK, or India for a GBP target, then converts using retrieved Frankfurter FX evidence. Original amount/currency, rate, FX URL/date and a converted flag remain visible. No current exchange rate is hardcoded.

Price and URL-search caches are separately bounded to 256 entries with a 900-second TTL. Equivalent concurrent queries share in-flight work. Search terms can be reused when dimensions change, but each actual dimensional query still validates offers. Revision/fingerprint checks prevent a quote for an old identity, ownership state or measurement entering the current record. Corrections invalidate dependent valuations. Earlier provider work with ambiguous attribution is not silently replayed when opening saved inventory.

The same ten identified physical book IDs can be requested in a second supported market without changing the primary locale. This capability is not evidence that the required ten-book comparison has been completed.

`packet.py` builds typed `claim_packet.json`, derives `report.html` from the same serialization and packages immutable evidence into `packet.zip`. Monetary arithmetic uses `Decimal`; unknown numbers are `null`, and money serializes as decimal strings. Every book/item frame reference must resolve. Prices lacking eligible identity, dated source, condition or conversion evidence are removed from totals. Review entries name the affected record and reason. Completeness is `partial` when review needs remain. Runtime code never reads ground truth.

## 7. Runtime settings, costs and failure behavior

This snapshot separates verified active settings from source defaults/limits. It contains no secrets.

| Control | Active setting or implemented bound | Consequence |
|---|---|---|
| Provider / vision / voice | OpenAI / `gpt-6-astra` / `gpt-realtime-2.1` | Historical providers inactive |
| Local-first / paid pricing / cloud voice | All enabled | Selected images and eligible valuation may incur cost |
| Vision routing | 20 attempts/sweep; minimum 6 s apart | Source defaults are 3 attempts / 30 s; workspace permits more |
| Frame workers / server queue | 2 workers / 12 queued frames | Backpressure keeps phone evidence on disk |
| Pricing workers / queue | 4 workers / 250 jobs | Independent of audio and frame queue |
| Provider adapter cap | 240 requests per provider instance | Separate from 20-image admission; vision/search share adapter |
| RoomPlan upload | At most every 3 s; one in flight | Coalesces provisional updates; finalization awaits current work |
| Frame/body bounds | 360 frames/sweep; JPEG ≤10 MB; HTTP body ≤22 MB | Oversized/excessive evidence rejected |
| Final processing wait | Source default 240 s, configurable ceiling 270 s | Partial results on deadline; no guarantee of total under-five-minute acceptance |
| Quote deadline | 45 s per quote; four concurrent | Timeout leaves unknown; no automatic paid replay |
| Appraisal threshold | INR 10,000 equivalent | High-value offers retained as evidence, excluded from automatic value |

Live audio has a separate bounded queue. Dropped audio chunks are counted and trigger a request to repeat missed speech. The event stream publishes revisions and overlays independently of voice generation. This is a local single-process demonstration with bounded concurrent sweeps, in-process workers and filesystem snapshots, not a distributed durable job system.

The cost ledger uses reported usage, including cached input, output and Realtime text/audio/image modality splits, input transcription and reported search-tool calls. Its dated rate card is in `costing.py`. In general, cost is `(uncached input × input rate + cached input × cache rate + output × output rate) / 1,000,000`, summed by model/modality, plus supported search/cache-write fees. It counts reported tool calls rather than assuming one execution per API request. Onboarding usage transfers into the sweep once. Estimates are not invoices and exclude hosting, tax, device and network expense. Unknown usage/rates and ambiguous interrupted requests are uncosted; a partial known subtotal is not the full bill.

Telemetry includes `identification_stages`, `pricing_stages`, `provider_usage`, `live_usage`, `live_first_audio_ms`, `backend_finalize_s` and `time_to_packet_s`. Final timing conservatively includes native finalization/upload delay using server creation plus native monotonic capture duration. Provider first-audio timing differs from phone audible latency. No end-to-end latency or accuracy is inferred from health or a synthetic fixture.

| Failure | Implemented response |
|---|---|
| LAN loss or backpressure | Retain atomic phone payloads; retry evidence upload with delay |
| Blur, darkness, glare or crop | Request improved view within the continuous pass; do not invent hidden objects |
| Invalid depth/calibration | Preserve visual evidence; unknown measurements and review reasons |
| Billing/access error | Latch further affected provider work off; retain unknowns and error usage |
| Interrupted/uncertain paid request | Mark uncosted/unknown; do not automatically repeat |
| Late quote after correction | Revision and eligibility checks reject stale results |
| Final processing deadline | Cancel unfinished workers, exclude unfinished values, emit partial packet |
| Server restart during capture | Mark partial; require a new continuous capture rather than join AR sessions |
| Audio stop/background/route failure | End/reconnect voice with stale callbacks discarded; visuals have their own lifecycle |

## 8. Module map

| Area | Main files | Review focus |
|---|---|---|
| Native orchestration/transport | `CaptureController.swift`, `BackendClient.swift`, `EvidenceOutbox.swift` | Session lifecycle, setup, durable evidence and finalization |
| Native recognition/selection | `LocalObjectDetector.swift`, `LocalRecognitionModels.swift`, `FrameEncoder.swift`, `DeviceIntelligence.swift` | Proposals, OCR, coordinates and cloud request |
| Native display tracking | `LocalPreviewTracker.swift`, `PreviewTrackingModels.swift`, `ObjectOverlay.swift`, `OverlayModels.swift` | Dotted outlines, freshness, separation from inventory |
| Native measurements/audio | `RoomGeometry.swift`, `MeasurementModels.swift`, `LiveAudio.swift` | Geometry encoding/coalescing and playback accounting |
| API/setup | `server.py`, `config.py`, `onboarding.py` | Pairing, bounds, provider routing, spoken authorization |
| Sweep coordination | `pipeline.py`, `models.py` | Independent queues, revisions, corrections, final deadline |
| Recognition/routing | `vision.py`, `local_evidence.py`, `image_selection.py`, `openai_provider.py` | Structured output, OCR, novelty, spend reservation |
| Identity/scale | `tracking.py`, `object_anchor.py`, `visual_correspondence.py`, `geometry.py` | One-to-one matches, conflicting evidence, dimensions |
| Live interaction | `openai_live.py`, `live.py`, `sweep_guide.py` | PCM, playback gates, tools and current guidance |
| Valuation/evidence | `pricing.py`, `providers.py`, `storage.py`, `packet.py`, `costing.py` | Retrieved offers, hashes, arithmetic, usage provenance |
| Replay/evaluation | `inventory_repair.py`, `comparison.py`, `evaluate.py`, `evaluation/compare_packets.py` | Audit-preserving repair, depth ablation, external ground truth |

Native paths are under `ios/LibraryClaim/`; unprefixed server files are under `claim_agent/`. Tests under `tests/` and `ios/Tests/` cover geometry, identity, price revalidation, voice gates, reconnects, outbox and overlays.

## 9. Setup, privacy and security

Development uses a Mac with Python 3.12, pinned `requirements.lock`, Xcode/signing and a physical ARKit-capable iPhone. LiDAR measurement requires supported hardware; this workspace uses an iPhone 17 Pro. The server starts with `python -m claim_agent` on port 4180. Follow [repository setup](../../README.md) and [physical validation](../setup/iphone-validation.md). Toolchain installation, signing and account provisioning are prerequisites, not part of a measured fifteen-minute fresh setup claim.

Credentials remain in ignored server configuration. `scripts/configure_ios.py` copies only local host, port and pairing token into ignored phone build configuration. A stable `.local` hostname avoids Wi-Fi IP churn. Wildcard binding uses IPv4 and IPv6 where supported; default loopback binding must be changed for phone access. `/api/health` reports configuration, not secrets or proven credit.

HTTP endpoints and WebSockets enforce shared pairing; same-origin checks, payload limits, path validation and hashes protect the evidence interface. Source retrieval allows public HTTP(S) on standard ports, validates redirects, rejects local/private destinations, bounds downloads and never forwards provider credentials. Image text and pages are data, not instructions authorizing mutations. HTML rendering escapes content.

This is demo security: LAN HTTP/WebSocket traffic is unencrypted, and the shared pairing secret grants access. Use a trusted network; production would require TLS, individual authentication/authorization and retention controls. Frames, OCR, transcripts/events, geometry and receipts may contain sensitive household information. Selected images go to OpenAI vision; microphone audio streams to OpenAI Realtime. The application does not persist raw microphone PCM, but retains transcripts/events and evidence locally. Responses requests set `store=False`; that does not establish a blanket provider-retention guarantee. Automatic face/document redaction and scheduled evidence deletion are not implemented. The operator must avoid people/private documents, obtain permission and manage retained files.

## 10. Evidence, limitations and assessment coverage

Latest recorded software verification is **362 passing Python tests**, scoped repair regressions and a clean whitespace/diff check. Native signed builds, installation, small real-photo checks and voice troubleshooting are documented historically. These prove execution of specific components, not whole-room accuracy. The small detector timing sample must not be generalized to continuous phone throughput.

The saved 21-frame sweep demonstrates evidence persistence and the reported duplicate repair. Its three recorded books and 76 unvalidated item records do not satisfy the required ≥60-book, two-shelving-unit, ≥8-non-book-item evaluation dataset. Floor area and room length/width are unknown in this saved result. Remaining ambiguous mappings are exposed. A sourced chair comparable exists in the repair history; it does not establish room-wide retailer coverage. Later pricing exhausted credit and left unsupported values unknown.

The saved usage ledger reports a **known cost subtotal of USD 6.01116405, with partial cost status: 191 costed and one uncosted request**. This is the implemented API-usage estimate, not a complete billed sweep cost. It must not be rounded into a claim that all provider charges are known.

| Assessment measure | Required bar | Supported status |
|---|---|---|
| Book count | Within 5% of true count | Not established against full physical ground truth |
| Title identification | ≥70% of human-legible spines; ≤3% confidently wrong | Not established |
| Spine dimensions | Within 15% on 20 measured spines | Method/regressions exist; physical sample not established |
| Book prices | Within 25% on 15 hand-checked prices, same source type | Validation pipeline exists; required sample not established |
| Non-book detection | ≥80%, correct category | Repair is not a complete labelled detection evaluation |
| Floor / wall area | Within 10% / 15% of tape measurements | Method exists; physical comparison not established |
| Packet latency | Under 5 minutes after sweep | Bounds/telemetry exist; scored run not established |
| Second locale | Same 10 books | Workflow exists; completed comparison not established |

Key limitations are thin/reflective or occluded spines, broad detector false positives, nonplanar objects, incomplete RoomPlan surfaces, cautious merging and retailer JSON-LD/access coverage. Confidence scores are uncalibrated. Quality warnings can remain in review until a supported resolution exists. Masks are not a current display feature. There is no continuous video-to-model stream: continuous camera preview yields selected still-image evidence while audio streams separately.

The optional camera comparison freezes a LiDAR result and independently reanalyses its JPEGs without depth, RoomPlan, inherited inventory or prices. It preserves hashes and independent usage. Reused AR poses may have benefited from LiDAR: this is same-frame depth ablation, not a pure hardware benchmark. Replay latency differs from capture latency; response deltas need independent physical-ID mappings and frozen ground truth before they become accuracy results.
