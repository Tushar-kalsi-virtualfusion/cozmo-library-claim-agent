import Foundation

/// Fail at the URL loading boundary after a mutating request has been submitted.
final class FailedConnectionProtocol: URLProtocol, @unchecked Sendable {
    private static let lock = NSLock()
    nonisolated(unsafe) private static var failure = URLError.Code.cannotConnectToHost
    nonisolated(unsafe) private static var count = 0
    static func configure(_ code: URLError.Code) {
        lock.lock(); defer { lock.unlock() }
        failure = code; count = 0
    }
    static var requests: Int {
        lock.lock(); defer { lock.unlock() }; return count
    }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.lock.lock(); Self.count += 1; let code = Self.failure; Self.lock.unlock()
        client?.urlProtocol(self, didFailWithError: URLError(code))
    }
    override func stopLoading() {}
}

@main struct BackendConnectionChecks {
    static func main() async throws {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [FailedConnectionProtocol.self]
        let backend = try BackendClient(address: "http://paired-mac.local:4180", token: "fixture", configuration: config)
        for code in [URLError.Code.cannotConnectToHost, .cannotFindHost, .networkConnectionLost, .timedOut] {
            FailedConnectionProtocol.configure(code)
            do {
                _ = try await backend.call("api/sweeps/example/local-voice", body: .object([:]))
                fatalError("Transport failure must surface")
            } catch BackendError.connectionUnavailable { }
            precondition(FailedConnectionProtocol.requests == 1, "An ambiguous POST must not automatically execute twice")
        }
        FailedConnectionProtocol.configure(.cancelled)
        do {
            _ = try await backend.call("api/onboarding/example/local-voice/assistant", body: .object([:]))
            fatalError("Cancellation must surface")
        } catch let failure as URLError {
            precondition(failure.code == .cancelled, "Cancellation must not become a connection error")
        }
        precondition(FailedConnectionProtocol.requests == 1)
        print("Backend connection checks passed: useful failure, no ambiguous POST retry, cancellation preserved")
    }
}
