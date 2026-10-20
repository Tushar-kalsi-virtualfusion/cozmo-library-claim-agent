# Reference app: execution evidence and reuse decisions

Historical check: 17 October 2026. The upstream reference is the
[Insurance Claim Live Agent Team](https://github.com/Shubhamsaboo/awesome-llm-apps/tree/main/voice_ai_agents/insurance_claim_live_agent_team)
at commit `40b16b66e5257c528059bb93ca9410b55d39278a`.
The reference checkout and its local configuration are excluded from this repository.

## What was exercised

- Python 3.12.12; installed reference versions are in [reference-requirements.lock.txt](reference-requirements.lock.txt).
- The local HTTP page and health endpoint on port 4177 returned 200.
- The supplied 55 Python tests, 18 JavaScript client tests and seven avatar-player tests passed.
- A small opt-in private-network patch and five regression tests brought its Python total to 60. The [patch](reference-lan.patch) and [tests](reference-lan-tests.py) preserve that experiment; their private addresses are example fixtures.
- The reference had no configured Gemini key. A model-backed conversation, microphone/camera flow and final live claim were **not** demonstrated. Mocked tests and HTTP responses are narrower evidence.

## Reproduction and limits

Check out the recorded upstream commit and follow its own README using a separate
Python environment. The saved dependency lock records the environment used here.
Run the supplied tests and serve its `live_demo.server:app` locally on port 4177.
Model access and a funded Gemini account are separate prerequisites for its live
conversation. The library application in this repository instead runs on port
4180 and uses the OpenAI configuration in `.env.example`.

The LAN experiment was an HTTP interface preview. Browser camera/microphone APIs
require an appropriate secure context; the preview did not establish phone capture.
The native iPhone application is the implemented sensor path for this assessment.

## What carried over

Retained the interaction ideas: bidirectional PCM voice, interruption handling,
background work, correction handling and evidence references. Replaced the domain
workflow with physical identity tracking, calibrated geometry, sourced prices and
a deterministic claim packet. Omitted policy lookup, avatars, generated sketches
and a narrative-only claim. High-resolution selected images serve book recognition
instead of treating small conversational frames as metric evidence.

Application code was developed for this task with Codex assistance. The upstream
reference is acknowledged as an interaction/design reference; no claim is made
that its unavailable model-backed workflow was successfully run.
