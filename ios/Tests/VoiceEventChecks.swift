import Foundation

@main struct VoiceEventChecks {
    static func main() async throws {
        let client = try BackendClient(address:CommandLine.arguments[1],token:"test")
        try await client.connect(sweep:"fixture")
        let ready = try await client.receive()
        precondition(ready["type"].string == "voice_ready")
        try await client.stopVoice()
        let stopped = try await client.receive()
        precondition(stopped["type"].string == "voice_stopped")
        let overlay = try await client.receive()
        precondition(overlay["type"].string == "frame_overlays")
        let inventory = try await client.receive()
        precondition(inventory["type"].string == "inventory")
        await client.closeVoice()
        print("Voice stop keeps the same socket open for camera overlays and inventory")
    }
}
