import Foundation

@main struct RealtimeVoiceChecks {
    static func chunk(_ id: String = "answer", end: Double = 100, bytes: Int = 4800) throws -> RealtimeAudioChunk {
        try RealtimeAudioChunk(.object(["type":.string("audio"),"pcm_base64":.string(Data(repeating:1,count:bytes).base64EncodedString()),
            "sample_rate":.number(24000),"item_id":.string(id),"content_index":.number(0),"audio_end_ms":.number(end)]))
    }
    static func main() async throws {
        let ready: JSONValue = .object(["voice_transport":.string("realtime"),"assistant_provider":.string("openai"),"realtime_ready":.bool(true)])
        precondition(RealtimeVoiceCapability.available(ready))
        precondition(!RealtimeVoiceCapability.available(.object(["voice_transport":.string("local"),"assistant_provider":.string("anthropic"),"realtime_ready":.bool(true)])))
        precondition(!RealtimeVoiceCapability.available(.object(["voice_transport":.string("realtime"),"assistant_provider":.string("openai"),"realtime_ready":.bool(false)])))

        var ledger = RealtimePlaybackLedger()
        let first = try chunk(), second = try chunk(end:200)
        let a = try ledger.reserve(first)!, b = try ledger.reserve(second)!
        precondition(ledger.queuedFrames == 4800)
        let receipt = ledger.complete(a)
        precondition(receipt?["item_id"].string == "answer" && receipt?["played_ms"].number == 100)
        precondition(ledger.complete(a) == nil, "A completion must never be acknowledged twice")
        ledger.interrupt()
        precondition(ledger.complete(b) == nil, "Interrupted playback is not delivered speech")
        let late = try ledger.reserve(try chunk(end:300))
        precondition(late == nil, "Late chunks from interrupted items must be ignored")
        let fresh = try ledger.reserve(try chunk("next"))!
        precondition(ledger.complete(fresh)?["played_ms"].number == 100)
        for bad in [try? chunk(bytes:3), try? chunk(end:0), try? chunk(bytes:0)] { precondition(bad == nil) }
        var long = RealtimePlaybackLedger()
        for index in 1...150 { _ = try long.reserve(try chunk("long",end:Double(index)*100)) }
        precondition(long.queuedFrames == 360000, "Speech longer than 12 seconds must play in full")
        do {
            for index in 151...1201 { _ = try long.reserve(try chunk("long",end:Double(index)*100)) }
            fatalError("Unbounded playback backlog accepted")
        } catch RealtimeVoiceError.playbackBacklog {}

        let outbox = RealtimeEventOutbox(capacity:3)
        outbox.offerPCM(Data([1,0])) // Until voice_ready no microphone leaves the phone.
        outbox.enableMicrophone()
        outbox.offerPCM(Data([2,0]))
        outbox.offer(.object(["type":.string("playback"),"played_ms":.number(100)]))
        outbox.offer(.object(["type":.string("interrupt_voice")]))
        var iterator = outbox.events.makeAsyncIterator()
        let microphone = try await iterator.next(), played = try await iterator.next(), interrupt = try await iterator.next()
        precondition(microphone?["pcm_base64"].string == Data([2,0]).base64EncodedString())
        precondition(played?["type"].string == "playback" && interrupt?["type"].string == "interrupt_voice", "A played receipt must precede its interruption")
        outbox.finish()
        outbox.offerPCM(Data([3,0]))
        let closed = try await iterator.next()
        precondition(closed == nil, "Closed connection callbacks must be inert")
        let overloaded = RealtimeEventOutbox(capacity:2)
        overloaded.enableMicrophone()
        for _ in 0..<3 { overloaded.offerPCM(Data([1,0])) }
        do { try overloaded.checkOpen(); fatalError("Microphone backlog silently discarded") }
        catch RealtimeVoiceError.microphoneBacklog {}
        print("Realtime capability, complete playback, cancellation and bounded outbound checks passed")
    }
}
