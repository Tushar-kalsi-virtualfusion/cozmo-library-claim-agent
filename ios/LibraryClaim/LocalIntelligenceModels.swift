import Foundation

enum LocalEvidence {
    static func supported(_ candidate: String?, by text: String) -> String? {
        guard let candidate = candidate?.trimmingCharacters(in:.whitespacesAndNewlines), !candidate.isEmpty,
              candidate.count <= 200, text.range(of:candidate,options:[.caseInsensitive,.diacriticInsensitive]) != nil else { return nil }
        return candidate
    }
    static func isbn(_ candidate: String?, by text: String) -> String? {
        guard let candidate else { return nil }
        let digits = candidate.uppercased().filter { $0.isNumber || $0 == "X" }
        guard [10,13].contains(digits.count), text.uppercased().filter({$0.isNumber || $0 == "X"}).contains(digits) else { return nil }
        return digits
    }
    static func text(in polygon: [[Double]], spans: [OCRSpan]) -> String {
        guard polygon.count >= 3, polygon.allSatisfy({$0.count == 2}) else { return "" }
        let xs = polygon.map{$0[0]}, ys = polygon.map{$0[1]}
        return String(spans.filter { span in
            span.center.count == 2 && Double(span.center[0]) >= xs.min()! && Double(span.center[0]) <= xs.max()!
                && Double(span.center[1]) >= ys.min()! && Double(span.center[1]) <= ys.max()!
        }.map(\.text).joined(separator:" ").prefix(800))
    }
    static func polygon(_ points: [[Double]]) -> JSONValue { .array(points.map { .array($0.map { .number($0) }) }) }
}

enum LocalSetupAction: Equatable {
    case none, askDetails, askConfirmation, askReadiness, readback(JSONValue), configure(JSONValue), requestCapture
}

enum CaptureModePolicy {
    static func preferred(lidarSupported: Bool) -> CaptureMode { lidarSupported ? .lidar : .camera }
    static func accepts(_ selection: String, lidarSupported: Bool) -> Bool {
        selection == preferred(lidarSupported:lidarSupported).rawValue || (selection == "comparison" && lidarSupported)
    }
}

/// Readback, confirmation and readiness always require distinct user turns.
/// The model cannot bypass this gate by emitting multiple instructions at once.
struct LocalSetupGate {
    private(set) var preferences: JSONValue = .object([:])
    private(set) var phase = "collecting"
    private var hardwareMode: CaptureMode?
    /// Capability is a default, never evidence of spoken confirmation or readiness.
    mutating func seedCaptureMode(_ mode: String) {
        guard let mode = CaptureMode(rawValue:mode), hardwareMode != mode else { return }
        hardwareMode = mode
        var fields: [String:JSONValue] = [:]
        for key in ["language","country","currency"] { fields[key] = preferences[key] }
        fields["capture_mode"] = .string(mode.rawValue)
        preferences = .object(fields); phase = "collecting"
    }
    private func acceptsMode(_ selection: String) -> Bool {
        guard let hardwareMode else { return ["camera","lidar","comparison"].contains(selection) }
        return CaptureModePolicy.accepts(selection,lidarSupported:hardwareMode == .lidar)
    }
    mutating func configured() { phase = "readiness_prompt" }
    mutating func readbackDelivered() { if phase == "readback" { phase = "confirmation" } }
    mutating func readinessDelivered() { if phase == "readiness_prompt" { phase = "readiness" } }
    mutating func restore(_ value: JSONValue) {
        if value["confirmed"] == .bool(true), acceptsMode(value["capture_mode"].string ?? "") {
            preferences = value; phase = "readiness_prompt"
        }
    }
    mutating func retryReadiness() { phase = "readiness" }
    /// Interrupting preparation is different from cancelling an already-sent
    /// mutation. Unknown server outcomes freeze actions until a context refresh.
    mutating func interruptPendingAction(requestWasSent: Bool) -> Bool {
        if requestWasSent || phase == "reconciling" { phase = "reconciling"; return true }
        if phase == "requested" { phase = "readiness_prompt" }
        if phase == "configuring" { phase = "confirmation" }
        return false
    }
    @discardableResult mutating func reconcileAfterInterruption(_ context: JSONValue) -> Bool {
        guard case .bool = context["capture_requested"] else { return false }
        if context["capture_requested"] == .bool(true) { phase = "requested"; return true }
        let saved = context["preferences"]
        if saved["confirmed"] == .bool(true), acceptsMode(saved["capture_mode"].string ?? "") {
            preferences = saved; phase = "readiness_prompt"
        } else {
            let complete = ["language","country","currency","capture_mode"].allSatisfy {
                !(preferences[$0].string ?? "").isEmpty
            }
            phase = complete ? "readback" : "collecting"
        }
        return true
    }
    mutating func next(intent: String, proposal: JSONValue) -> LocalSetupAction {
        guard phase != "requested" else { return .none }
        guard phase != "reconciling" else { return .askDetails }
        var fields: [String:JSONValue] = [:]
        for key in ["language","country","currency","capture_mode"] { fields[key] = preferences[key] }
        var changed = false
        let proposedMode = proposal["capture_mode"].string?.trimmingCharacters(in:.whitespacesAndNewlines) ?? ""
        let modeConflict = !proposedMode.isEmpty && !acceptsMode(proposedMode)
        for key in ["language","country","currency","capture_mode"] {
            if let value = proposal[key].string?.trimmingCharacters(in:.whitespacesAndNewlines), !value.isEmpty, value.count <= 80,
               (key != "capture_mode" || acceptsMode(value)), fields[key] != .string(value) {
                fields[key] = .string(value); changed = true
            }
        }
        preferences = .object(fields)
        let complete = fields.values.allSatisfy { !($0.string ?? "").isEmpty }
        if changed || modeConflict { phase = complete ? "readback" : "collecting"; return complete ? .readback(preferences) : .askDetails }
        if phase == "readback" { return .readback(preferences) }
        if phase == "readiness_prompt" { return .askReadiness }
        if phase == "readiness" {
            if intent == "ready" { phase = "requested"; return .requestCapture }
            return .askReadiness
        }
        if phase == "confirmation", intent == "confirm" { phase = "configuring"; return .configure(preferences) }
        if phase == "confirmation" || phase == "configuring" { return .askConfirmation }
        return .askDetails
    }
    mutating func configurationFailed() { phase = "confirmation" }
}

/// A byte cap also bounds scripts whose characters require several tokens.
func localBytePrefix(_ text: String, limit: Int) -> String {
    var bytes = 0
    return String(text.prefix { character in
        bytes += String(character).utf8.count
        return bytes <= limit
    })
}

extension JSONValue {
    var compact: String { (try? JSONEncoder().encode(self)).map { String(decoding:$0,as:UTF8.self) } ?? "null" }
}
