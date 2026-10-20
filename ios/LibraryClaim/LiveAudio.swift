import AVFoundation

/// Bounded transport/device metadata only; never speech, PCM, or credentials.
final class RealtimeVoiceDiagnostics: @unchecked Sendable {
    static let shared = RealtimeVoiceDiagnostics()
    private let queue = DispatchQueue(label:"claim.voice.diagnostics")
    private var entries: [[String: String]] = []
    func record(_ event: String, _ fields: [String: String] = [:]) {
        queue.async {
            var row = fields
            row["event"] = event; row["time"] = ISO8601DateFormatter().string(from:Date())
            self.entries.append(row)
            self.entries = Array(self.entries.suffix(120))
            guard let directory = FileManager.default.urls(for:.documentDirectory,in:.userDomainMask).first,
                  let data = try? JSONSerialization.data(withJSONObject:self.entries,options:[.sortedKeys]) else { return }
            try? data.write(to:directory.appendingPathComponent("RealtimeVoiceDiagnostics.json"),options:.atomic)
        }
    }
}

/// Full-duplex PCM audio. Voice processing suppresses the loudspeaker echo while
/// leaving the microphone available for the owner's spoken interruption.
final class LiveAudio {
    private var engine = AVAudioEngine()
    private var player = AVAudioPlayerNode()
    private let queue = DispatchQueue(label:"claim.audio.realtime")
    private let queueKey = DispatchSpecificKey<Bool>()
    private var generation = UUID()
    private var drainScheduled = false
    private var started = false
    private var startupID: UUID?
    private var tapInstalled = false
    private var ledger = RealtimePlaybackLedger()
    private var onPlayed: ((JSONValue) -> Void)?
    private var onDrained: (() -> Void)?
    private var onFailure: ((Error) -> Void)?
    private var lifecycle = RealtimeAudioLifecycleGate()
    private var configurationObservers: [NSObjectProtocol] = []
    private var scheduledChunks = 0
    private var playedChunks = 0

    init() { queue.setSpecific(key:queueKey,value:true); engine.attach(player) }

    func start(onPCM: @escaping (Data) -> Void, onPlayed: @escaping (JSONValue) -> Void,
               onFailure: @escaping (Error) -> Void, onDrained: @escaping () -> Void) async throws {
        let attemptID = UUID()
        let audioGeneration = queue.sync {
            replaceAudioObjectsOnQueue()
            startupID = attemptID
            return lifecycle.beginStart()
        }
        do {
            try queue.sync {
                let session = AVAudioSession.sharedInstance()
                RealtimeVoiceDiagnostics.shared.record("audio_start")
                try session.setCategory(.playAndRecord,mode:.voiceChat,options:[.defaultToSpeaker,.allowBluetoothHFP])
                try session.setPreferredSampleRate(48000)
                try session.setPreferredIOBufferDuration(0.02)
                try session.setActive(true)
                try engine.inputNode.setVoiceProcessingEnabled(true)
            }
            // Voice processing changes the hardware format asynchronously. Do not
            // open a paid voice session until a graph using the settled format runs.
            try await Task.sleep(for:.milliseconds(200))
            for attempt in 1...4 {
                try Task.checkCancellation()
                let source: AVAudioFormat = try queue.sync {
                    guard startupID == attemptID else { throw CancellationError() }
                    player.stop(); engine.stop()
                    let input = engine.inputNode
                    if tapInstalled { input.removeTap(onBus:0); tapInstalled = false }
                    let source = input.outputFormat(forBus:0)
                    guard source.sampleRate > 0, source.channelCount > 0,
                          let target = AVAudioFormat(commonFormat:.pcmFormatInt16,sampleRate:16000,channels:1,interleaved:true),
                          let converter = AVAudioConverter(from:source,to:target) else { throw RealtimeVoiceError.microphoneUnavailable }
                    input.installTap(onBus:0,bufferSize:1024,format:source) { buffer, _ in
                        let capacity = AVAudioFrameCount(Double(buffer.frameLength)*16000/source.sampleRate)+32
                        guard let output = AVAudioPCMBuffer(pcmFormat:target,frameCapacity:capacity) else {
                            onFailure(RealtimeVoiceError.microphoneUnavailable); return
                        }
                        var supplied = false
                        var error: NSError?
                        converter.convert(to:output,error:&error) { _, status in
                            if supplied { status.pointee = .noDataNow; return nil }
                            supplied = true; status.pointee = .haveData; return buffer
                        }
                        if let error { onFailure(error); return }
                        guard output.frameLength > 0, let samples = output.int16ChannelData?[0] else { return }
                        onPCM(Data(bytes:samples,count:Int(output.frameLength)*2))
                    }
                    tapInstalled = true
                    let format = AVAudioFormat(standardFormatWithSampleRate:24000,channels:1)!
                    engine.disconnectNodeOutput(player)
                    engine.connect(player,to:engine.mainMixerNode,format:format)
                    player.volume = 1
                    engine.prepare(); try engine.start(); player.play()
                    return source
                }
                try await Task.sleep(for:.milliseconds(200))
                let ready = try queue.sync {
                    guard startupID == attemptID else { throw CancellationError() }
                    let current = engine.inputNode.outputFormat(forBus:0)
                    guard engine.isRunning, current == source else {
                        RealtimeVoiceDiagnostics.shared.record("audio_start_settling",["attempt":String(attempt)])
                        return false
                    }
                    let session = AVAudioSession.sharedInstance()
                    generation = UUID(); drainScheduled = false
                    ledger = RealtimePlaybackLedger(); self.onPlayed = onPlayed; self.onDrained = onDrained; self.onFailure = onFailure
                    scheduledChunks = 0; playedChunks = 0; started = true; startupID = nil
                    RealtimeVoiceDiagnostics.shared.record("audio_started",[
                        "input_rate":String(source.sampleRate),"input_channels":String(source.channelCount),
                        "output_rate":String(engine.outputNode.outputFormat(forBus:0).sampleRate),
                        "volume":String(session.outputVolume),
                        "outputs":session.currentRoute.outputs.map { $0.portType.rawValue }.joined(separator:","),
                        "engine_running":String(engine.isRunning),"player_playing":String(player.isPlaying)])
                    lifecycle.didStart(generation:audioGeneration)
                    observeConfiguration(engine:engine,format:source,route:routeIdentity(session),generation:audioGeneration)
                    return true
                }
                if ready { return }
            }
            throw RealtimeVoiceError.audioConfigurationChanged
        } catch {
            queue.sync {
                guard startupID == attemptID else { return }
                let failure = error as NSError
                RealtimeVoiceDiagnostics.shared.record("audio_start_failed",["domain":failure.domain,"code":String(failure.code)])
                stopOnQueue()
            }
            throw error
        }
    }

