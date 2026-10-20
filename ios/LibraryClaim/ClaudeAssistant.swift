import Foundation

enum ClaudeAssistantMode: String, Sendable { case setup, reply, translate }

enum ClaudeAssistantError: LocalizedError {
    case invalidResponse, actionUnavailable
    var errorDescription: String? {
        switch self {
        case .invalidResponse: return "Claude returned an incomplete or invalid response. Please try again."
        case .actionUnavailable: return "That action is unavailable or has already been requested for this turn."
        }
    }
}

/// One immutable identity per logical request; reusing a request also reuses its server cache key.
struct ClaudeAssistantRequest: Sendable {
    let mode: ClaudeAssistantMode
    let allowActions: Bool
    let body: JSONValue

    init(mode: ClaudeAssistantMode, text: String, phase: String = "", preferences: JSONValue = .object([:]),
         previous: String = "", language: String = "English", allowActions: Bool = true) {
        self.mode = mode; self.allowActions = allowActions
        // Python's wire bounds count Unicode scalars rather than Swift grapheme clusters.
        func bounded(_ value: String, _ count: Int) -> JSONValue { .string(String(value.unicodeScalars.prefix(count))) }
        self.body = .object([
            "request_id":.string(UUID().uuidString), "mode":.string(mode.rawValue),
            "text":bounded(text,4000), "phase":bounded(phase,32), "preferences":preferences,
            "previous":bounded(previous,1200), "language":bounded(language,80), "allow_actions":.bool(allowActions)
        ])
    }
}

/// Validate the complete response before exposing any proposed action to the native executor.
struct ClaudeAssistantTurn: Sendable {
    let reply: String
    let intent: String
    let language: String
    let country: String
    let currency: String
    let captureMode: String
    let speechLocale: String
    let toolName: String
    let toolArguments: String

    init(_ value: JSONValue, mode: ClaudeAssistantMode, allowActions: Bool) throws {
        guard case .object(let fields) = value else { throw ClaudeAssistantError.invalidResponse }
        func field(_ key: String, limit: Int = 80, fallback: String = "") throws -> String {
            guard let candidate = fields[key] else { return fallback }
            guard let string = candidate.string, string.unicodeScalars.count <= limit else { throw ClaudeAssistantError.invalidResponse }
            return string
        }
        guard fields["reply"] != nil else { throw ClaudeAssistantError.invalidResponse }
        reply = try field("reply",limit:1200)
        intent = try field("intent",fallback:"provide")
        language = try field("language"); country = try field("country"); currency = try field("currency")
        captureMode = try field("capture_mode"); speechLocale = try field("speech_locale")
        toolName = try field("tool_name"); toolArguments = try field("tool_arguments",limit:16000)
        guard !reply.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty,
              ["provide","confirm","ready"].contains(intent), ["","camera","lidar","comparison"].contains(captureMode),
              ["","India","United Kingdom","United States"].contains(country), ["","INR","GBP","USD"].contains(currency),
              ["","correct_inventory","compare_market"].contains(toolName) else { throw ClaudeAssistantError.invalidResponse }
        if toolName.isEmpty {
            guard toolArguments.isEmpty else { throw ClaudeAssistantError.invalidResponse }
        } else {
            guard mode == .reply, allowActions, Self.arguments(toolArguments) != nil else { throw ClaudeAssistantError.invalidResponse }
        }
    }

    static func arguments(_ text: String) -> JSONValue? {
        guard text.unicodeScalars.count <= 16000, let data = text.data(using:.utf8),
              let value = try? JSONDecoder().decode(JSONValue.self,from:data), case .object = value else { return nil }
        return value
    }
}

extension BackendClient {
    func assistant(_ path: String, request: ClaudeAssistantRequest) async throws -> ClaudeAssistantTurn {
        try Task.checkCancellation()
        let value = try await call(path + "/assistant",body:request.body)
        try Task.checkCancellation()
        return try ClaudeAssistantTurn(value,mode:request.mode,allowActions:request.allowActions)
    }
}

enum LocalCaptureHandoff {
    @MainActor static func request(
        prepareAnnouncement: @MainActor () async throws -> String,
        checkCurrent: @MainActor () throws -> Void,
        requestCapture: @MainActor () async throws -> Void
    ) async throws -> String {
        try checkCurrent()
        // Onboarding can close immediately after request_capture succeeds. Finish
        // translation before that event; never acquire its assistant lock afterward.
        let announcement = try await prepareAnnouncement()
        try Task.checkCancellation()
        try checkCurrent()
        try await requestCapture()
        try checkCurrent()
        return announcement
    }
}

/// Shares the voice agent's actor so Stop invalidates a pending plan synchronously.
@MainActor final class LocalVoiceToolExecutor {
    private let client: BackendClient
    private let path: String
    private let allowed: Set<String>
    private var active = true
    private var used = false
    init(client: BackendClient, path: String, allowed: Set<String>) { self.client = client; self.path = path; self.allowed = allowed }
    func invalidate() { active = false }
    func call(name: String, arguments: String) async throws -> JSONValue {
        try Task.checkCancellation()
        guard active, !used, allowed.contains(name), ["correct_inventory","compare_market"].contains(name),
              let value = ClaudeAssistantTurn.arguments(arguments) else { throw ClaudeAssistantError.actionUnavailable }
        used = true // Never retry an ambiguous network result automatically.
        return try await client.call(path,body:.object(["name":.string(name),"arguments":value]))
    }
}
