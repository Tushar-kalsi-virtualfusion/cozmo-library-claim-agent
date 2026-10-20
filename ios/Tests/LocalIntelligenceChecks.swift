import Foundation

@main struct LocalIntelligenceChecks {
    static func main() async throws {
        precondition(LocalEvidence.supported("Invented Author", by:"DUNE FRANK HERBERT") == nil)
        precondition(LocalEvidence.supported("Dune", by:"DUNE FRANK HERBERT") == "Dune")
        precondition(LocalEvidence.supported("", by:"DUNE") == nil)
        precondition(LocalEvidence.isbn("9780441172719", by:"ISBN 978-0-441-17271-9") == "9780441172719")
        precondition(LocalEvidence.isbn("9780441172719", by:"DUNE FRANK HERBERT") == nil)
        var setup = LocalSetupGate()
        let proposal: JSONValue = .object(["language":.string("English"),"country":.string("India"),"currency":.string("INR"),"capture_mode":.string("camera")])
        precondition(setup.next(intent:"ready",proposal:proposal) == .readback(proposal))
        precondition(setup.next(intent:"confirm",proposal:.null) == .readback(proposal)) // An undelivered readback cannot be confirmed.
        setup.readbackDelivered()
        precondition(setup.next(intent:"ready",proposal:.null) == .askConfirmation)
        precondition(setup.next(intent:"confirm",proposal:.null) == .configure(proposal))
        precondition(setup.next(intent:"ready",proposal:.null) == .askConfirmation) // Backend has not accepted setup yet.
        setup.configured()
        precondition(setup.next(intent:"ready",proposal:.null) == .askReadiness) // Readiness question has not been spoken yet.
        setup.readinessDelivered()
        precondition(setup.next(intent:"confirm",proposal:.null) == .askReadiness)
        precondition(setup.next(intent:"ready",proposal:.null) == .requestCapture)
        precondition(setup.next(intent:"ready",proposal:.null) == .none) // No duplicate handoff.
        setup.retryReadiness() // Translation or handoff failed before capture was accepted.
        precondition(setup.next(intent:"confirm",proposal:.null) == .askReadiness)
        precondition(setup.next(intent:"ready",proposal:.null) == .requestCapture)
        var changed = LocalSetupGate()
        _ = changed.next(intent:"provide",proposal:proposal)
        let correction: JSONValue = .object(["capture_mode":.string("lidar")])
        precondition(changed.next(intent:"confirm",proposal:correction) != .configure(proposal))
        precondition(localBytePrefix("हिन्दी",limit:5).utf8.count <= 5)
        precondition(localBytePrefix("exact",limit:5) == "exact")
        let local = await DeviceIntelligence.shared.analyze(ocr:[],recognition:nil)
        precondition(local["source"].string == "cloud_requested", "Semantic evidence must request Claude without a local language-model prerequisite")
        precondition(local["status"].string == "unavailable" && local["needs_cloud"] == .bool(true))
        precondition(local["books"].array.isEmpty && local["objects"].array.isEmpty)
        var metadata = FrameMetadata(id:"local-frame",timestamp_s:1,image_width:1920,image_height:1440,
            intrinsics:[],camera_transform:[],tracking:"normal",depth_width:0,depth_height:0,ar_session_id:"test",ocr:[])
        metadata.local_analysis = local
        let roundtrip = try JSONDecoder().decode(FrameMetadata.self,from:JSONEncoder().encode(metadata))
        precondition(roundtrip.local_analysis == local)
        print("Local intelligence evidence and separate confirmation/readiness checks passed")
    }
}