    func play(_ chunk: RealtimeAudioChunk) throws {
        try queue.sync {
            guard started else { throw RealtimeVoiceError.microphoneUnavailable }
            guard let ticket = try ledger.reserve(chunk) else { return }
            guard let format = AVAudioFormat(standardFormatWithSampleRate:24000,channels:1),
                  let buffer = AVAudioPCMBuffer(pcmFormat:format,frameCapacity:AVAudioFrameCount(chunk.frames)),
                  let channel = buffer.floatChannelData?[0] else { throw RealtimeVoiceError.invalidAudio }
            buffer.frameLength = buffer.frameCapacity
            chunk.pcm.withUnsafeBytes { bytes in
                for index in 0..<chunk.frames {
                    let sample = UInt16(bytes[index*2]) | UInt16(bytes[index*2+1]) << 8
                    channel[index] = Float(Int16(bitPattern:sample))/32768
                }
            }
            scheduledChunks += 1
            if scheduledChunks == 1 || scheduledChunks % 50 == 0 {
                RealtimeVoiceDiagnostics.shared.record("audio_scheduled",["chunks":String(scheduledChunks),
                    "peak":String((0..<chunk.frames).map { abs(channel[$0]) }.max() ?? 0),
                    "engine_running":String(engine.isRunning),"player_playing":String(player.isPlaying),
                    "volume":String(AVAudioSession.sharedInstance().outputVolume)])
            }
            player.scheduleBuffer(buffer,completionCallbackType:.dataPlayedBack) { [weak self] _ in
                self?.queue.async { [weak self] in
                    guard let self, self.started, self.engine.isRunning, self.player.isPlaying,
                          let receipt = self.ledger.complete(ticket) else { return }
                    self.playedChunks += 1
                    if self.playedChunks == 1 || self.playedChunks % 50 == 0 {
                        RealtimeVoiceDiagnostics.shared.record("audio_played",["chunks":String(self.playedChunks)])
                    }
                    self.onPlayed?(receipt)
                    self.notifyWhenDrained()
                }
            }
        }
    }

    func interrupt() {
        queue.sync {
            generation = UUID(); drainScheduled = false; ledger.interrupt()
            player.stop(); player.reset()
            if started { player.play() }
        }
    }

