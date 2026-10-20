import Foundation

@main struct CloudSpeechChecks {
    static func main() throws {
        // Removing the energy/minimum-duration gate would send silence or a short click.
        var silence = CloudUtteranceBuffer()
        precondition(feed(&silence, amplitude:0, seconds:30) == .discarded)
        var click = CloudUtteranceBuffer()
        _ = feed(&click, amplitude:0.1, seconds:0.08)
        precondition(feed(&click, amplitude:0, seconds:1.2) == .discarded)
        var noise = CloudUtteranceBuffer()
        precondition(feed(&noise, amplitude:0.001, seconds:30) == .discarded)
        var buzz = CloudUtteranceBuffer()
        let highFrequency = (0..<320).map { Float($0 % 2 == 0 ? 0.04 : -0.04) }
        for _ in 0..<1499 { precondition(buzz.append(highFrequency) == .collecting) }
        precondition(buzz.append(highFrequency) == .discarded)

        // A real utterance retains its leading context and submits after a pause.
        var quick = CloudUtteranceBuffer()
        _ = feed(&quick,amplitude:0.04,seconds:0.4)
        precondition(feed(&quick,amplitude:0,seconds:0.6) == .collecting,
                     "A short mid-sentence pause must remain in the same utterance")
        _ = feed(&quick,amplitude:0.04,seconds:0.3)
        guard case .utterance = feed(&quick,amplitude:0,seconds:0.7) else {
            fatalError("A completed phrase must submit within 700 ms of silence")
        }
        var speech = CloudUtteranceBuffer()
        _ = feed(&speech, amplitude:0, seconds:0.2)
        _ = feed(&speech, amplitude:0.04, seconds:0.4)
        guard case .utterance(let wav) = feed(&speech, amplitude:0, seconds:1.2) else { fatalError("Speech did not finalize") }
        precondition(String(decoding:wav.prefix(4),as:UTF8.self) == "RIFF")
        precondition(String(decoding:wav[8..<12],as:UTF8.self) == "WAVE")
        precondition(Array(wav[22..<24]) == [1,0], "WAV must be mono")
        precondition(Array(wav[24..<28]) == [128,62,0,0], "WAV must use 16 kHz")
        precondition(Array(wav[34..<36]) == [16,0], "WAV must use PCM16")
        precondition(wav.count >= 44 + 16000 * 2 / 2, "Speech onset/pre-roll was lost")
        precondition(wav[44..<100].allSatisfy { $0 == 0 }, "Pre-roll must preserve the quiet lead-in")
        precondition(speech.append([Float](repeating:0.04,count:320)) == .collecting, "Finalized input cannot submit twice")
        var long = CloudUtteranceBuffer()
        guard case .utterance(let bounded) = feed(&long, amplitude:0.04, seconds:31) else { fatalError("Long speech did not finalize") }
        precondition(bounded.count == 960044, "A continuous utterance must never exceed 30 seconds")
        let pcm = CloudSpeechWAV.encode(samples:[-2,-1,0,1,2,.nan,.infinity])
        precondition(Array(pcm.suffix(14)) == [1,128,1,128,0,0,255,127,255,127,0,0,0,0])

        // Cloud language selection does not depend on installed Apple speech packs.
        precondition(CloudSpeechLanguage.code("Hindi") == "hi")
        precondition(CloudSpeechLanguage.code("हिन्दी",explicitLocale:"hi-IN") == "hi")
        precondition(CloudSpeechLanguage.code("Japanese") == "ja")
        precondition(CloudSpeechLanguage.code("Arabic",explicitLocale:"ar-SA") == "ar")
        precondition(CloudSpeechLanguage.code("unknown-language") == nil)

        // A stale, cancelled, failed, or duplicate player completion cannot open setup gates.
        let generation = UUID(), old = UUID(), playback = UUID()
        var receipt = CloudSpeechPlaybackReceipt()
        receipt.begin(id:playback,generation:generation,prompt:"readback")
        precondition(receipt.complete(id:playback,generation:old,success:true) == nil)
        precondition(receipt.complete(id:playback,generation:generation,success:false) == nil)
        precondition(receipt.complete(id:playback,generation:generation,success:true) == nil)
        receipt.begin(id:playback,generation:generation,prompt:"readiness")
        receipt.cancel()
        precondition(receipt.complete(id:playback,generation:generation,success:true) == nil)
        receipt.begin(id:playback,generation:generation,prompt:"readback")
        precondition(receipt.complete(id:playback,generation:generation,success:true)?.prompt == "readback")
        precondition(receipt.complete(id:playback,generation:generation,success:true) == nil)
        print("Cloud speech silence, bounds, WAV, language and playback cancellation checks passed")
    }
    static func feed(_ buffer: inout CloudUtteranceBuffer, amplitude: Float, seconds: Double) -> CloudUtteranceBuffer.Event {
        let samples = (0..<320).map { amplitude * sin(Float($0) * 2 * .pi * 200 / 16000) }
        for _ in 0..<Int(seconds / 0.02) {
            let result = buffer.append(samples)
            if result != .collecting { return result }
        }
        return .collecting
    }
}
