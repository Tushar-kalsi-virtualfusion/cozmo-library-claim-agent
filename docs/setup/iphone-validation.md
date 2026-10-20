# Physical iPhone validation

Current build uses OpenAI Realtime voice, GPT-6 Astra image recognition, a local
YOLOE Core ML detector, Vision OCR and LiDAR/RoomPlan. The display uses dotted
borders with available labels and supported dimensions; filled masks are disabled.
Use a stable Mac `.local` hostname with the server bound to `0.0.0.0`; see the
[README](../../README.md) for pairing and connection configuration.

Historical device setup was verified on 17 October 2026: an iPhone 17 Pro was
paired, Developer Mode and signing were enabled, installation/launch succeeded,
and wireless debugging reported a connected local-network tunnel. Six earlier
phone photos exercised the 4,585-class detector; four AC views returned an air
conditioner proposal, with 705-895 ms three-view inference. These small diagnostics
do not establish general recognition accuracy. Earlier Claude/mask experiments
are not the active pipeline.

The 20 October submission includes a real capture and a separate saved-inventory
recording, but independent physical ground truth and the full acceptance checks
below remain incomplete. See [results](../submission/results.md) and
[fresh software verification](../submission/validation.md). Compilation, signed
installation and server audio probes do not establish measured room accuracy.

## Physical acceptance procedure

1. Install a full Xcode version compatible with both this Mac and the iPhone's installed iOS. Open `ios/LibraryClaim.xcodeproj`, select the app target, choose your signing team and the connected iPhone. Enable Developer Mode, trust the Mac, and run. No simulator can prove LiDAR capture.
2. Set `CLAIM_HOST=0.0.0.0` and `CLAIM_PHONE_HOST=<Mac local hostname>.local` in `.env`, then start `.venv/bin/python -m claim_agent`. Run `.venv/bin/python scripts/configure_ios.py` and rebuild. Verify OpenAI Realtime and image-model access. Tap Start; allow Local Network, Camera and Microphone. Answer language and pricing market aloud, hear the complete readback including automatic LiDAR selection, confirm, then separately say you are ready. Verify natural English/Hindi responses, full readback, listening after playback, speaker/headphone routing and phone volume. The app streams speech to OpenAI through the Mac; Apple Speech language packs are not required.
3. In LiDAR/comparison mode, confirm one `ar_session_id` across every uploaded frame and final room JSON. The app creates an ARSession, enables `sceneDepth`, then passes it to RoomCaptureView. It polls `currentFrame`; it never replaces RoomPlan's ARSession delegate or runs a second measuring pass.
4. Inspect a saved JPEG and corresponding metadata/depth. JPEG width/height must match calibration, intrinsics are column-major, pose is column-major, JPEG is unrotated, depth is row-major little-endian float32 with row padding removed, confidence is uint8. Check a known edge overlay in portrait and landscape; all normalized polygons refer to raw JPEG coordinates.
5. Use the predetermined 20-spine sample to test plane measurements, including thin, thick, stacked, oblique and reflective spines. Verify two identical neighboring books remain distinct and revisiting a shelf updates existing IDs. Record failures, not just successful examples.
6. Speak while scanning. Hear streamed OpenAI speech and verify the microphone remains available. Test spoken interruption, echo cancellation, and quiet-room versus background-noise behavior. Tap Interrupt & speak during an API request and mid-sentence; verify no late action executes and playback stops while camera/event updates continue. Reconnect voice and verify setup/geometry state remains accurate. Test background noise, silence, selected-language recognition and recovery on the real phone.
7. Disable Wi-Fi briefly during a sweep. Frames must remain in Files → On My iPhone → Library Claim → Captures → sweep ID. Re-enable Wi-Fi; pending frames should upload and move into `uploaded`. No file is deleted on acknowledgment. A conflicting frame ID must be rejected.
8. End capture while an encode/upload is pending. The app must stop sampling, wait for that encode and all queued uploads, submit final RoomPlan geometry, then call finish. If uploads do not finish within 60 seconds, retry finish after reconnecting; do not start another measuring pass.
9. Deny each permission independently. Ensure the error is clear and nothing claims a completed capture. Send the app to the background during capture; it should end the sweep and record an interruption rather than silently continue a broken coordinate frame.
10. Review floor polygons, every wall, openings and bookshelf projection associations against saved same-sweep evidence. An irregular floor must retain its polygon. Curved/missing surfaces remain a measurement limitation.

