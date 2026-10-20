import Foundation

enum ComparisonConflict: String {
    case capacity, alreadyRunning = "already_running", unavailable = "source_unavailable"
}

enum BackendError: LocalizedError {
    case invalidAddress, response(Int, String), disconnected
    case connectionUnavailable, staleGuidance
    case comparisonConflict(ComparisonConflict, String)
    case handoffUnavailable(String)
    case backpressure(Double)
    var errorDescription: String? {
        switch self {
        case .invalidAddress: return "The app needs a configured Mac address. Run scripts/configure_ios.py and rebuild."
        case .response(let status, let detail): return "Server \(status): \(detail)"
        case .comparisonConflict(_, let detail): return detail
        case .handoffUnavailable(let detail): return detail
        case .backpressure: return "Server is processing earlier photos. This photo remains saved for retry."
        case .disconnected: return "Live voice is disconnected"
        case .staleGuidance: return "The view changed; this guidance audio was discarded."
        case .connectionUnavailable: return "The iPhone could not reach your Mac. Keep both devices on the same Wi-Fi and the Mac awake, then tap Reconnect voice."
        }
    }
}

actor BackendClient {
    let base: URL
    private let token: String
    private let session: URLSession
    private var socket: URLSessionWebSocketTask?
    private var socketGeneration = UUID()
    private var connectionEvents: [JSONValue] = []
    init(address: String, token: String, configuration: URLSessionConfiguration = .default) throws {
        guard let url = URL(string: address.trimmingCharacters(in: .whitespacesAndNewlines)),
              ["http", "https"].contains(url.scheme), url.host != nil,
              url.user == nil, url.password == nil, url.query == nil, url.fragment == nil else { throw BackendError.invalidAddress }
        base = url; self.token = token
        let config = configuration
        config.timeoutIntervalForRequest = 45
        config.timeoutIntervalForResource = 300
        config.waitsForConnectivity = false
        session = URLSession(configuration: config)
    }
    private func request(_ path: String, method: String = "GET", body: Data? = nil) -> URLRequest {
        var request = URLRequest(url: base.appendingPathComponent(path))
        request.httpMethod = method; request.httpBody = body
        request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization")
        if body != nil { request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        return request
    }
    func call(_ path: String, body: JSONValue? = nil, timeout: TimeInterval = 45) async throws -> JSONValue {
        let bytes = try body.map { try JSONEncoder().encode($0) }
        return try await rawCall(path, body: bytes, timeout: timeout)
    }
    func rawCall(_ path: String, body: Data? = nil, timeout: TimeInterval = 45) async throws -> JSONValue {
        try Task.checkCancellation()
        var request = request(path, method: body == nil ? "GET" : "POST", body: body)
        request.timeoutInterval = timeout
        let started = Date()
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            recordConnection(path: path, started: started, error: error as NSError)
            if let failure = error as? URLError,
               [.cannotFindHost, .dnsLookupFailed, .cannotConnectToHost, .networkConnectionLost,
                .notConnectedToInternet, .timedOut].contains(failure.code) {
                throw BackendError.connectionUnavailable
            }
            throw error
        }
        try Task.checkCancellation()
        guard let http = response as? HTTPURLResponse else { throw BackendError.disconnected }
        recordConnection(path: path, started: started, status: http.statusCode)
        let json = (try? JSONDecoder().decode(JSONValue.self, from: data)) ?? .null
        guard (200..<300).contains(http.statusCode) else {
            let detail = json["detail"]
            if http.statusCode == 429, detail["processing"].string == "retry_backpressure" {
                throw BackendError.backpressure(min(15,max(1,detail["retry_after_s"].number ?? 3)))
            }
            if http.statusCode == 409, detail["code"].string == "guidance_stale" {
                throw BackendError.staleGuidance
            }
            if http.statusCode == 409, detail["code"].string == "handoff_unavailable" {
                throw BackendError.handoffUnavailable(detail["message"].string ?? "Start a new conversation")
            }
            if http.statusCode == 409, let code = detail["code"].string, let conflict = ComparisonConflict(rawValue:code) {
                throw BackendError.comparisonConflict(conflict,detail["message"].string ?? "Comparison needs attention")
            }
            throw BackendError.response(http.statusCode,detail.string ?? detail["message"].string ?? "Request rejected")
        }
        return json
    }
    private func recordConnection(path: String, started: Date, status: Int? = nil, error: NSError? = nil) {
        #if os(iOS)
        // Diagnose the transport boundary without retaining speech, payloads,
        // pairing headers, session identifiers or provider credentials.
        guard path.contains("local-voice") || path == "api/onboarding" else { return }
        var event: [String: JSONValue] = [
            "time": .string(ISO8601DateFormatter().string(from: Date())),
            "host": .string(base.host ?? ""),
            "endpoint": .string(path.split(separator: "/").last.map(String.init) ?? ""),
            "elapsed_s": .number(Date().timeIntervalSince(started))
        ]
        if let status { event["http_status"] = .number(Double(status)) }
        if let error {
            event["error_domain"] = .string(error.domain)
            event["error_code"] = .number(Double(error.code))
        }
        connectionEvents.append(.object(event))
        connectionEvents = Array(connectionEvents.suffix(50))
        let destination = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("ConnectionDiagnostics.json")
        try? JSONEncoder().encode(connectionEvents).write(to: destination, options: .atomic)
        #endif
    }
    func cameraComparison(sweep: String) async throws -> JSONValue {
        let path = "api/sweeps/\(sweep)/camera-comparison"
        let report: JSONValue
        do { report = try await call(path,body:.object([:]),timeout:300) }
        catch BackendError.comparisonConflict(.alreadyRunning, _) {
            // Another request owns the analysis. Read its persisted result; do not
            // turn a concurrent response or a running report into unavailability.
            report = try await call(path)
        }
        guard ["complete","failed"].contains(report["status"].string ?? "") else {
            throw BackendError.comparisonConflict(.alreadyRunning,"Camera comparison is still running. Retry finish to read its result.")
        }
        return report
    }
    @discardableResult
    func connect(sweep: String, startVoice: Bool = true) async throws -> UUID {
        try await connect(path:"api/sweeps/\(sweep)/live",startVoice:startVoice)
    }
    @discardableResult
    func connect(onboarding: String, startVoice: Bool = true) async throws -> UUID {
        try await connect(path:"api/onboarding/\(onboarding)/live",startVoice:startVoice)
    }
    private func connect(path: String, startVoice: Bool) async throws -> UUID {
        try Task.checkCancellation()
        socket?.cancel(with:.goingAway,reason:nil)
        socketGeneration = UUID()
        let generation = socketGeneration
        var components = URLComponents(url:base.appendingPathComponent(path),resolvingAgainstBaseURL:false)!
        components.scheme = base.scheme == "https" ? "wss" : "ws"
        var request = URLRequest(url:components.url!)
        request.setValue("Bearer " + token,forHTTPHeaderField:"Authorization")
        let task = session.webSocketTask(with:request); socket = task; task.resume()
        if startVoice { try await send(.object(["type":.string("start_voice")]),connection:generation) }
        guard socketGeneration == generation else { throw CancellationError() }
        return generation
    }
    func send(_ value: JSONValue, connection: UUID? = nil) async throws {
        try Task.checkCancellation()
        guard connection == nil || connection == socketGeneration else { throw CancellationError() }
        guard let socket else { throw BackendError.disconnected }
        let generation = socketGeneration
        let bytes = try JSONEncoder().encode(value)
        try await socket.send(.string(String(decoding:bytes,as:UTF8.self)))
        try Task.checkCancellation()
        guard generation == socketGeneration else { throw CancellationError() }
    }
    func receive(connection: UUID? = nil) async throws -> JSONValue {
        try Task.checkCancellation()
        guard connection == nil || connection == socketGeneration else { throw CancellationError() }
        guard let socket else { throw BackendError.disconnected }
        let generation = socketGeneration
        let message = try await socket.receive()
        try Task.checkCancellation()
        guard generation == socketGeneration else { throw CancellationError() }
        let data: Data
        switch message {
        case .data(let value): data = value
        case .string(let value): data = Data(value.utf8)
        @unknown default: throw BackendError.disconnected
        }
        return try JSONDecoder().decode(JSONValue.self,from:data)
    }
    func stopVoice(connection: UUID? = nil) async throws {
        try await send(.object(["type":.string("stop_voice")]),connection:connection)
    }
    func closeVoice(connection: UUID? = nil) {
        guard connection == nil || connection == socketGeneration else { return }
        socketGeneration = UUID()
        socket?.cancel(with:.normalClosure,reason:nil); socket = nil
    }
    func downloadPacket(sweep: String) async throws -> URL {
        let (file, response) = try await session.download(for: request("api/sweeps/\(sweep)/packet.zip"))
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw BackendError.disconnected }
        let destination = FileManager.default.temporaryDirectory.appendingPathComponent("claim_\(sweep).zip")
        if FileManager.default.fileExists(atPath: destination.path) { try FileManager.default.removeItem(at: destination) }
        try FileManager.default.moveItem(at: file, to: destination)
        return destination
    }
    func reviewURL(sweep: String) -> URL {
        var components = URLComponents(url: base, resolvingAgainstBaseURL: false)!
        // Fragment is consumed and cleared by the review page; never sent in HTTP logs.
        components.fragment = "token=\(token)&sweep=\(sweep)"
        return components.url!
    }
}
