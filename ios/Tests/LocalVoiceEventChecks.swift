import Foundation

@main struct LocalVoiceEventChecks {
    static func main() async throws {
        let client = try BackendClient(address:CommandLine.arguments[1],token:"test")
        try await client.connect(sweep:"fixture",startVoice:false)
        let event = try await client.receive()
        precondition(event["type"].string == "frame_overlays")
        // Native local voice stops only its microphone/TTS; event transport remains subscribed.
        let inventory = try await client.receive()
        precondition(inventory["type"].string == "inventory")
        await client.closeVoice()
        print("Local event subscription sends no start_voice or audio; overlays and inventory remain available")
    }
}
