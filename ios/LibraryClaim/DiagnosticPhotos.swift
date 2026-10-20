import Foundation

/// Explicit developer-invoked photo check. Never creates or uploads a sweep.
struct DiagnosticPhotoSchedule {
    private(set) var count = 0
    private var lastTime = -Double.infinity
    var enabled: Bool
    init(enabled: Bool) { self.enabled = enabled }
    mutating func reserve(time: Double, foreground: Bool, hasConversation: Bool) -> Int? {
        guard enabled, foreground, !hasConversation, count < 6, time-lastTime >= 3 else { return nil }
        count += 1; lastTime = time
        return count
    }
}

actor DiagnosticPhotoStore {
    private let runID = UUID().uuidString
    func save(_ payload: FrameUpload) throws {
        let root = try FileManager.default.url(for:.documentDirectory,in:.userDomainMask,appropriateFor:nil,create:true)
            .appendingPathComponent("ModelDiagnostics").appendingPathComponent(runID)
        try FileManager.default.createDirectory(at:root,withIntermediateDirectories:true)
        try JSONEncoder().encode(payload).write(to:root.appendingPathComponent(payload.metadata.id+".json"),options:.atomic)
    }
}
