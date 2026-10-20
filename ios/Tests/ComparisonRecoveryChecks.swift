import Foundation

final class ReplyProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var replies: [(Int, String)] = []
    nonisolated(unsafe) static var methods: [String] = []
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.methods.append(request.httpMethod ?? "")
        let (status, body) = Self.replies.removeFirst()
        client?.urlProtocol(self,didReceive:HTTPURLResponse(url:request.url!,statusCode:status,httpVersion:nil,headerFields:nil)!,cacheStoragePolicy:.notAllowed)
        client?.urlProtocol(self,didLoad:Data(body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@main struct ComparisonRecoveryChecks {
    static func main() async throws {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [ReplyProtocol.self]
        let backend = try BackendClient(address:"http://localhost",token:"test",configuration:config)
        ReplyProtocol.replies = [(409,#"{"detail":{"code":"capacity","message":"Busy"}}"#),
                                 (200,#"{"status":"complete"}"#)]
        do { _ = try await backend.cameraComparison(sweep:"same-id"); fatalError("Capacity must remain retryable") }
        catch BackendError.comparisonConflict(.capacity, _) {}
        let retried = try await backend.cameraComparison(sweep:"same-id")
        precondition(retried["status"].string == "complete")
        precondition(ReplyProtocol.methods == ["POST","POST"])
        ReplyProtocol.methods = []
        ReplyProtocol.replies = [(409,#"{"detail":{"code":"already_running","message":"In progress"}}"#),
                                 (200,#"{"status":"running"}"#)]
        do { _ = try await backend.cameraComparison(sweep:"same-id"); fatalError("Running is not terminal") }
        catch BackendError.comparisonConflict(.alreadyRunning, _) {}
        precondition(ReplyProtocol.methods == ["POST","GET"])
        ReplyProtocol.methods = []
        ReplyProtocol.replies = [(409,#"{"detail":{"code":"already_running","message":"In progress"}}"#),
                                 (200,#"{"status":"complete"}"#)]
        let saved = try await backend.cameraComparison(sweep:"same-id")
        precondition(saved["status"].string == "complete")
        precondition(ReplyProtocol.methods == ["POST","GET"])
        ReplyProtocol.replies = [(409,#"{"detail":{"code":"source_unavailable","message":"Missing evidence"}}"#)]
        do { _ = try await backend.cameraComparison(sweep:"same-id"); fatalError("Unavailable source must be classified") }
        catch BackendError.comparisonConflict(.unavailable, _) {}
        ReplyProtocol.replies = [(409,#"{"detail":"Unrecognized conflict"}"#)]
        do { _ = try await backend.cameraComparison(sweep:"same-id"); fatalError("Unknown conflicts remain retryable") }
        catch BackendError.response(409, _) {}
        ReplyProtocol.replies = [(409,#"{"detail":{"code":"handoff_unavailable","message":"Expired with no saved sweep"}}"#)]
        do { _ = try await backend.call("api/sweeps",body:.object([:])); fatalError("Definitive no-handoff response must permit a new conversation") }
        catch BackendError.handoffUnavailable(_) {}
        print("Comparison recovery checks passed: capacity retries, running reads saved report, terminal/unavailable classification, conservative unknown conflict")
    }
}
