import Foundation

final class SpeechHTTPProtocol: URLProtocol, @unchecked Sendable {
    private static let lock = NSLock()
    nonisolated(unsafe) private static var captured: [URLRequest] = []
    nonisolated(unsafe) private static var payload = Data()
    nonisolated(unsafe) private static var status = 200
    nonisolated(unsafe) private static var held = false
    static func configure(_ reply: JSONValue, status: Int = 200, held: Bool = false) throws {
        lock.lock(); defer { lock.unlock() }
        payload = try JSONEncoder().encode(reply); self.status = status; self.held = held; captured = []
    }
    static var requests: [URLRequest] { lock.lock(); defer { lock.unlock() }; return captured }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.lock.lock(); Self.captured.append(request); let payload = Self.payload, status = Self.status, held = Self.held; Self.lock.unlock()
        if held { return }
        client?.urlProtocol(self,didReceive:HTTPURLResponse(url:request.url!,statusCode:status,httpVersion:nil,headerFields:nil)!,cacheStoragePolicy:.notAllowed)
        client?.urlProtocol(self,didLoad:payload); client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@main struct CloudSpeechHTTPChecks {
    static func main() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [SpeechHTTPProtocol.self]
        let client = try BackendClient(address:"http://localhost",token:"paired-test",configuration:configuration)
        let path = "api/onboarding/test/local-voice"
        let wav = CloudSpeechWAV.encode(samples:(0..<16000).map { 0.05 * sin(Float($0) * 0.08) })
        let transcription = try CloudTranscriptionRequest(audio:wav,language:"hi")
        precondition(transcription.body == transcription.body, "Request IDs must remain stable")
        precondition(UUID(uuidString:transcription.body["request_id"].string ?? "") != nil)
        precondition(transcription.body["audio_base64"].string == wav.base64EncodedString())
        precondition(transcription.body["language"].string == "hi")
        let autodetect = try CloudTranscriptionRequest(audio:wav)
        precondition(autodetect.body["language"] == .null)
        try SpeechHTTPProtocol.configure(.object(["text":.string("हाँ"),"language":.string("hi")]))
        let result = try await client.transcribe(path,request:transcription)
        precondition(result.text == "हाँ")
        precondition(SpeechHTTPProtocol.requests.count == 1)
        let sent = SpeechHTTPProtocol.requests[0]
        precondition(sent.url?.path == "/api/onboarding/test/local-voice/transcribe" && sent.httpMethod == "POST")
        precondition(sent.value(forHTTPHeaderField:"Authorization") == "Bearer paired-test")
        precondition(sent.timeoutInterval <= 45)

        let synthesis = try CloudSynthesisRequest(text:"नमस्ते",language:"Hindi")
        try SpeechHTTPProtocol.configure(.object(["audio_base64":.string(wav.base64EncodedString()),"audio_mime":.string("audio/wav")]))
        let synthesized = try await client.synthesize(path,request:synthesis)
        precondition(synthesized == wav)
        precondition(SpeechHTTPProtocol.requests[0].url?.path == "/api/onboarding/test/local-voice/speech")
        precondition(synthesis.body["text"].string == "नमस्ते")
        precondition(synthesis.body["guidance_event_id"] == .null)
        let guided = try CloudSynthesisRequest(text:"Saved. Move along.",language:"English",guidanceEventID:"guide-123")
        precondition(guided.body["guidance_event_id"].string == "guide-123")
        try SpeechHTTPProtocol.configure(.object(["detail":.object(["code":.string("guidance_stale"),"message":.string("View changed")])]),status:409)
        do { _ = try await client.synthesize(path,request:guided); fatalError("Stale guide audio accepted") }
        catch BackendError.staleGuidance {}
        precondition(SpeechHTTPProtocol.requests.count == 1, "A stale cue must not trigger a second synthesis")
        for bad in ["",String(repeating:"क्",count:601)] {
            do { _ = try CloudSynthesisRequest(text:bad,language:"Hindi"); fatalError("Empty/oversize speech text accepted") }
            catch CloudSpeechError.invalidText {}
        }
        for bad in [Data(),Data(repeating:0,count:960045)] {
            do { _ = try CloudTranscriptionRequest(audio:bad); fatalError("Invalid/oversize recording accepted") }
            catch CloudSpeechError.invalidAudio {}
        }
        try SpeechHTTPProtocol.configure(.object(["audio_base64":.string(Data("no audio".utf8).base64EncodedString()),"audio_mime":.string("audio/wav")]))
        do { _ = try await client.synthesize(path,request:synthesis); fatalError("Malformed audio accepted") }
        catch CloudSpeechError.invalidResponse {}
        for invalid in [CloudSpeechWAV.encode(samples:[Float](repeating:0,count:16000)),Data(wav.dropLast(10))] {
            try SpeechHTTPProtocol.configure(.object(["audio_base64":.string(invalid.base64EncodedString()),"audio_mime":.string("audio/wav")]))
            do { _ = try await client.synthesize(path,request:synthesis); fatalError("Blank or partial audio accepted") }
            catch CloudSpeechError.invalidResponse {}
        }
        try SpeechHTTPProtocol.configure(.object(["detail":.string("Speech outcome unknown. Reconnect voice.")]),status:503)
        do { _ = try await client.transcribe(path,request:transcription); fatalError("Server error swallowed") }
        catch BackendError.response(503,let detail) { precondition(detail.contains("outcome unknown")) }
        precondition(SpeechHTTPProtocol.requests.count == 1, "Ambiguous speech requests must not retry")
        try SpeechHTTPProtocol.configure(.object(["text":.string("yes"),"language":.string("en")]))
        let task = Task {
            withUnsafeCurrentTask { $0?.cancel() }
            return try await client.transcribe(path,request:transcription)
        }
        do { _ = try await task.value; fatalError("Cancelled transcription sent") }
        catch is CancellationError {}
        precondition(SpeechHTTPProtocol.requests.isEmpty)
        try SpeechHTTPProtocol.configure(.object([:]),held:true)
        let pending = Task { try await client.synthesize(path,request:synthesis) }
        for _ in 0..<100 where SpeechHTTPProtocol.requests.isEmpty { try await Task.sleep(for:.milliseconds(2)) }
        precondition(SpeechHTTPProtocol.requests.count == 1, "The cancellation fixture must reach a pending request")
        pending.cancel()
        do { _ = try await pending.value; fatalError("An in-flight stopped request returned audio") }
        catch is CancellationError {}
        catch let error as URLError { precondition(error.code == .cancelled) }
        precondition(SpeechHTTPProtocol.requests.count == 1, "Cancellation must never resubmit a paid request")
        try SpeechHTTPProtocol.configure(.object(["reply":.string("The book is verified and saved."),"event_id":.string("saved-book-1")]))
        let cueID = UUID()
        let cue = try await client.guidance(path,requestID:cueID)
        precondition(cue.eventID == "saved-book-1" && cue.reply == "The book is verified and saved.")
        precondition(SpeechHTTPProtocol.requests.count == 1)
        precondition(SpeechHTTPProtocol.requests[0].url?.path == "/api/onboarding/test/local-voice/guidance")
        let empty = try CloudGuidance(.object(["reply":.string(""),"event_id":.string("")]))
        precondition(empty.reply.isEmpty)
        do { _ = try CloudGuidance(.object(["reply":.string("Saved"),"event_id":.string("")]))
            fatalError("A proactive cue without identity cannot be deduplicated")
        } catch CloudSpeechError.invalidResponse {}
        print("Cloud speech wire, bounds, one-attempt, guidance and cancellation checks passed")
    }
}