11. In camera-only mode, verify the preview works and every saved frame has zero depth dimensions and empty depth/confidence payloads. No RoomPlan geometry should be uploaded. End the sweep and confirm unknown physical dimensions remain unknown and duplicate-track warnings remain visible.
12. In comparison mode, verify both analyses use identical JPEG hashes and the recorded model/market. Confirm the camera replay has no inherited LiDAR geometry, prices or inventory. Read both responses; evaluate against the same independently measured room with separate physical-ID mappings. Do not interpret raw count deltas or replay timing as improved accuracy or sensor speed.
13. Check spoken onboarding asks language first, never infers country, requires confirmation then readiness, and creates no sweep/timer before readiness. Simulator capability must reject real capture. Interrupt a confirmation, reconnect voice, stop voice and start a new conversation after finishing.

After these pass, record the full real-room run, fill the frozen ground-truth workbook, run the evaluator and quantify all pass bars. Test at least one measured fix before the final scored run.

The local demo uses HTTP on trusted Wi-Fi; pairing prevents unpaired access but does not encrypt traffic. The native app's development-only ATS exception permits that connection. Provider keys stay on the Mac. Do not expose this server publicly.
# Hybrid recognition acceptance — next physical run

Use the current OpenAI Realtime build for these physical acceptance checks.

1. Open the app and point at a sofa/chair/book. Provisional labels and dotted borders should appear before starting a conversation; no evidence should upload yet. Check the local-model status. Slowly move the phone while keeping an object in view: its border should follow it between detector updates without blinking. Cover the lens or move the object out of view: lost borders must clear promptly. Check that an empty detector result does not erase a still-tracked object. Border labels show names and available measurements only, without “Server confirms details” or missing-measurement text. These are not world-plane measurements.
2. Start spoken setup, confirm the automatic capture mode in the readback, then separately confirm readiness. Blur or partially crop an object: the assistant should request a clear/full view aloud. Hold steady and include the complete object.
3. Verify clear photos reach server evidence validation. The phone requests server interpretation without invoking Apple Intelligence. Clear novel scenes may escalate to GPT-6 Astra within the budget; other physical-object proposals stay provisional until confirmed. Blurred or unreadable book evidence should request a clearer view. Hold the same scene still: upload/vision counts should not keep climbing. Move to a new object: a new photo should be admitted. Verify the 20-call limit, six-second cooldown and credit-error stop from recorded routing metrics, without repeatedly probing an unfunded provider.
4. In LiDAR mode, verify the depth status changes to available and saved metadata has nonzero aligned depth sizes. Confirm a later improved-confidence depth view is retained even when RGB is similar. Compare displayed measured edges to a ruler; dimensions without supporting geometry must remain unknown.
5. End capture and inspect the packet: correct object categories, no unsupported titles/metric dimensions, separate claim valuation and API estimate, duplicate counter and independent camera comparison. Freeze ground truth before reporting accuracy.

## Assessment acceptance still required
The original email and linked brief were reread on October 20. Capture one real enclosed room with at least 60 books across two shelving units and eight non-book objects. Freeze true counts/titles, 20 measured spines, 15 checked prices and tape-measured room dimensions before evaluating. Record one unedited demo up to six minutes including the continuous sweep and generated packet. Test same 10 books in a second market. Report all pass bars, actual stage latency/cost and the three worst observed errors. A software build and successful API probes do not substitute for these deliverables. The source email deadline was October 19, 8 PM IST; the brief says late submissions are reviewed with the delay noted.
