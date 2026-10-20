import Foundation

struct CloudTranscriptionRequest: Sendable {
    let body: JSONValue
    init(audio: Data, language: String? = nil) throws {
        guard audio.count <= 960044, CloudSpeechWAV.validate(audio,requiredRate:16000,maxSeconds:30) else {
            throw CloudSpeechError.invalidAudio
        }
        var fields: [String: JSONValue] = ["request_id":.string(UUID().uuidString),"audio_base64":.string(audio.base64EncodedString())]
        if let language, !language.isEmpty { fields["language"] = .string(language) }
        body = .object(fields)
    }
}

struct CloudSynthesisRequest: Sendable {
    let body: JSONValue
    init(text: String, language: String, guidanceEventID: String? = nil) throws {
        let text = text.trimmingCharacters(in:.whitespacesAndNewlines)
        guard !text.isEmpty, text.unicodeScalars.count <= 1200,
              !language.isEmpty, language.unicodeScalars.count <= 80 else { throw CloudSpeechError.invalidText }
        var fields: [String: JSONValue] = ["request_id":.string(UUID().uuidString),"text":.string(text),"language":.string(language)]
        if let guidanceEventID {
            guard !guidanceEventID.isEmpty, guidanceEventID.utf8.count <= 256 else { throw CloudSpeechError.invalidText }
            fields["guidance_event_id"] = .string(guidanceEventID)
        }
        body = .object(fields)
    }
}

struct CloudTranscription: Sendable {
    let text: String
    let language: String
    init(_ value: JSONValue) throws {
        guard let text = value["text"].string, text.unicodeScalars.count <= 4000,
              let language = value["language"].string, language.count <= 80 else { throw CloudSpeechError.invalidResponse }
        self.text = text.trimmingCharacters(in:.whitespacesAndNewlines); self.language = language
    }
}

extension BackendClient {
    func transcribe(_ path: String, request: CloudTranscriptionRequest) async throws -> CloudTranscription {
        let result = try await speechCall(path + "/transcribe",body:request.body)
        try Task.checkCancellation()
        return try CloudTranscription(result)
    }

    func synthesize(_ path: String, request: CloudSynthesisRequest) async throws -> Data {
        let result = try await speechCall(path + "/speech",body:request.body)
        try Task.checkCancellation()
        guard result["audio_mime"].string == "audio/wav", let encoded = result["audio_base64"].string,
              encoded.utf8.count <= 8_000_000, let audio = Data(base64Encoded:encoded),
              CloudSpeechWAV.validate(audio,maxSeconds:120) else { throw CloudSpeechError.invalidResponse }
        return audio
    }

    private func speechCall(_ path: String, body: JSONValue) async throws -> JSONValue {
        try Task.checkCancellation()
        // URLRequest's timeout is an inactivity timeout. Bound total elapsed time
        // too, and cancel the request on Stop. Never replay an ambiguous request.
        return try await withThrowingTaskGroup(of:JSONValue.self) { group in
            group.addTask { try await self.call(path,body:body,timeout:45) }
            group.addTask {
                try await Task.sleep(for:.seconds(45))
                throw BackendError.response(504,"Speech timed out; its outcome is unknown. Reconnect voice to continue.")
            }
            defer { group.cancelAll() }
            guard let value = try await group.next() else { throw CancellationError() }
            return value
        }
    }
}

struct CloudGuidance: Sendable {
    let reply: String
    let eventID: String
    init(_ value: JSONValue) throws {
        guard let reply = value["reply"].string, reply.unicodeScalars.count <= 1200,
              let eventID = value["event_id"].string, eventID.utf8.count <= 256,
              reply.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty || !eventID.isEmpty else {
            throw CloudSpeechError.invalidResponse
        }
        self.reply = reply.trimmingCharacters(in:.whitespacesAndNewlines); self.eventID = eventID
    }
}

extension BackendClient {
    func guidance(_ path: String, requestID: UUID) async throws -> CloudGuidance {
        let response = try await speechCall(path + "/guidance",body:.object(["request_id":.string(requestID.uuidString)]))
        try Task.checkCancellation()
        return try CloudGuidance(response)
    }
}
