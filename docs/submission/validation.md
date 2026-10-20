# Submission validation — 20 October 2026

These checks validate the current software and packaging on this Mac. They do not
establish real-room identification accuracy, measurement error, speech quality,
retailer coverage, or the assessment acceptance scores. No paid provider request,
server restart, device installation, commit or push was performed for this audit.

## Fresh verification results

Environment: Apple silicon macOS, Python 3.12.12, Xcode 26.6 (17F113).

| Check | Result |
| --- | --- |
| Full Python suite, `python -m unittest discover -s tests -v` | **362 tests passed**; final run 11.451 seconds |
| JavaScript syntax, `node --check claim_agent/static/app.js` | Passed |
| README Swift wire/outbox checks | Passed: calibration keys, base64, immutable retries, disk recovery and decimal/null decoding |
| README Swift preview-tracking checks | Passed: moving borders, missed detections, identity, jitter, expiry and coordinates |
| README Swift Vision-preview checks | Passed: delayed/repeated detections, movement, missed detection and interruption; these are Mac fixture checks, not an iPhone performance benchmark |
| README Swift speech-selection checks, with warnings treated as errors | Passed |
| Parse every app Swift source | Passed |
| Unsigned iPhoneOS device-target build, with Swift warnings treated as errors | **BUILD SUCCEEDED**; approximately 23 seconds on this prepared Mac |
| Explicit synthetic replay | Passed after updating its fixture configuration: 3 books, 20 × 3 cm spines, 12 m² floor, 35 m² gross wall area, unknown prices |
| Fresh Python environment install from `requirements.lock` | Passed; approximately 28 seconds here, including some cached wheels |
| Fresh environment `pip check` | No broken requirements |
| Fresh environment `import claim_agent.server` | Passed; no service started |

The replay initially returned zero books because its declared detections were
behind the newer local-first admission policy and it supplies no phone OCR or
detector metadata. `scripts/replay_smoke.py` now explicitly sets
`local_first=False` for its synthetic provider. This keeps its original scope:
storage, tracking, calibrated geometry and packet mechanics. It does not test the
production local-first router or recognition accuracy. Production settings were
not changed. The existing fixture assertions passed, and the full Python suite
was rerun after the edit.

## Reproduction

Run the Swift compile-and-execute checks listed in the main README. This audit
used `/tmp/library-submission-*` executable names so it did not overwrite another
run's outputs. The main commands were:

```sh
.venv/bin/python -m unittest discover -s tests -v
node --check claim_agent/static/app.js
swiftc -frontend -parse ios/LibraryClaim/*.swift
.venv/bin/python scripts/replay_smoke.py /tmp/library-submission-synthetic-data
xcodebuild -project ios/LibraryClaim.xcodeproj -target LibraryClaim \
  -configuration Debug -sdk iphoneos \
  CONFIGURATION_BUILD_DIR=/tmp/library-submission-build/products \
  OBJROOT=/tmp/library-submission-build/objects \
  CODE_SIGNING_ALLOWED=NO SWIFT_TREAT_WARNINGS_AS_ERRORS=YES build
```

The fresh installation used the existing Python 3.12.12 interpreter solely to
create a new environment; it did not reuse the application's installed packages:

```sh
.venv/bin/python -m venv /tmp/library-submission-fresh-venv
/tmp/library-submission-fresh-venv/bin/python -m pip install -r requirements.lock
/tmp/library-submission-fresh-venv/bin/python -m pip check
/tmp/library-submission-fresh-venv/bin/python -c 'import claim_agent.server'
```

Local diagnostic logs are `/tmp/library-submission-python-tests.log`,
`/tmp/library-submission-build.log`, `/tmp/library-submission-fresh-install.log`,
and `/tmp/library-submission-{wire,preview-tracking,preview-vision,speech,swift-parse,javascript,replay}.log`.
These local logs and compiled application outputs are not submission artifacts.

## Setup feasibility

Installing the locked Python dependencies, importing the server and building the
unsigned app completed comfortably inside 15 minutes on this prepared Mac. This
supports the repository setup target after prerequisites are available; it is
not a timed clean-machine or signed-device onboarding claim. Python 3.12 must be
installed and accessible, and network speed/cache state affect installation.
The current workspace environment was created with `uv` and has no `pip` module;
the README's fresh ordinary-venv path was verified independently above.

Xcode installation, iOS SDK support, signing-team setup, trusting the phone,
Developer Mode, permissions and provider-account/model access remain external
prerequisites. No fresh model-access or credit check was attempted. Model export
requires a separate pinned environment described in
`scripts/export_household_model.py`; export is unnecessary when the complete
bundled asset is present. Downloading Xcode or re-exporting a model is outside the
15-minute target.

## Publication hygiene and model assets

