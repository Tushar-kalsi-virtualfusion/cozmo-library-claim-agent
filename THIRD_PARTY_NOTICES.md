# Third-party components and attribution

## Bundled on-device models

The application resource `ios/LibraryClaim/Models/household.mlpackage` is an
INT8 Core ML export of Ultralytics YOLOE-26s prompt-free. The separate
`yolo26n.mlpackage` is an earlier offline baseline and is not bundled by the app
target. Ultralytics is the upstream author/provider of those models.

The upstream model licensing is AGPL-3.0 or a separately obtained Ultralytics
enterprise license. No enterprise license is asserted by this submission. The
[AGPL-3.0 text](licenses/Ultralytics-AGPL-3.0.txt) is retained with the distribution.
Model source locations, original and exported hashes, conversion changes and
verification instructions are recorded in the [model README](ios/LibraryClaim/Models/README.md).
The [export script](scripts/export_household_model.py) records the conversion
source and pinned tool versions. Upstream source is available from
[Ultralytics](https://github.com/ultralytics/ultralytics); its
[license page](https://www.ultralytics.com/license) describes the licensing choices.
These model terms are not replaced by any project-level label.

## Platform, service and Python dependencies

Apple ARKit, RoomPlan, Vision, Core ML and audio frameworks are used through the
installed Apple SDK. OpenAI APIs provide the active hosted vision, voice and
search services. Legacy provider adapters remain in source but are inactive in
the submitted OpenAI configuration. No hosted-model weights or provider secrets
are distributed.

Python dependency versions are pinned in `requirements.lock`; each dependency
retains its own upstream license. This repository does not vendor their installed
packages. Source-page/product information is retained only as evidence for the
saved claim; source URLs, retrieval metadata and asset hashes remain traceable.

## Reference and assistance

The [Insurance Claim Live Agent Team reference](https://github.com/Shubhamsaboo/awesome-llm-apps/tree/main/voice_ai_agents/insurance_claim_live_agent_team)
informed the voice/evidence interaction pattern. Its execution evidence and reuse
decisions are in [reference-run.md](docs/setup/reference-run.md). The reference
checkout is not redistributed. Codex assisted with application code, tests,
documentation and submission packaging. The project did not train a new model.
