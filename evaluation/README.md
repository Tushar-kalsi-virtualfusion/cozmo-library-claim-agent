# Independent ground truth

Use a new copy of [the submission workbook](../submission/ground-truth-and-results.xlsx) and fill its independent truth areas **before tuning**. Keep the submitted observations unchanged as historical evidence. Select all 20 spine samples and all 15 price samples before looking at prediction errors. Keep the original workbook and a timestamped frozen copy.

The evaluator accepts a JSON export with the following rows. Copy `truth.template.json` to a private `room.truth.json`; fill actual observations, never sample values. All `*.truth.json` files in this folder are ignored by Git. `frozen_at` is the ISO timestamp of the independent measurement set. Record the later physical-ID mapping separately so it cannot change what was measured.

| Array | Required row fields |
|---|---|
| `books` | `id` (physical truth ID), `predicted_id` (packet ID or null), `legible` (boolean), `title` (actual title, blank if illegible); optional `acceptable_titles` for independently established variants |
| `identity_checks` | `predicted_id`, `correct` (true/false), for predictions whose correctness was independently checked, including illegible spines. Unverified confident predictions are not treated as correct. |
| `spines` | Exactly 20 preselected rows: `predicted_id`, `height_cm`, `thickness_cm` |
| `prices` | Exactly 15 preselected rows: `predicted_id`, `kind` (`replacement_cost` or `used_value`), `amount`, `currency`, `country`, `condition_assumed`, `source_type` (`retailer_offer`), plus your independently checked source URL/date for auditing |
| `items` | Every physical non-book item: `id`, `predicted_id` or null, `category`; optional independently established `acceptable_categories` |
| `room` | Tape-derived `floor_area_m2` and gross `wall_area_m2`, plus dimensions and method |

Set `book_count` to the complete physical count. Every legible spine belongs in `books`, including titles the system missed. Never remove rows just because they are missing from the packet. Runtime code does not import or read this folder.

```sh
.venv/bin/python -m claim_agent.evaluate \
  --packet data/SWEEP_ID/claim_packet.json \
  --truth evaluation/room.truth.json \
  --output data/SWEEP_ID/evaluation.json
```

The command writes machine-readable per-row results and a Markdown table alongside them. Compare those results with the workbook's calculated fields. A missing observation fails sample coverage; absent ground truth or timestamps stay unverified. A synthetically generated packet is visibly marked synthetic and must not be submitted as real accuracy evidence.

## Paired camera and LiDAR evaluation

The authenticated comparison endpoint saves a frozen `lidar_packet.json`, fresh
`camera_packet.json` and `comparison.json` below
`data/SWEEP_ID/camera-comparison/`. The runtime comparison reports response and
coverage differences, not accuracy. Source and replay use the same JPEGs and
calibration, but the camera analysis receives neither depth nor RoomPlan. Reused
AR poses came from a LiDAR session, so this is a same-frame depth ablation rather
than a pure sensor benchmark. Camera counts currently carry unresolved duplicate
risk because this pipeline does not establish cross-frame physical identity
without depth.

For offline accuracy evaluation, start with the frozen independent truth schema
above and replace each `predicted_id` with an explicit mapping for the two saved
packets. All physical truth and measurement rows remain shared. A human must map
each mode independently; a missing prediction stays `null` and remains in recall
and sample coverage. Never copy LiDAR physical IDs into the camera packet.

```json
{
  "frozen_at": "INDEPENDENT_MEASUREMENT_TIMESTAMP",
  "book_count": 1,
  "books": [{
    "id": "PHYSICAL_TRUTH_ID", "legible": true, "title": "INDEPENDENTLY_READ_TITLE",
    "predicted_ids": {"lidar": "LIDAR_PACKET_ID", "camera": null}
  }]
}
```

The snippet illustrates the schema, not measured assessment values. Apply the
same mapping to `items`, `spines`, `prices` and `identity_checks`. Each identity
check must also supply `"correct_by_mode": {"lidar": true, "camera": false}`
with independently verified values. Use `null` for unchecked correctness or an
unmatched prediction ID. A shared `correct` field is rejected because correctness
belongs to a prediction, rather than the physical truth row.

`frozen_at` must be an ISO timestamp with a timezone, no later than the original
LiDAR packet's `sweep.captured_at`. Freeze the independently measured physical
truth before capture. Human prediction mappings and correctness checks may be
added later; they do not change that original freeze timestamp.

```sh
.venv/bin/python -m evaluation.compare_packets \
  --comparison-dir data/SWEEP_ID/camera-comparison \
  --truth evaluation/paired-room.truth.json \
  --output outputs/paired-evaluation.json
```

The command evaluates the same frozen truth rows for each packet with the existing
evaluator. It also reports explicit camera-minus-LiDAR deltas for count error,
title recall, confidently wrong predictions, spine error/coverage, object recall,
and floor/wall error. Fraction/error deltas use percentage points and identify
whether higher or lower is favorable; unknown operands produce null deltas.
It preserves missing observations and synthetic labels. It does not
make the original capture timing comparable to replay timing or supply ground
truth for you. The voice session can request the comparison; the browser's
“Compare camera and LiDAR” button starts or reopens the same saved analysis.

Wrong-identity count/fraction deltas remain unknown if either mode still has unchecked high-confidence predictions. Inspect each mode’s verified coverage before interpreting an apparent improvement.
