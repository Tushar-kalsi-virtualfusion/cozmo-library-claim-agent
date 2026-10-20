import Foundation

enum CaptureMode: String, Codable, Sendable {
    case camera, lidar
}

// Wire names intentionally match the Python contract without acronym conversion.
struct OCRSpan: Codable, Sendable {
    var text: String
    var center: [Float]
    var confidence: Float
}

struct FrameMetadata: Codable, Sendable {
    var id: String
    var timestamp_s: Double
    var image_width: Int
    var image_height: Int
    var orientation = "arkit_landscape_right"
    var intrinsics: [Float]
    var camera_transform: [Float]
    var tracking: String
    var depth_width: Int
    var depth_height: Int
    var ar_session_id: String
    var ocr: [OCRSpan]
    var local_recognition: LocalRecognition? = nil
    var vision_rotation: Int? = nil
    var local_analysis: JSONValue? = nil
}

struct FrameUpload: Codable, Sendable {
    var metadata: FrameMetadata
    var jpeg_base64: String
    var depth_base64: String
    var confidence_base64: String

    // Camera uploads must withhold depth even if a producer supplies it accidentally.
    func applyingCaptureMode(_ mode: CaptureMode) -> FrameUpload {
        guard mode == .camera else { return self }
        var frame = self
        frame.metadata.depth_width = 0; frame.metadata.depth_height = 0
        frame.depth_base64 = ""; frame.confidence_base64 = ""
        return frame
    }
}

indirect enum JSONValue: Codable, Equatable, Sendable {
    case object([String: JSONValue]), array([JSONValue]), string(String), number(Double), bool(Bool), null
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let v = try? c.decode(Bool.self) { self = .bool(v) }
        else if let v = try? c.decode(Double.self) { self = .number(v) }
        else if let v = try? c.decode(String.self) { self = .string(v) }
        else if let v = try? c.decode([JSONValue].self) { self = .array(v) }
        else { self = .object(try c.decode([String: JSONValue].self)) }
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .object(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .string(let v): try c.encode(v)
        case .number(let v): try c.encode(v)
        case .bool(let v): try c.encode(v)
        case .null: try c.encodeNil()
        }
    }
    subscript(_ key: String) -> JSONValue {
        if case .object(let values) = self { return values[key] ?? .null }; return .null
    }
    var string: String? { if case .string(let value) = self { return value }; return nil }
    var number: Double? { if case .number(let value) = self { return value }; return nil }
    var array: [JSONValue] { if case .array(let values) = self { return values }; return [] }
    var pretty: String {
        let encoder = JSONEncoder(); encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        return (try? encoder.encode(self)).flatMap { String(data: $0, encoding: .utf8) } ?? ""
    }
}

struct InventoryRow: Identifiable {
    let value: JSONValue
    var id: String { value["id"].string ?? "" }
    var category: String { displayCategory(value["category"].string ?? "Book") }
    var title: String { value["title"].string.flatMap { $0.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty ? nil : $0 } ?? category }
    var dimensionsText: String {
        if let length = value["spine_height_cm"].number, let thickness = value["spine_thickness_cm"].number {
            return "Spine \(dimension(length)) × \(dimension(thickness)) cm"
        }
        let fields = [("w", "W"), ("h", "H"), ("d", "D")].compactMap { key, label in
            value["dimensions_cm"][key].number.flatMap { $0 > 0 ? "\(label) \(dimension($0))" : nil }
        }
        return fields.isEmpty ? "Dimensions not measured" : fields.joined(separator:" · ") + " cm · RoomPlan"
    }
    var subtitle: String { category + " · " + dimensionsText }
    var pricingStatus: InventoryPricingStatus { InventoryPricingStatus(rawValue:value["pricing_status"].string ?? "") ?? .idle }
    var isExcluded: Bool { value["excluded"] == .bool(true) }
    var needsAppraisal: Bool { value["status"].string == "needs_appraisal" }
    var replacementEstimate: InventoryPriceEstimate? {
        isExcluded || needsAppraisal ? nil : InventoryPriceEstimate(value["replacement_cost"])
    }
    var resaleEstimate: InventoryPriceEstimate? {
        isExcluded || needsAppraisal ? nil : InventoryPriceEstimate(value["used_value"])
    }
    var hasResaleField: Bool { value["used_value"] != .null }
    var pricingReason: String {
        let supplied = value["pricing_reason"].string?.trimmingCharacters(in:.whitespacesAndNewlines) ?? ""
        if !supplied.isEmpty { return supplied }
        if isExcluded { return "Excluded from this claim; no automatic valuation." }
        if needsAppraisal { return "A specialist appraisal is needed before a value can be assigned." }
        return ""
    }
    var pricingStatusText: String {
        if isExcluded { return "Excluded from valuation" }
        if needsAppraisal { return "Appraisal required" }
        switch pricingStatus {
        case .idle: return "Valuation pending"
        case .queued: return "Valuation queued"
        case .searching: return "Searching source prices…"
        case .completed: return "Source search complete"
        case .unavailable: return "Valuation unavailable"
        case .interrupted: return "Valuation interrupted"
        }
    }
}

