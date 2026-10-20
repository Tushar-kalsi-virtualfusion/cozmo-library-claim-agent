# Results and evidence

This is an incomplete real-device prototype submission. No accuracy pass bar is claimed. The original ground-truth workbook contained only instructions, thresholds and blank observation tables. Independent counts, hand-read titles, 20 spine measurements, 15 price checks, an item list and room tape measurements were not recorded. The submitted workbook preserves those missing inputs and shows the actual system outputs separately.

The current packet is sweep `ad0ab3c707b149948ed21e4593fe3675`, revision **406**, captured **20 October 2026 at 01:21:36 IST**. Its saved completion status is **partial**. It contains **3 book records** (1 identified, 2 unidentified) and **76 non-book records**, following a post-capture repair that merged 11 item records from the previous 87. These are predictions, not verified physical counts. Four item identities remained ambiguous and one repair group was blocked.

| Requirement or pass bar | Current evidence | Result |
|---|---|---|
| Real enclosed room; at least 60 books, 2 shelving units, 8 non-book items | Real iPhone/LiDAR capture; 3 book and 76 non-book system records. No independent scene count or shelving-unit count. | Required library scenario not demonstrated |
| Book count within 5% | 3 system records; true count absent | Unverified |
| At least 70% of human-legible titles correct; at most 3% confidently wrong | 1 predicted title; no complete human-legible list or identity audit | Unverified |
| Spine dimensions within 15% on 20 books | 2 records have metric height/thickness; no hand-measured sample | Unverified |
| Book prices within 25% on 15 comparable checks | 1 replacement offer, no used offers; no independent checks | Unverified |
| Non-book category recall at least 80% | 76 predictions; independent item list absent; residual duplicate risk | Unverified |
| Floor area within 10% | Floor area, length and width unavailable; tape truth absent | Unverified; system output incomplete |
| Wall area within 15% | Provisional gross wall area 48.6542 m² / 523.7093 ft²; tape truth absent | Unverified |
| Packet within 5 minutes after sweep ends | `ended_at`, `packet_ready_at` and `duration_s` are absent | Unverified |
| Same 10 books priced in a second country | No 10-book second-locale result accompanies this run | Not demonstrated |
| Unedited continuous demo with audible agent, live inventory and final packet | Main video 169.9683 s has **no audio track**. Separate 47.4083 s video has an audio track and shows the later saved inventory. | Full required demonstration not satisfied |
| Structured packet, readable report and openable evidence | Current JSON/HTML and 66 referenced assets packaged; all hashes and lengths verified | Delivered for the partial saved run |

## Valuation and geometry limits

The packet retains **₹273** in book replacement value and **₹3,999** in non-book replacement value. Only the non-book low/high endpoints are equal; this does not mean every item was priced. **77 records are excluded from valuation totals**, two book replacement prices are missing, and all three used-book prices are missing. The used-value total of zero is an empty sum, not evidence that the books have no used value.

The Atomic Habits offer points to a **Hindi-edition** listing. Observed author, edition and language were not established. It is labelled an equivalent offer in the saved run, not an exact match, and has not been independently hand-validated. The source's retained match-basis wording mentions an observed author although the packet author field is blank; this is an unresolved provenance defect. The chair offer is a retail comparable for an ALIZA Fabric Office Arm Chair; exact identity, material and dimensions are unverified. Both retained prices are shown as saved, without silently correcting the live valuation.

Room height is a provisional **2.9399 m**. The floor outline is empty. The recorded shelved-wall area of **0 m²** has no shelving associations and must not be treated as a verified physical zero. Metric scale comes from RoomPlan/ARKit LiDAR coordinates from the capture session. A saved metric estimate is not a tape-measure accuracy result.

## Timing and cost

| Recorded stage | Entries | Aggregate duration | Median | Maximum |
|---|---:|---:|---:|---:|
| Identification stage | 21 | 275.6758 s | 0.0509 s | 43.8855 s |
| Pricing stage | 207 | 1,741.8732 s | 2.1255 s | 56.5425 s |
| Live first-audio latency | 3 | Not additive | 1,017 ms | 1,074 ms |

Stage durations include short local/admission outcomes, retries and post-capture repair work; overlapping calls mean their sums are **not wall-clock completion latency**. There are eight recorded vision-model calls. Geometry/export stage latency was not independently recorded. End-to-packet latency cannot be reconstructed from the missing timestamps.

The recorded known API-cost estimate is **US$6.01116405**, with **191 costed requests and 1 uncosted request**. Cost status is **partial**, and the ledger includes onboarding and subsequent repair/retry usage associated with this saved run. A `credit_balance_exhausted` provider failure is recorded. This is not an invoice or a complete per-capture bill. Unknown charges are not zero; tax, hosting, network and device costs are excluded.

## Artifact map and provenance

- [Results workbook](../../submission/ground-truth-and-results.xlsx): actual system rows, blank human observation areas, guarded comparison formulas and unverified pass bars.
- [Current packet](../../submission/evidence/current/claim_packet.json) and [readable report](../../submission/evidence/current/report.html): the saved **post-repair** run shown by the supplemental inventory video. The main capture video predates these repairs, so this is not represented as a final packet produced during that uninterrupted take.
- [Evidence ZIP](../../submission/evidence/current-evidence.zip): extract it before opening `current/report.html` so local links resolve.
- [Manifest](../../submission/evidence/current/manifest.json), [validation record](../../submission/evidence/current/validation.json), and [timing summary](../../submission/evidence/current/metrics-summary.json).

The bundle includes exactly the packet's transitive evidence references: **8 frames, 14 depth files, 30 geometry records, 11 observation records and 3 price-source records**. `EvidenceStore.read_evidence` verified the original SHA-256 for every copied asset; the exported copies also passed SHA-256 and byte-length checks, **66/66**. All **9** local HTML link targets resolve. The manifest excludes 106 unreferenced diagnostic assets.

For public export, optional packet metrics were reduced to remove spoken setup, guidance transcripts, routing diagnostics, request/response identifiers and local connection context. All non-metric packet fields, valuation amounts, review reasons, geometry and referenced evidence bytes remain unchanged. No live state, `.env`, credentials or connection files are included. Text scans found no credential-like values or private-host paths in the exported JSON/HTML. Evidence hashes establish byte integrity, not recognition accuracy or human ground truth. The videos remain separate, unedited recordings.

The source requirements are in the [assessment brief (reviewer access required)](https://docs.google.com/document/d/1U-Q1EBCZl9hDmOzVfPOmk453Qe5UAM_AZKcmISPbcec/edit). Results were reconciled against the current saved state, its packet serialization and the original empty workbook; no new paid provider calls or live-state changes were made while preparing these artifacts.
