# Failure log — real-device run, 20 October 2026

Sweep `ad0ab3c707b149948ed21e4593fe3675`, revision 406, remains **partial**. Independent ground truth was not collected, so accuracy and price-error rates are unverified.

1. **Repeated objects inflated the inventory.** The saved run contained 87 non-book records. Replayed observations supported 11 conservative merges, leaving **76**. Cross-frame identity association had split repeat views into separate records; occlusion, changed views and incomplete metric associations contributed. The post-capture repair replayed **21 frames**, retained alias/provenance links, skipped all 3 books and kept uncertain histories separate. Four identities remain ambiguous and one group was blocked. **Measured fix:** 87 → 76 records, not a verified correction to 76 physical objects. Next: freeze an independent object list and evaluate track matches on an unseen room.

2. **Capture did not demonstrate the required library or finish room geometry.** Only **3 book records** were produced; the required 60-book/two-unit scenario is not demonstrated. Floor area, room length/width, sweep-end time and packet-ready time are missing. Review records show blur, occlusion, cloud cooldown and local-analysis gaps; the run was recovered after a server restart rather than finalized continuously. The main **169.9683 s** video has **no audio track**; a separate **47.4083 s** recording has audio and shows saved inventory after repair. **Change needed:** require coverage and final RoomPlan export before declaring completion, verify microphone/audio capture before recording, then repeat a qualifying uninterrupted run. No measured end-to-end fix is claimed.

3. **Pricing coverage was poor and one retained book match is too loose.** Only one book replacement offer (**₹273**) and one chair comparable (**₹3,999**) remain; **77 records** are excluded from totals and all used-book prices are missing. The ledger records `credit_balance_exhausted`. The Atomic Habits URL is a **Hindi-edition** listing while observed author, edition and language are unverified; its match-basis text mentions an author that the packet leaves blank. This is a sourced equivalent, **not an exact or hand-checked match**. The chair's identity/material/dimensions are also unverified. **Change tried:** conservative identity repair and sourced comparable pricing with missing offers excluded. **Next:** enforce language/edition/author agreement, bound retries and stop admission on exhausted quota. Price accuracy remains unmeasured.

| Recorded timing | Entries | Aggregate | Median | Maximum |
|---|---:|---:|---:|---:|
| Identification | 21 | 275.676 s | 0.051 s | 43.886 s |
| Pricing | 207 | 1,741.873 s | 2.126 s | 56.542 s |
| First response audio | 3 | — | 1,017 ms | 1,074 ms |

**Cost:** known estimate **US$6.01116405**, **191 costed / 1 uncosted** requests, status **partial**. Includes recorded onboarding and repair/retry usage; not an invoice or isolated capture-only cost. Unknown billing is not zero. Excludes tax, hosting, network and device costs. Stage sums include overlapping calls and short admission outcomes, so they are not wall-clock latency. Geometry/export timing and end-to-packet latency are unavailable.

**With two more weeks:** first collect frozen full-room truth and a qualifying audible one-take video; then evaluate identity tracking, spine scale and source matching against that baseline and unseen shelves. Sources: [current packet](../../submission/evidence/current/claim_packet.json), [timing summary](../../submission/evidence/current/metrics-summary.json), [results](results.md).