enum InventoryPricingStatus: String { case idle, queued, searching, completed, unavailable, interrupted }
enum InventoryPriceMatch: String { case exact, equivalent, unknown }

/// Sheets retain identity only; every body update resolves the current packet row.
struct InventorySelection: Identifiable {
    let id: String
    func resolve(in rows: [InventoryRow]) -> InventoryRow? { rows.first { $0.id == id } }
}

struct SavedInventorySummary: Identifiable {
    let id: String
    let capturedAt: String
    let country: String
    let completion: String

    init?(_ value: JSONValue) {
        guard value["synthetic"] != .bool(true), let id = value["id"].string,
              id.range(of:#"^[A-Za-z0-9_-]{1,80}$"#,options:.regularExpression) != nil else { return nil }
        self.id = id
        capturedAt = value["captured_at"].string ?? ""
        country = value["country"].string ?? ""
        completion = Self.completionText(value["completion"].string)
    }

    static func completionText(_ value: String?) -> String {
        switch value {
        case "ready": return "Ready"
        case "partial": return "Partial inventory"
        case "capturing": return "Capture in progress"
        case "processing": return "Processing"
        default: return "Saved inventory"
        }
    }

    var dateText: String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime,.withFractionalSeconds]
        let fractionalDate = formatter.date(from:capturedAt)
        formatter.formatOptions = [.withInternetDateTime]
        guard let date = fractionalDate ?? formatter.date(from:capturedAt) else { return "Saved inventory" }
        return date.formatted(date:.abbreviated,time:.shortened)
    }
}

enum InventoryMoney {
    static func decimal(_ value: JSONValue) -> Decimal? {
        let text: String
        switch value {
        case .string(let raw): text = raw.trimmingCharacters(in:.whitespacesAndNewlines)
        case .number(let number) where number.isFinite: text = String(number)
        default: return nil
        }
        guard text.count <= 128,
              text.range(of:#"^[+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$"#,options:.regularExpression) != nil,
              let amount = Decimal(string:text,locale:Locale(identifier:"en_US_POSIX")), !amount.isNaN, amount >= 0 else { return nil }
        return amount
    }
    static func formatted(_ value: Decimal) -> String? {
        let formatter = NumberFormatter()
        formatter.locale = Locale(identifier:"en_US_POSIX")
        formatter.numberStyle = .decimal; formatter.usesGroupingSeparator = true
        formatter.minimumFractionDigits = 2; formatter.maximumFractionDigits = 2
        return formatter.string(from:NSDecimalNumber(decimal:value))
    }
}

/// A displayable estimate must still have a supported amount and source evidence.
/// Verification belongs to the server; the phone never invents or completes prices.
struct InventoryPriceEstimate {
    let moneyText: String
    let match: InventoryPriceMatch
    let matchBasis: String
    let productName: String
    let sourceTitle: String
    let sourceURL: URL
    let retrievedDateText: String
    let condition: String
    let converted: Bool

