import AVFoundation
import Foundation

@main struct CloudSpeechRecorderChecks {
    static func main() throws {
        // Exercise the real converter without opening the microphone or network.
        let format = AVAudioFormat(commonFormat:.pcmFormatFloat32,sampleRate:48000,channels:2,interleaved:false)!
        let recorder = try CloudSpeechRecorder(inputFormat:format)
        var recorded: Data?
        for frame in 0..<100 {
            let buffer = AVAudioPCMBuffer(pcmFormat:format,frameCapacity:960)!
            buffer.frameLength = 960
            for channel in 0..<2 {
                for sample in 0..<960 {
                    buffer.floatChannelData![channel][sample] = frame < 25 ? 0.04 * sin(Float(sample) * 2 * .pi * 200 / 48000) : 0
                }
            }
            if case .utterance(let wav) = recorder.consume(buffer) { recorded = wav; break }
        }
        guard let recorded else { fatalError("48 kHz stereo conversion lost the utterance") }
        precondition(CloudSpeechWAV.validate(recorded,requiredRate:16000,maxSeconds:30))
        let cancelled = try CloudSpeechRecorder(inputFormat:format)
        cancelled.cancel()
        let buffer = AVAudioPCMBuffer(pcmFormat:format,frameCapacity:960)!
        buffer.frameLength = 960
        for channel in 0..<2 {
            for sample in 0..<960 { buffer.floatChannelData![channel][sample] = 0.1 }
        }
        precondition(cancelled.consume(buffer) == nil, "Cancelled microphone callbacks must be discarded")
        print("Cloud speech real audio conversion and recorder cancellation checks passed")
    }
}
