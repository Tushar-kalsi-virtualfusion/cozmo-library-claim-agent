import Foundation

/// The transport is replaced so contract/action tests never call a paid provider.
final class ClaudeReplyProtocol: URLProtocol, @unchecked Sendable {
    private static let lock = NSLock()
    nonisolated(unsafe) private static var captured: [(String, String, JSONValue)] = []
    nonisolated(unsafe) private static var status = 200
    nonisolated(unsafe) private static var reply = #"{"reply":"Done","intent":"provide","language":"","country":"","currency":"","capture_mode":"","speech_locale":"","tool_name":"","tool_arguments":""}"#
    static func configure(status: Int = 200) {
        lock.lock(); defer { lock.unlock() }
        self.status = status; captured = []
    }
    static var requests: [(String, String, JSONValue)] {
        lock.lock(); defer { lock.unlock() }; return captured
    }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var data = request.httpBody ?? Data()
        if let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var buffer = [UInt8](repeating:0,count:1024)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer,maxLength:buffer.count)
                if count <= 0 { break }; data.append(contentsOf:buffer.prefix(count))
            }
        }
        let body = (try? JSONDecoder().decode(JSONValue.self,from:data)) ?? .null
        Self.lock.lock()
        Self.captured.append((request.httpMethod ?? "",request.url!.path,body))
        let status = Self.status, reply = Self.reply
        Self.lock.unlock()
        client?.urlProtocol(self,didReceive:HTTPURLResponse(url:request.url!,statusCode:status,httpVersion:nil,headerFields:nil)!,cacheStoragePolicy:.notAllowed)
        client?.urlProtocol(self,didLoad:Data(reply.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@main struct ClaudeAssistantChecks {
    @MainActor static func main() async throws {
        let request = ClaudeAssistantRequest(mode:.setup,text:"मैं हिंदी बोलना चाहता हूँ",phase:"collecting",
            preferences:.object([:]),previous:"Which language?",language:"Hindi")
        let body = request.body
        precondition(UUID(uuidString:body["request_id"].string ?? "") != nil)
        precondition(body == request.body, "A retry must reuse the same request identity")
        precondition(body["mode"].string == "setup" && body["allow_actions"] == .bool(true))
        precondition(body["text"].string == "मैं हिंदी बोलना चाहता हूँ" && body["language"].string == "Hindi")
        let bounded = ClaudeAssistantRequest(mode:.reply,text:String(repeating:"ह",count:4001),
            previous:String(repeating:"x",count:1201)).body
        precondition(bounded["text"].string?.unicodeScalars.count == 4000)
        precondition(bounded["previous"].string?.count == 1200)

        let setupValue = try json(#"{"reply":"किस देश में?","intent":"provide","language":"Hindi","country":"","currency":"","capture_mode":"lidar","speech_locale":"hi-IN","tool_name":"","tool_arguments":""}"#)
        let setup = try ClaudeAssistantTurn(setupValue,mode:.setup,allowActions:true)
        precondition(setup.speechLocale == "hi-IN" && setup.captureMode == "lidar")
        precondition(setup.reply == "किस देश में?", "A model reply must retain the selected language")
        let plan = try json(#"{"reply":"I will exclude it.","tool_name":"correct_inventory","tool_arguments":"{\"action\":\"exclude\",\"ref_id\":\"book-1\"}"}"#)
        let valid = try ClaudeAssistantTurn(plan,mode:.reply,allowActions:true)
        precondition(valid.toolName == "correct_inventory")
        for mode in [ClaudeAssistantMode.setup,.translate] {
            expectInvalid { _ = try ClaudeAssistantTurn(plan,mode:mode,allowActions:true) }
        }
        expectInvalid { _ = try ClaudeAssistantTurn(plan,mode:.reply,allowActions:false) }
        for raw in [#"{"tool_name":"correct_inventory","tool_arguments":"{}"}"#,
                    #"{"reply":"Okay","tool_name":"request_capture","tool_arguments":"{}"}"#,
                    #"{"reply":"Okay","tool_name":"correct_inventory","tool_arguments":"[]"}"#,
                    #"{"reply":"Okay","tool_name":"correct_inventory","tool_arguments":"{"}"#,
                    #"{"reply":"","tool_name":"correct_inventory","tool_arguments":"{}"}"#,
                    #"{"reply":"Okay","country":"Unknown"}"#,
                    #"{"reply":"Okay","intent":"start"}"#,
                    #"{"reply":true}"#] {
            let value = try json(raw)
            expectInvalid { _ = try ClaudeAssistantTurn(value,mode:.reply,allowActions:true) }
        }

        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ClaudeReplyProtocol.self]
        let backend = try BackendClient(address:"http://localhost",token:"test",configuration:configuration)
        ClaudeReplyProtocol.configure()
        _ = try await backend.assistant("api/onboarding/test/local-voice",request:request)
        _ = try await backend.assistant("api/onboarding/test/local-voice",request:request)
        precondition(ClaudeReplyProtocol.requests.count == 2)
        precondition(ClaudeReplyProtocol.requests.allSatisfy {
            $0.0 == "POST" && $0.1 == "/api/onboarding/test/local-voice/assistant" && $0.2 == body
        })

        let arguments = #"{"action":"exclude","ref_id":"book-1"}"#
        ClaudeReplyProtocol.configure()
        let executor = LocalVoiceToolExecutor(client:backend,path:"api/sweeps/test/local-voice",allowed:["correct_inventory","compare_market"])
        _ = try await executor.call(name:"correct_inventory",arguments:arguments)
        do { _ = try await executor.call(name:"compare_market",arguments:#"{"country":"India","currency":"INR"}"#); fatalError("A second action must be rejected") }
        catch ClaudeAssistantError.actionUnavailable {}
        precondition(ClaudeReplyProtocol.requests.count == 1)
        let action = ClaudeReplyProtocol.requests[0]
        precondition(action.0 == "POST" && action.1 == "/api/sweeps/test/local-voice")
        precondition(action.2["name"].string == "correct_inventory" && action.2["arguments"]["ref_id"].string == "book-1")

        ClaudeReplyProtocol.configure()
        let stopped = LocalVoiceToolExecutor(client:backend,path:"api/sweeps/test/local-voice",allowed:["correct_inventory"])
        stopped.invalidate()
        do { _ = try await stopped.call(name:"correct_inventory",arguments:arguments); fatalError("Stopped voice must not act") }
        catch ClaudeAssistantError.actionUnavailable {}
        let cancelled = LocalVoiceToolExecutor(client:backend,path:"api/sweeps/test/local-voice",allowed:["correct_inventory"])
        let task = Task { @MainActor in
            withUnsafeCurrentTask { $0?.cancel() }
            return try await cancelled.call(name:"correct_inventory",arguments:arguments)
        }
        do { _ = try await task.value; fatalError("A cancelled turn must not act") }
        catch is CancellationError {}
        precondition(ClaudeReplyProtocol.requests.isEmpty)

        let restricted = LocalVoiceToolExecutor(client:backend,path:"api/sweeps/test/local-voice",allowed:["compare_market"])
        do { _ = try await restricted.call(name:"correct_inventory",arguments:arguments); fatalError("The current tool list must authorize the action") }
        catch ClaudeAssistantError.actionUnavailable {}
        precondition(ClaudeReplyProtocol.requests.isEmpty)

        ClaudeReplyProtocol.configure(status:503)
        let ambiguous = LocalVoiceToolExecutor(client:backend,path:"api/sweeps/test/local-voice",allowed:["correct_inventory"])
        do { _ = try await ambiguous.call(name:"correct_inventory",arguments:arguments); fatalError("The server failure must surface") }
        catch BackendError.response(503, _) {}
        do { _ = try await ambiguous.call(name:"correct_inventory",arguments:arguments); fatalError("An ambiguous action response must never auto-retry") }
        catch ClaudeAssistantError.actionUnavailable {}
        precondition(ClaudeReplyProtocol.requests.count == 1)

        // Onboarding may close as soon as request_capture is accepted. Translation
        // must finish first, and must never hold the server assistant lock at handoff.
        ClaudeReplyProtocol.configure()
        let handoffPath = "api/onboarding/test/local-voice"
        let announcement = try await LocalCaptureHandoff.request(
            prepareAnnouncement: {
                try await backend.assistant(handoffPath,request:ClaudeAssistantRequest(
                    mode:.translate,text:"Starting capture.",language:"Hindi",allowActions:false)).reply
            },
            checkCurrent: {},
            requestCapture: {
                _ = try await backend.call(handoffPath,body:.object(["name":.string("request_capture"),"arguments":.object(["ready":.bool(true)])]))
            })
        precondition(announcement == "Done")
        precondition(ClaudeReplyProtocol.requests.map { $0.1 } == [
            "/api/onboarding/test/local-voice/assistant", "/api/onboarding/test/local-voice"
        ], "Translation must finish before requesting capture, with no later onboarding assistant request")

        ClaudeReplyProtocol.configure()
        var current = true
        do {
            _ = try await LocalCaptureHandoff.request(
                prepareAnnouncement: { current = false; return "Translated announcement" },
                checkCurrent: { if !current { throw CancellationError() } },
                requestCapture: { _ = try await backend.call(handoffPath,body:.object(["name":.string("request_capture")])) })
            fatalError("A stop or new session during translation must cancel capture")
        } catch is CancellationError {}
        precondition(ClaudeReplyProtocol.requests.isEmpty)

        do {
            _ = try await LocalCaptureHandoff.request(
                prepareAnnouncement: { throw ClaudeAssistantError.invalidResponse },
                checkCurrent: {},
                requestCapture: { _ = try await backend.call(handoffPath,body:.object(["name":.string("request_capture")])) })
            fatalError("A failed translation must not request capture")
        } catch ClaudeAssistantError.invalidResponse {}
        precondition(ClaudeReplyProtocol.requests.isEmpty)
        print("Claude wire, action restrictions, cancellation and single-use tool checks passed")
    }

    static func json(_ value: String) throws -> JSONValue { try JSONDecoder().decode(JSONValue.self,from:Data(value.utf8)) }
    static func expectInvalid(_ operation: () throws -> Void) {
        do { try operation(); fatalError("Invalid or forbidden assistant response accepted") }
        catch ClaudeAssistantError.invalidResponse {}
        catch { fatalError("Unexpected error: \(error)") }
    }
}