    // At most one UI notification is pending, even if very short chunks drain
    // faster than the main thread can run. Recheck the current playback epoch.
    private func notifyWhenDrained() {
        guard ledger.queuedFrames == 0, !drainScheduled else { return }
        drainScheduled = true
        let epoch = generation
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            let callback: (() -> Void)? = self.queue.sync {
                guard self.generation == epoch else { return nil }
                self.drainScheduled = false
                guard self.started, self.ledger.queuedFrames == 0 else { return nil }
                return self.onDrained
            }
            callback?()
        }
    }

    private func routeIdentity(_ session: AVAudioSession) -> [String] {
        let route = session.currentRoute
        return route.inputs.map { "input:" + $0.portType.rawValue + ":" + $0.uid }
            + route.outputs.map { "output:" + $0.portType.rawValue + ":" + $0.uid }
    }

    private func observeConfiguration(engine observedEngine: AVAudioEngine, format: AVAudioFormat, route: [String], generation: UUID) {
        let center = NotificationCenter.default
        // Observe after our graph is running. The captured engine and generation
        // reject notifications queued before a stop/reconnect. A delayed startup
        // notification with a healthy, unchanged graph is harmless.
        for (name, object) in [(Notification.Name.AVAudioEngineConfigurationChange, observedEngine as AnyObject),
                               (AVAudioSession.routeChangeNotification, AVAudioSession.sharedInstance() as AnyObject)] {
            configurationObservers.append(center.addObserver(forName:name,object:object,queue:nil) { [weak self, weak observedEngine] _ in
                self?.queue.async { [weak self, weak observedEngine] in
                    guard let self, let observedEngine, self.engine === observedEngine else { return }
                    let running = observedEngine.isRunning
                    let current = running ? observedEngine.inputNode.outputFormat(forBus:0) : nil
                    let matches = current.map { $0.sampleRate == format.sampleRate && $0.channelCount == format.channelCount
                        && $0.commonFormat == format.commonFormat && $0.isInterleaved == format.isInterleaved } ?? false
                    guard self.lifecycle.configurationChanged(generation:generation,engineRunning:running,
                        formatMatches:matches,routeMatches:self.routeIdentity(AVAudioSession.sharedInstance()) == route) else { return }
                    RealtimeVoiceDiagnostics.shared.record("audio_configuration_changed",[
                        "engine_running":String(running),"format_matches":String(matches),
                        "route_matches":String(self.routeIdentity(AVAudioSession.sharedInstance()) == route)])
                    let failure = self.onFailure
                    self.stopOnQueue()
                    failure?(RealtimeVoiceError.audioConfigurationChanged)
                }
            })
        }
    }

    private func removeConfigurationObservers() {
        configurationObservers.forEach(NotificationCenter.default.removeObserver)
        configurationObservers.removeAll()
    }

    private func replaceAudioObjectsOnQueue() {
        lifecycle.stop(); removeConfigurationObservers()
        generation = UUID(); drainScheduled = false; ledger.interrupt()
        onPlayed = nil; onDrained = nil; onFailure = nil
        tapInstalled = false; started = false; startupID = nil
        let freshEngine = AVAudioEngine(), freshPlayer = AVAudioPlayerNode()
        freshEngine.attach(freshPlayer)
        engine = freshEngine; player = freshPlayer
    }

    func mediaServicesWereReset() {
        queue.sync {
            let failure = onFailure
            // The old graph is orphaned. Dispose it without stopping/removing
            // taps through the reset audio service, then use fresh objects.
            replaceAudioObjectsOnQueue()
            failure?(RealtimeVoiceError.audioServicesReset)
        }
    }

    private func stopOnQueue() {
        startupID = nil
        if started { RealtimeVoiceDiagnostics.shared.record("audio_stopped",["scheduled":String(scheduledChunks),"played":String(playedChunks)]) }
        lifecycle.stop(); removeConfigurationObservers()
        generation = UUID(); drainScheduled = false; ledger.interrupt(); onPlayed = nil; onDrained = nil; onFailure = nil
        if tapInstalled { engine.inputNode.removeTap(onBus:0); tapInstalled = false }
        player.stop(); engine.stop(); started = false
        try? AVAudioSession.sharedInstance().setActive(false,options:.notifyOthersOnDeactivation)
    }
    func stop() {
        if DispatchQueue.getSpecific(key:queueKey) == true { stopOnQueue() }
        else { queue.sync { stopOnQueue() } }
    }
    deinit { stop() }
}
