# On-device recognition model

Current display: dotted borders only, including live and saved-frame views. Mask
textures are not rendered, and the Apple Vision foreground-segmentation task is
not used. The decoder and historical mask fixtures below remain available for
model validation.

The app bundles Ultralytics **YOLOE-26s prompt-free**, INT8 Core ML, approximately
12.5 MB. The model owns its complete 4,585-name vocabulary. The app reads names
from model metadata; it has no household class list, label-specific switch,
category-specific threshold or detail-view whitelist. It is a compact vision
model, not a generative text SLM. The model vocabulary is broad but finite.

Source: https://github.com/ultralytics/assets/releases/download/v8.4.0/yoloe-26s-seg-pf.pt
Source SHA-256: `01605da3f2e0e5325df45d5f5eef8e40b5b5c7d085399be3c33ac638258b150a`
Bundled weight.bin SHA-256: `d1f2a6dd2279730c9ccf42dcc6c4091068622a40cb2bd76e49ea105f861540c6`
Bundled model.mlmodel SHA-256: `8b385f0c586d8cb71f739ae639697898add2386463a18e8972ff3ce8d7a66df4`
Docs: https://docs.ultralytics.com/models/yoloe/
License: AGPL-3.0, or a separate Ultralytics enterprise license.
https://ultralytics.com/license

Reproduce using `scripts/export_household_model.py --weights /path/to/yoloe-26s-seg-pf.pt --output /path/to/household.mlpackage`
in the pinned isolated Python environment documented in that script. No text
encoder, manually selected prompts or provider key is bundled. The static export
preserves the learned proposal filter with fixed-shape masked scores instead of
dynamic indexing; the export script documents the upstream behavior it adapts.
The backbone uses FP16 compute and INT8 weight palettes, while casts and
concatenations retain FP32. Class IDs remain INT32 until an FP32 output cast:
an all-FP16 export silently rounds IDs above 2,048 to different categories.
`scripts/check_model_export.py` tests the actual YOLO postprocessor through
Core ML and verifies every one of the 4,585 IDs exactly; `--unsafe-default`
reproduces the original rounding failure.

The metadata contract pins model/version, complete labels, 640x640 input,
output name/shape, and one global score threshold. The first six values of
[1,300,38] are xyxy, confidence, class id. The remaining 32 coefficients combine
with [1,32,160,160] prototypes to produce each proposal's instance mask locally.
Masks are cropped to the proposal, unletterboxed, rotated and mapped through
detail crops alongside the boxes. No server result is required. Textures stay
on the device and are excluded from frame JSON. The current UI ignores these masks and does not run Apple Vision foreground
segmentation; all object borders are dotted.
The phone examines the full image and two overlapping detail regions for every
class. Crop-edge truncations are rejected, coordinates return to the calibrated
AR image, full-image labels take priority over overlapping crops, and geometric
suppression is class-agnostic. At most 300 highest-confidence proposals are sent.

Evaluation: native Core ML finds the photographed AC (air conditioning), coffee
table (cocktail table), and framed/decorative wall art. However, raw prompt-free
labels also include false objects and scene descriptions, and can confuse a
charger with a converter/battery and a monitor base with equipment. These results
are explicitly labelled Possible; they are not verified inventory or accuracy.
The current DeviceIntelligence adapter requests cloud interpretation and does
not invoke an on-device Apple language model. Core ML detection and Vision OCR
still run locally. The server checks book identity against independent OCR and
catalogue evidence, and escalates clear unresolved images within its budget.
Other object identities remain provisional pending server confirmation. No label
supplies metric dimensions or prices. Real accuracy requires a labelled test set
and room measurements.

Local mask regression checks cover prototype arithmetic, crop/rotation mapping,
invalid masks, serialization exclusion and capture-age/pose gates. Actual Core ML
execution on saved AC, table and artwork photos produced 5/5, 21/21 and 12/12
proposal masks respectively on the Mac. This proves mask generation without a
cloud event, not mask accuracy or phone latency. The screen-space preview expires
two seconds after capture, or sooner after 2 cm / 3 degrees of camera movement;
it must not pretend to be an anchored world-plane measurement.

Physical iPhone 17 Pro verification, 2026-10-17 23:55 IST: the signed model was
installed over Wi-Fi. A bounded six-photo diagnostic run recorded this exact
model ID. Four photos containing the AC returned `air conditioner` at scores
0.64–0.81; two frames aimed elsewhere did not. The photos were retrieved and
inspected. Three-view detection took 705–895 ms per saved phone frame (roughly
0.7 seconds warm); this is a small diagnostic sample, not a throughput or
accuracy benchmark. Saved room photographs tested on the Mac returned
`cocktail table` at 0.59 and `decorative picture` at 0.81–0.90. Broad vocabulary
false positives remain, including scene/action labels. Confidence is a model
score, not measured accuracy; server confirmation is still required.

The earlier COCO YOLO26n asset remains available only as an offline baseline and
is not bundled in the app. Source:
https://github.com/ultralytics/yolo-ios-app/releases/download/v8.3.0/yolo26n.mlpackage.zip
ZIP SHA-256: `4d77f5470e3816e6e56fb679bf88b7d24a2d1453951be216b348eec042b86af6`
