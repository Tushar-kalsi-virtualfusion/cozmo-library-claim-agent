import Foundation

@main struct RealtimeSocketChecks {
    static func main() async throws {
        guard CommandLine.arguments.count == 2 else { fatalError("Pass the local fixture URL") }
        let client = try BackendClient(address:CommandLine.arguments[1],token:"realtime-test")
        let old = try await client.connect(onboarding:"first",startVoice:false)
        try await client.send(.object(["type":.string("probe")]),connection:old)
        let first = try await client.receive(connection:old)
        precondition(first["path"].string == "/api/onboarding/first/live")
        let waiting = Task { try await client.receive(connection:old) }
        let current = try await client.connect(sweep:"second",startVoice:false)
        do { _ = try await waiting.value; fatalError("Old receiver consumed a new connection event") }
        catch is CancellationError {}
        catch let error as URLError { precondition([.cancelled,.networkConnectionLost].contains(error.code)) }
        do { try await client.send(.object(["type":.string("old_audio")]),connection:old); fatalError("Old audio reached the new socket") }
        catch is CancellationError {}
        do { _ = try await client.receive(connection:old); fatalError("Old receiver resumed on the new socket") }
        catch is CancellationError {}
        await client.closeVoice(connection:old)
        try await client.send(.object(["type":.string("probe")]),connection:current)
        let second = try await client.receive(connection:current)
        precondition(second["path"].string == "/api/sweeps/second/live", "An old close must not close the current socket")
        try await client.stopVoice(connection:current)
        let stopped = try await client.receive(connection:current)
        precondition(stopped["type"].string == "stopped")
        await client.closeVoice(connection:current)
        print("Realtime socket replacement, stale callbacks, scoped close and stop checks passed")
    }
}