A custom local scanner checked 185 candidate working-tree files and 300 blobs
from the all-refs Git object enumeration. It searched common provider/GitHub/AWS/
Slack key forms, private-key markers, and token-boundary matches to current local
credentials, printing paths/reasons only. No high-confidence secret match was
found. This was a targeted source/configuration scan, not a guarantee that every
possible secret format or binary payload is covered. Private capture media were
not scanned or copied.

An initial substring-only hit in `tests/test_pricing_search.py` was an eight-digit
numeric sequence inside two thirteen-digit ISBN test fixtures, not a credential
literal. The URL user-info match in `ios/Tests/InventoryValuationChecks.swift` is
an example-domain test fixture. Neither was treated as evidence of an exposed
provider key.

Keep these paths out of the public source submission:

- `.env`: local provider keys and pairing configuration; verified ignored,
  untracked and mode 0600.
- `ios/LocalConnection.xcconfig`: local endpoint/pairing configuration; verified
  ignored, untracked and mode 0600. It is compiled into local app builds, so do not
  publish those binaries as source artifacts.
- `data/` and `artifacts/local/`: local captures, generated packets and diagnostic
  artifacts. Include only separately reviewed, intentionally selected evidence.
- `.venv/`, `.reference/`, build/DerivedData directories and user-specific Xcode
  state: dependencies, reference checkout or local generated state.

`ios/Connection.xcconfig` has blank public defaults and an optional include of the
ignored local configuration. The committed project contains developer signing
settings; a reviewer must select their own signing team as documented. Those
settings do not establish that a reviewer can install the app without signing
setup.

The original `.gitignore` pattern `data/` also excluded the Core ML package's
`Data/` directory on this filesystem. It was narrowed to `/data/` during handoff;
the model payload is now eligible for inclusion. A fresh clone must contain the
complete `household.mlpackage`, not just its manifest.

| Asset | Bytes on disk | Use / license provenance |
| --- | ---: | --- |
| `ios/LibraryClaim/Models/household.mlpackage` | 12,688,803 | App resource: YOLOE-26s prompt-free INT8 Core ML; model README records upstream source and AGPL-3.0 or a separate Ultralytics enterprise license |
| `ios/LibraryClaim/Models/yolo26n.mlpackage` | 2,722,158 | Earlier offline baseline; not referenced as an app resource; retain provenance if included |

The largest intended model file is the household `weights/weight.bin` at
12,280,320 bytes. No other candidate file exceeded 5 MB at scan time. The household
payload hashes match the model README:

```text
weight.bin    d1f2a6dd2279730c9ccf42dcc6c4091068622a40cb2bd76e49ea105f861540c6
model.mlmodel 8b385f0c586d8cb71f739ae639697898add2386463a18e8972ff3ce8d7a66df4
```

Preserve the model source/export instructions and applicable third-party license
notices in a public distribution. This audit records existing provenance; it does
not establish that an enterprise license has been obtained or give a legal
determination about distribution.

## Remaining evidence gaps

- No completed, frozen physical ground truth was supplied to this validation run.
  A workbook/template or passing synthetic tests must not be reported as measured
  assessment accuracy.
- The reported real inventory repair from 87 to 76 records is a before/after
  software result; without matched physical truth it does not prove the final
  count is correct.
- Book/title accuracy, confidently wrong rate, spine and room measurement errors,
  non-book recall, price coverage/error, full voice interaction and final
  capture-to-packet timing still require the defined physical evaluation.
- An unsigned build proves iOS SDK compilation and resource packaging. This audit
  did not install or run it on a phone, verify microphone output, or make paid
  calls. Previous device/provider evidence must remain labelled with its own date
  and scope.
- No final unedited demo, selected real packet, completed results workbook or
  public repository availability was certified by this check; those deliverables
  require their own handoff verification.

## Final publication snapshot check

The isolated public source snapshot passed all 362 Python tests again in 15.863
seconds. All Markdown relative links resolved; the two short PDF notes are one
page each and the original full report was nine pages. The final source/evidence package
passed the targeted credential scan, and the included household-model weight
hash still matches its provenance record. Referenced evidence and original
recording checksums are documented separately.


## Detailed flow documentation update - 20 October 2026

The later documentation-only update adds 12 source-traced sequences and 28
endpoint/provider contracts. The flow atlas is 20 landscape pages; the full
technical report now contains 29 pages including that appendix. Every page was
rendered with Poppler and visually reviewed. Diagram generation validates
participant IDs and referenced source paths/line numbers. SVG XML, relative
document links, PDF page/text presence and changed-file whitespace were checked.
Application source and recorded evidence are unchanged; no new live-provider or
physical-accuracy result is claimed by these diagrams.

To regenerate with the optional authoring dependencies `reportlab` and `pypdf`:

```sh
python scripts/build_service_flows.py
python scripts/build_submission_docs.py
```