    init?(_ value: JSONValue) {
        let currency = value["currency"].string ?? ""
        let source = value["source"].string?.trimmingCharacters(in:.whitespacesAndNewlines) ?? ""
        guard value["verified"] == .bool(true), !source.isEmpty,
              !(value["source_ref"].string?.trimmingCharacters(in:.whitespacesAndNewlines) ?? "").isEmpty,
              currency.range(of:#"^[A-Z]{3}$"#,options:.regularExpression) != nil,
              let components = URLComponents(string:value["url"].string ?? ""),
              ["https","http"].contains(components.scheme?.lowercased() ?? ""),
              let host = components.host, !host.isEmpty, components.user == nil, components.password == nil,
              let url = components.url,
              let retrieved = Self.retrievedDate(value["retrieved_at"].string ?? "") else { return nil }
        let low: Decimal, high: Decimal
        if value["amount"] != .null {
            guard let amount = InventoryMoney.decimal(value["amount"]) else { return nil }
            low = amount; high = amount
        } else {
            guard let minimum = InventoryMoney.decimal(value["low"]),
                  let maximum = InventoryMoney.decimal(value["high"]), minimum <= maximum else { return nil }
            low = minimum; high = maximum
        }
        guard let minimumText = InventoryMoney.formatted(low), let maximumText = InventoryMoney.formatted(high) else { return nil }
        moneyText = currency + " " + minimumText + (low == high ? "" : "–" + maximumText)
        match = InventoryPriceMatch(rawValue:value["match"].string ?? "") ?? .unknown
        matchBasis = value["match_basis"].string ?? ""
        productName = value["product_name"].string ?? ""
        sourceTitle = source; sourceURL = url
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier:"en_US_POSIX"); formatter.timeZone = TimeZone(secondsFromGMT:0)
        formatter.dateFormat = "d MMM yyyy"
        retrievedDateText = formatter.string(from:retrieved)
        condition = value["condition_assumed"].string ?? ""
        converted = value["converted"] == .bool(true)
    }
    private static func retrievedDate(_ text: String) -> Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime,.withFractionalSeconds]
        if let date = formatter.date(from:text) { return date }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from:text)
    }
    var matchLabel: String {
        switch match {
        case .exact: return "Exact product match"
        case .equivalent: return "Comparable offer"
        case .unknown: return "Product match unconfirmed"
        }
    }
}

func displayCategory(_ text: String) -> String {
    let cleaned = text.trimmingCharacters(in:.whitespacesAndNewlines)
    guard let first = cleaned.first else { return "Object" }
    return first.uppercased() + cleaned.dropFirst()
}

func dimension(_ number: Double) -> String {
    String(format: number.rounded() == number ? "%.0f" : "%.1f", number)
}

// Realtime uses a single, bounded stream in each direction. These models have no
// audio-device dependency so interruption and delivery rules can be checked on host.
enum RealtimeVoiceCapability {
    static func available(_ health: JSONValue) -> Bool {
        health["voice_transport"].string == "realtime" && health["assistant_provider"].string == "openai"
            && health["realtime_ready"] == .bool(true)
    }
}

enum RealtimeVoiceError: LocalizedError {
    case unavailable, invalidAudio, microphoneBacklog, playbackBacklog, microphoneUnavailable, audioConfigurationChanged, audioServicesReset
    var errorDescription: String? {
        switch self {
        case .unavailable: return "OpenAI Realtime voice is unavailable. Check the server’s OpenAI access, then reconnect voice."
        case .invalidAudio: return "OpenAI voice returned invalid audio. Reconnect voice to continue."
        case .microphoneBacklog: return "Voice upload could not keep up. Check Wi-Fi, then reconnect voice."
        case .playbackBacklog: return "Voice playback could not keep up. Reconnect voice to continue."
        case .microphoneUnavailable: return "The microphone is unavailable. Check audio permissions and reconnect voice."
        case .audioConfigurationChanged: return "The audio device changed. Tap Reconnect voice to use the current microphone and speaker."
        case .audioServicesReset: return "iPhone audio restarted. Tap Reconnect voice to continue."
        }
    }
}

struct RealtimeAudioChunk: Sendable {
    let pcm: Data
    let itemID: String
    let contentIndex: Int
    let endMilliseconds: Double
    var frames: Int { pcm.count / 2 }
    var key: String { itemID + ":" + String(contentIndex) }
    var receipt: JSONValue { .object(["type":.string("playback"),"item_id":.string(itemID),
        "content_index":.number(Double(contentIndex)),"played_ms":.number(endMilliseconds.rounded(.down))]) }
    init(_ event: JSONValue) throws {
        guard event["sample_rate"].number == 24000,
              let id = event["item_id"].string, !id.isEmpty, id.utf8.count <= 256,
              let index = event["content_index"].number, index.isFinite, index >= 0, index <= 100, index.rounded() == index,
              let end = event["audio_end_ms"].number, end.isFinite, end > 0, end <= 3_600_000,
              let encoded = event["pcm_base64"].string, encoded.utf8.count <= 640000,
              let data = Data(base64Encoded:encoded), !data.isEmpty, data.count % 2 == 0,
              data.count <= 480000, end + 0.1 >= Double(data.count) / 48 else { throw RealtimeVoiceError.invalidAudio }
        pcm = data; itemID = id; contentIndex = Int(index); endMilliseconds = end
    }
}

