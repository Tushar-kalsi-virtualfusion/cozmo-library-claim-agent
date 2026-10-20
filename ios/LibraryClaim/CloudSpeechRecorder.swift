import AVFoundation
import Foundation

/// Confined by a lock because the engine tap and Stop run on different queues.
/// Audio conversion and utterance storage stay off the main actor/camera path.
final class CloudSpeechRecorder: @unchecked Sendable {
    enum Event { case speechStarted, utterance(Data), discarded, failure(String) }
    private let lock = NSLock()
    private let converter: AVAudioConverter
    private let outputFormat: AVAudioFormat
    private var utterance = CloudUtteranceBuffer()
    private var cancelled = false

    init(inputFormat: AVAudioFormat) throws {
        guard inputFormat.sampleRate > 0, inputFormat.channelCount > 0,
              let output = AVAudioFormat(commonFormat:.pcmFormatFloat32,sampleRate:16000,channels:1,interleaved:false),
              let converter = AVAudioConverter(from:inputFormat,to:output) else { throw CloudSpeechError.invalidAudio }
        self.outputFormat = output; self.converter = converter
    }

    var isHearingSpeech: Bool {
        lock.lock(); defer { lock.unlock() }
        return !cancelled && utterance.hasSpeech
    }

    func cancel() {
        lock.lock(); defer { lock.unlock() }
        cancelled = true; utterance = CloudUtteranceBuffer()
    }

    func consume(_ input: AVAudioPCMBuffer) -> Event? {
        lock.lock(); defer { lock.unlock() }
        guard !cancelled else { return nil }
        let capacity = AVAudioFrameCount(ceil(Double(input.frameLength) * 16000 / input.format.sampleRate) + 64)
        guard let output = AVAudioPCMBuffer(pcmFormat:outputFormat,frameCapacity:capacity) else {
            cancelled = true; return .failure("Microphone audio conversion could not allocate a buffer.")
        }
        var supplied = false
        var error: NSError?
        let status = converter.convert(to:output,error:&error) { _, state in
            guard !supplied else { state.pointee = .noDataNow; return nil }
            supplied = true; state.pointee = .haveData; return input
        }
        if status == .error {
            cancelled = true
            return .failure(error?.localizedDescription ?? "Microphone audio conversion failed.")
        }
        guard output.frameLength > 0, let channel = output.floatChannelData?[0] else { return nil }
        let previouslySpeaking = utterance.hasSpeech
        switch utterance.append(Array(UnsafeBufferPointer(start:channel,count:Int(output.frameLength)))) {
        case .collecting: return !previouslySpeaking && utterance.hasSpeech ? .speechStarted : nil
        case .discarded: cancelled = true; return .discarded
        case .utterance(let data): cancelled = true; return .utterance(data)
        }
    }
}
