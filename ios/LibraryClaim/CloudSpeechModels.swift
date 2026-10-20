import Foundation

/// Audio rules shared by the microphone path and host-side boundary checks.
struct CloudUtteranceBuffer {
    enum Event: Equatable { case collecting, discarded, utterance(Data) }
    private let frameSize = 320 // 20 ms at 16 kHz.
    private let maxSamples = 480_000
    private var pending: [Float] = []
    private var preRoll: [Float] = []
    private var samples: [Float] = []
    private var observedSamples = 0
    private var voicedSamples = 0
    private var quietSamples = 0
    private var noiseFloor: Float = 0.002
    private var finished = false
    private(set) var hasSpeech = false

    mutating func append(_ input: [Float]) -> Event {
        guard !finished else { return .collecting }
        pending.append(contentsOf:input.map { $0.isFinite ? $0 : 0 })
        var offset = 0
        while pending.count - offset >= frameSize {
            let frame = Array(pending[offset..<(offset + frameSize)])
            offset += frameSize
            observedSamples += frameSize
            let rms = sqrt(frame.reduce(Float(0)) { $0 + $1 * $1 } / Float(frameSize))
            let crossings = zip(frame,frame.dropFirst()).reduce(0) { count, pair in
                count + ((pair.0 < 0) != (pair.1 < 0) ? 1 : 0)
            }
            // Reject low energy and high-frequency clicks/hiss. This is a conservative
            // energy gate, not speaker identification; physical noise acceptance is separate.
            let voiced = rms > max(0.008,noiseFloor * 3) && crossings < frameSize / 3
            if voiced {
                if !hasSpeech { samples = preRoll; hasSpeech = true }
                voicedSamples += frameSize; quietSamples = 0
            } else {
                quietSamples += frameSize
                if !hasSpeech { noiseFloor = noiseFloor * 0.98 + min(rms,0.01) * 0.02 }
            }
            if hasSpeech {
                samples.append(contentsOf:frame.prefix(maxSamples - samples.count))
            } else {
                preRoll.append(contentsOf:frame)
                if preRoll.count > 4000 { preRoll.removeFirst(preRoll.count - 4000) }
            }
            if (hasSpeech && quietSamples >= 11_200) || samples.count >= maxSamples || observedSamples >= maxSamples {
                finished = true; pending = []; preRoll = []
                guard voicedSamples >= 3840 else { samples = []; return .discarded }
                let audio = CloudSpeechWAV.encode(samples:samples)
                samples = []
                return .utterance(audio)
            }
        }
        if offset > 0 { pending.removeFirst(offset) }
        return .collecting
    }
}

enum CloudSpeechWAV {
    static func encode(samples: [Float]) -> Data {
        var data = Data()
        func number<T: FixedWidthInteger>(_ value: T) {
            var little = value.littleEndian
            withUnsafeBytes(of:&little) { data.append(contentsOf:$0) }
        }
        let size = UInt32(samples.count * 2)
        data.append(contentsOf:"RIFF".utf8); number(size + 36)
        data.append(contentsOf:"WAVEfmt ".utf8); number(UInt32(16)); number(UInt16(1)); number(UInt16(1))
        number(UInt32(16_000)); number(UInt32(32_000)); number(UInt16(2)); number(UInt16(16))
        data.append(contentsOf:"data".utf8); number(size)
        for value in samples {
            number(Int16((max(-1,min(1,value.isFinite ? value : 0)) * 32767).rounded()))
        }
        return data
    }

    /// Require a complete, audible PCM16 WAV. Header-only, silent and truncated
    /// replies must never become successful setup-prompt delivery receipts.
    static func validate(_ data: Data, requiredRate: Int? = nil, maxSeconds: Int) -> Bool {
        guard data.count > 44, data.count <= 6_000_000,
              data.prefix(4) == Data("RIFF".utf8), data[8..<12] == Data("WAVE".utf8) else { return false }
        func integer(_ offset: Int, _ count: Int) -> Int {
            (0..<count).reduce(0) { $0 | Int(data[offset + $1]) << ($1 * 8) }
        }
        guard integer(4,4) + 8 == data.count else { return false }
        var rate: Int?, payload: Range<Int>?, offset = 12
        while offset + 8 <= data.count {
            let size = integer(offset + 4,4), start = offset + 8
            guard size <= data.count - start else { return false }
            let tag = String(decoding:data[offset..<(offset + 4)],as:UTF8.self)
            if tag == "fmt " {
                guard size >= 16, integer(start,2) == 1, integer(start + 2,2) == 1,
                      integer(start + 14,2) == 16, integer(start + 12,2) == 2 else { return false }
                rate = integer(start + 4,4)
            } else if tag == "data" { payload = start..<(start + size) }
            offset = start + size + (size % 2)
        }
        guard let rate, let payload, (8000...48000).contains(rate),
              requiredRate == nil || rate == requiredRate,
              payload.count % 2 == 0, payload.count >= rate / 5,
              payload.count <= rate * 2 * maxSeconds else { return false }
        var audible = 0
        for position in stride(from:payload.lowerBound,to:payload.upperBound,by:2) {
            if abs(Int(Int16(bitPattern:UInt16(integer(position,2))))) > 32 { audible += 1 }
        }
        return audible >= rate / 50
    }
}

