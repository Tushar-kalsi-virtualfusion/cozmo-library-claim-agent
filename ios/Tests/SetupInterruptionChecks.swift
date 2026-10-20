import Foundation

@main struct SetupInterruptionChecks {
    @MainActor static func main() async throws {
        let preferences: JSONValue = .object(["language":.string("English"),"country":.string("India"),
            "currency":.string("INR"),"capture_mode":.string("lidar"),"confirmed":.bool(true)])
        var gate = LocalSetupGate()
        gate.seedCaptureMode("lidar"); gate.restore(preferences); gate.readinessDelivered()
        precondition(gate.next(intent:"ready",proposal:.null) == .requestCapture)
        var current = true, requests = 0
        do {
            try await CloudSpeechHandoff.request(
                prepareAnnouncement: { "Starting capture" },
                play: { _ in
                    precondition(!gate.interruptPendingAction(requestWasSent:false))
                    current = false
                },
                checkCurrent: { if !current { throw CancellationError() } },
                requestCapture: { requests += 1 })
            fatalError("Interrupted announcement must cancel capture")
        } catch is CancellationError {}
        precondition(requests == 0)
        precondition(gate.next(intent:"ready",proposal:.null) == .askReadiness, "The final announcement did not send capture or deliver a new readiness prompt")
        gate.readinessDelivered()
        precondition(gate.next(intent:"ready",proposal:.null) == .requestCapture)

        // The local-speech handoff has the same interruption gap during translation.
        current = true
        do {
            _ = try await LocalCaptureHandoff.request(
                prepareAnnouncement: {
                    precondition(!gate.interruptPendingAction(requestWasSent:false)); current = false
                    return "Starting capture"
                },
                checkCurrent: { if !current { throw CancellationError() } },
                requestCapture: { requests += 1 })
            fatalError("Interrupted translation must cancel capture")
        } catch is CancellationError {}
        precondition(requests == 0 && gate.next(intent:"ready",proposal:.null) == .askReadiness)

        var configuring = LocalSetupGate(); configuring.seedCaptureMode("lidar")
        _ = configuring.next(intent:"provide",proposal:preferences)
        configuring.readbackDelivered()
        guard case .configure = configuring.next(intent:"confirm",proposal:.null) else { fatalError("Missing configuration request") }
        precondition(configuring.interruptPendingAction(requestWasSent:true))
        precondition(configuring.phase == "reconciling")
        precondition(!configuring.reconcileAfterInterruption(.null))
        precondition(configuring.phase == "reconciling", "Incomplete server context cannot resolve an ambiguous mutation")
        precondition(configuring.next(intent:"confirm",proposal:.null) == .askDetails, "No action while the result is unknown")
        configuring.reconcileAfterInterruption(.object(["preferences":preferences,"capture_requested":.bool(false)]))
        precondition(configuring.next(intent:"ready",proposal:.null) == .askReadiness)
        configuring.readinessDelivered()
        precondition(configuring.next(intent:"ready",proposal:.null) == .requestCapture)

        precondition(configuring.interruptPendingAction(requestWasSent:true))
        configuring.reconcileAfterInterruption(.object(["preferences":preferences,"capture_requested":.bool(true)]))
        precondition(configuring.next(intent:"ready",proposal:.null) == .none, "A verified accepted capture must not repeat")

        var unsaved = LocalSetupGate(); unsaved.seedCaptureMode("lidar")
        _ = unsaved.next(intent:"provide",proposal:preferences); unsaved.readbackDelivered()
        _ = unsaved.next(intent:"confirm",proposal:.null)
        precondition(unsaved.interruptPendingAction(requestWasSent:true))
        unsaved.reconcileAfterInterruption(.object(["preferences":.null,"capture_requested":.bool(false)]))
        guard case .readback = unsaved.next(intent:"confirm",proposal:.null) else { fatalError("Unconfirmed server state must get a fresh full readback") }
        unsaved.readbackDelivered()
        guard case .configure = unsaved.next(intent:"confirm",proposal:.null) else { fatalError("Explicit confirmation after reconciliation must work") }
        print("Setup interrupted playback, translation, configuration reconciliation and capture deduplication checks passed")
    }
}
