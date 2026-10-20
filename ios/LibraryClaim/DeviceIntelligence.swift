import Foundation

enum LocalIntelligenceError: LocalizedError {
    case unavailable(String)
    var errorDescription: String? {
        switch self {
        case .unavailable(let reason): return reason
        }
    }
}

/// The phone supplies OCR and object proposals; OpenAI performs server semantic analysis.
actor DeviceIntelligence {
    static let shared = DeviceIntelligence()
    nonisolated static let availability = "OpenAI vision · Realtime voice"

    func analyze(ocr: [OCRSpan], recognition: LocalRecognition?) async -> JSONValue {
        .object([
            "source":.string("cloud_requested"), "status":.string("unavailable"),
            "books":.array([]), "objects":.array([]), "needs_cloud":.bool(true),
            "reasons":.array([.string("OpenAI server recognition; local OCR and object proposals only")])
        ])
    }
}
