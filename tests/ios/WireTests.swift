import Foundation

@main struct WireTests {
    static func main() async throws {
        let meta = FrameMetadata(id: "f0001", timestamp_s: 1.25, image_width: 1920, image_height: 1440,
            intrinsics: [1000,0,0,0,1000,0,960,720,1],
            camera_transform: [1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1], tracking: "normal",
            depth_width: 256, depth_height: 192, ar_session_id: "same-session", ocr: [])
        let body = FrameUpload(metadata: meta, jpeg_base64: Data([1,2,3]).base64EncodedString(),
                               depth_base64: "", confidence_base64: "")
        let data = try JSONEncoder().encode(body)
        let json = try JSONDecoder().decode(JSONValue.self, from: data)
        precondition(json["metadata"]["image_width"].number == 1920)
        precondition(json["metadata"]["orientation"].string == "arkit_landscape_right")
        precondition(json["metadata"]["camera_transform"].array.count == 16)
        precondition(json["jpeg_base64"].string == "AQID")
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        let outbox = try EvidenceOutbox(root: root)
        try await outbox.save(data, id: "f0001")
        try await outbox.save(data, id: "f0001")
        let count = try await outbox.pendingCount()
        precondition(count == 1)
        let pending = try await outbox.nextPending()!
        let persisted = try Data(contentsOf: pending)
        precondition(persisted == data)
        do { try await outbox.save(Data([9]), id: "f0001"); fatalError("Evidence replacement accepted") }
        catch OutboxError.conflictingEvidence { }
        try await outbox.acknowledge(pending)
        let after = try await outbox.pendingCount()
        precondition(after == 0)
        let recovered = try EvidenceOutbox(root: root)
        try await recovered.save(data, id: "f0001")
        let retried = try await recovered.pendingCount()
        precondition(retried == 0)
        let money = try JSONDecoder().decode(JSONValue.self, from: Data("{\"amount\":\"499.50\",\"unknown\":null}".utf8))
        precondition(money["amount"].string == "499.50")
        precondition(money["unknown"] == .null)
        print("Swift wire/outbox checks passed: calibration keys, base64, immutable retry, disk recovery, decimal/null decoding")
    }
}
