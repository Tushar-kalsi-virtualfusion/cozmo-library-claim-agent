import Foundation

enum OutboxError: Error { case conflictingEvidence, invalidIdentifier, storageBudget }

actor EvidenceOutbox {
    let root: URL
    private let pending: URL
    private let saved: URL
    private var bytesWritten: Int
    init(root: URL) throws {
        self.root = root
        pending = root.appendingPathComponent("pending", isDirectory: true)
        saved = root.appendingPathComponent("uploaded", isDirectory: true)
        try FileManager.default.createDirectory(at: pending, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: saved, withIntermediateDirectories: true)
        bytesWritten = 0
        for folder in [pending, saved] {
            for url in try FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: [.fileSizeKey]) {
                bytesWritten += (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
            }
        }
    }
    func save(_ data: Data, id: String) throws {
        guard id.range(of: "^[A-Za-z0-9_-]{1,80}$", options: .regularExpression) != nil else { throw OutboxError.invalidIdentifier }
        for dir in [pending, saved] {
            let file = dir.appendingPathComponent(id + ".json")
            if FileManager.default.fileExists(atPath: file.path) {
                guard try Data(contentsOf: file) == data else { throw OutboxError.conflictingEvidence }
                return
            }
        }
        guard bytesWritten + data.count <= 1_000_000_000 else { throw OutboxError.storageBudget }
        try data.write(to: pending.appendingPathComponent(id + ".json"), options: .atomic)
        bytesWritten += data.count
    }
    func nextPending() throws -> URL? {
        try FileManager.default.contentsOfDirectory(at: pending, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "json" }.sorted { $0.lastPathComponent < $1.lastPathComponent }.first
    }
    func pendingCount() throws -> Int {
        try FileManager.default.contentsOfDirectory(at: pending, includingPropertiesForKeys: nil).filter { $0.pathExtension == "json" }.count
    }
    func acknowledge(_ url: URL) throws {
        guard url.deletingLastPathComponent().standardizedFileURL == pending.standardizedFileURL else { throw OutboxError.invalidIdentifier }
        let destination = saved.appendingPathComponent(url.lastPathComponent)
        if FileManager.default.fileExists(atPath: destination.path) {
            guard try Data(contentsOf: destination) == Data(contentsOf: url) else { throw OutboxError.conflictingEvidence }
            try FileManager.default.removeItem(at: url)
        } else { try FileManager.default.moveItem(at: url, to: destination) }
    }
}