struct RealtimePlaybackLedger {
    struct Ticket: Equatable { let id = UUID(); let generation: UUID }
    private var generation = UUID()
    private var pending: [UUID: RealtimeAudioChunk] = [:]
    private var ends: [String: Double] = [:]
    private var blocked: [String] = []
    private(set) var queuedFrames = 0
    mutating func reserve(_ chunk: RealtimeAudioChunk) throws -> Ticket? {
        guard !blocked.contains(chunk.key) else { return nil }
        let previous = ends[chunk.key] ?? 0
        guard abs(chunk.endMilliseconds - previous - Double(chunk.pcm.count)/48) < 1 else {
            throw RealtimeVoiceError.invalidAudio
        }
        guard queuedFrames + chunk.frames <= 24000 * 120, pending.count < 4096 else { throw RealtimeVoiceError.playbackBacklog }
        // Retain only current/recent item offsets, never the whole conversation.
        if ends.count > 64 { ends = ends.filter { key, _ in pending.values.contains { $0.key == key } } }
        ends[chunk.key] = chunk.endMilliseconds
        let ticket = Ticket(generation:generation)
        pending[ticket.id] = chunk; queuedFrames += chunk.frames
        return ticket
    }
    mutating func complete(_ ticket: Ticket) -> JSONValue? {
        guard ticket.generation == generation, let chunk = pending.removeValue(forKey:ticket.id) else { return nil }
        queuedFrames -= chunk.frames
        return chunk.receipt
    }
    mutating func interrupt() {
        blocked = Array(Set(blocked + pending.values.map(\.key)).sorted().suffix(64))
        generation = UUID(); pending.removeAll(); queuedFrames = 0
    }
}

/// The audio render thread only yields to a bounded stream; one task sends it.
/// Overflow is explicit and ends the session instead of dropping owner speech.
final class RealtimeEventOutbox: @unchecked Sendable {
    let events: AsyncThrowingStream<JSONValue, Error>
    private let continuation: AsyncThrowingStream<JSONValue, Error>.Continuation
    private let lock = NSLock()
    private var active = true
    private var microphoneEnabled = false
    private var failure: Error?
    private let onFailure: (Error) -> Void
    init(capacity: Int = 96, onFailure: @escaping (Error) -> Void = { _ in }) {
        let pair = AsyncThrowingStream<JSONValue, Error>.makeStream(bufferingPolicy:.bufferingOldest(max(1,capacity)))
        events = pair.stream; continuation = pair.continuation; self.onFailure = onFailure
    }
    func enableMicrophone() { lock.lock(); microphoneEnabled = true; lock.unlock() }
    func offerPCM(_ data: Data) {
        lock.lock(); let allowed = active && microphoneEnabled; lock.unlock()
        guard allowed else { return }
        guard !data.isEmpty, data.count <= 32000, data.count % 2 == 0 else { fail(RealtimeVoiceError.invalidAudio); return }
        offer(.object(["type":.string("audio"),"pcm_base64":.string(data.base64EncodedString())]))
    }
    func offer(_ event: JSONValue) {
        lock.lock()
        guard active else { lock.unlock(); return }
        let result = continuation.yield(event)
        lock.unlock()
        if case .dropped = result { fail(RealtimeVoiceError.microphoneBacklog) }
    }
    func checkOpen() throws {
        lock.lock(); let active = active, failure = failure; lock.unlock()
        if let failure { throw failure }
        if !active { throw CancellationError() }
    }
    func fail(_ error: Error) {
        lock.lock()
        guard active else { lock.unlock(); return }
        active = false; failure = error; continuation.finish(throwing:error); lock.unlock()
        onFailure(error)
    }
    func finish() {
        lock.lock(); active = false; continuation.finish(); lock.unlock()
    }
}

/// Notifications can arrive after an intentional start/stop or on a previous
/// engine. Only a changed, active audio generation may fail the voice session.
struct RealtimeAudioLifecycleGate {
    private var generation = UUID()
    private var active = false
    mutating func beginStart() -> UUID {
        generation = UUID(); active = false
        return generation
    }
    mutating func didStart(generation: UUID) {
        guard generation == self.generation else { return }
        active = true
    }
    mutating func stop() { generation = UUID(); active = false }
    mutating func configurationChanged(generation: UUID, engineRunning: Bool, formatMatches: Bool, routeMatches: Bool) -> Bool {
        guard generation == self.generation, active else { return false }
        guard !engineRunning || !formatMatches || !routeMatches else { return false }
        active = false
        return true
    }
}