enum CloudSpeechLanguage {
    private static let codes = Set(Locale.LanguageCode.isoLanguageCodes.map(\.identifier))
    static func code(_ language: String, explicitLocale: String? = nil) -> String? {
        if let explicitLocale,
           let code = Locale(identifier:explicitLocale).language.languageCode?.identifier,
           codes.contains(code) { return code }
        func normalized(_ value: String) -> String {
            value.trimmingCharacters(in:.whitespacesAndNewlines)
                .folding(options:[.caseInsensitive,.diacriticInsensitive],locale:Locale(identifier:"en"))
        }
        let target = normalized(language)
        guard !target.isEmpty else { return nil }
        if codes.contains(target) { return target }
        for code in codes.sorted() {
            let labels = [Locale(identifier:"en").localizedString(forLanguageCode:code),
                          Locale(identifier:code).localizedString(forLanguageCode:code)]
            if labels.compactMap({ $0 }).contains(where:{ normalized($0) == target }) { return code }
        }
        return nil // Omit the transcription hint; the provider can detect the language.
    }
}

/// Only a successful completion of the currently owned player counts as delivery.
struct CloudSpeechPlaybackReceipt {
    struct Delivery { let prompt: String? }
    private var current: (id: UUID, generation: UUID, prompt: String?)?
    mutating func begin(id: UUID, generation: UUID, prompt: String?) { current = (id,generation,prompt) }
    mutating func cancel() { current = nil }
    mutating func complete(id: UUID, generation: UUID, success: Bool) -> Delivery? {
        guard let current, current.id == id, current.generation == generation else { return nil }
        self.current = nil
        return success ? Delivery(prompt:current.prompt) : nil
    }
}

enum CloudSpeechError: LocalizedError {
    case invalidAudio, invalidText, invalidResponse, playbackUnavailable
    var errorDescription: String? {
        switch self {
        case .invalidAudio: return "The recorded audio is invalid or longer than 30 seconds. Reconnect voice to try again."
        case .invalidText: return "The speech reply is empty or too long. Reconnect voice to try again."
        case .invalidResponse: return "Cloud speech returned an invalid audio or transcription response. Reconnect voice to try again."
        case .playbackUnavailable: return "Cloud speech audio could not be played. Reconnect voice to try again."
        }
    }
}

/// A successful capture request consumes onboarding. Its final speech must have
/// completely played first, with no late synthesis against the consumed target.
enum CloudSpeechHandoff {
    @MainActor static func request(
        prepareAnnouncement: @MainActor () async throws -> String,
        play: @MainActor (String) async throws -> Void,
        checkCurrent: @MainActor () throws -> Void,
        requestCapture: @MainActor () async throws -> Void
    ) async throws {
        try Task.checkCancellation(); try checkCurrent()
        let announcement = try await prepareAnnouncement()
        try Task.checkCancellation(); try checkCurrent()
        try await play(announcement)
        try Task.checkCancellation(); try checkCurrent()
        try await requestCapture()
        try Task.checkCancellation(); try checkCurrent()
    }
}

/// Proactive messages share the conversation's audio ownership. There is only
/// one pending cue, and an uncertain paid outcome pauses the loop until reconnect.
struct CloudGuidanceGate {
    private var pending: (id: UUID, activity: UUID)?
    private var nextAllowedTime = -Double.infinity
    private var paused = false
    private var lastEventID: String?

    mutating func begin(now: TimeInterval, idle: Bool, activity: UUID) -> UUID? {
        guard idle, !paused, pending == nil, now >= nextAllowedTime else { return nil }
        let id = UUID()
        pending = (id,activity); nextAllowedTime = now + 8
        return id
    }

    mutating func complete(request: UUID, activity: UUID, idle: Bool, eventID: String) -> Bool {
        guard let pending, pending.id == request else { return false }
        self.pending = nil
        guard idle, pending.activity == activity, !eventID.isEmpty, eventID != lastEventID else { return false }
        lastEventID = eventID
        return true
    }

    mutating func cancelPending() { pending = nil }
    mutating func pause() { pending = nil; paused = true }

}
