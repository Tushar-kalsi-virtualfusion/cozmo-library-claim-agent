import Foundation
import AVFoundation
import Speech
import UIKit

/// The phone owns bounded microphone capture/playback; the paired server owns speech
/// provider credentials and Claude reasoning. Local speech is explicitly labeled recovery.
@MainActor final class LocalVoiceAgent: NSObject, AVSpeechSynthesizerDelegate, AVAudioPlayerDelegate {
    var onStatus: ((String, Bool) -> Void)?
    var onTranscript: ((String, String) -> Void)?
    var onGuidance: ((String) -> Void)?
    private let engine = AVAudioEngine()
    private let speaker = AVSpeechSynthesizer()
    private var recognizer: SFSpeechRecognizer?
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var recognition: SFSpeechRecognitionTask?
    private var tapInstalled = false
    private var active = false
    private var epoch = UUID()
    private var listenEpoch = UUID()
    private var silenceTask: Task<Void,Never>?
    private var responseTask: Task<Void,Never>?
    private var responseInFlight = false
    private var speechTask: Task<Void,Never>?
    private var cloudRecorder: CloudSpeechRecorder?
    private var cloudPlayer: AVAudioPlayer?
    private var cloudPlaybackID: UUID?
    private var cloudSpeechCompletion: CheckedContinuation<Void, Error>?
    private var cloudPlaybackReceipt = CloudSpeechPlaybackReceipt()
    private var cloudSpeech = false
    private var speechProvider = "local"
    private var transcriptionLanguage: String?
    private var cloudHasSpeech = false
    private var lifecycleObservers: [NSObjectProtocol] = []
    private var guidanceTask: Task<Void,Never>?
    private var guidanceGate = CloudGuidanceGate()
    private var guidanceActivity = UUID()
    private var toolExecutor: LocalVoiceToolExecutor?
    private var lastPartial = ""
    private var localeID = Locale.current.identifier
    private var client: BackendClient?
    private var path = ""
    private var configuration: JSONValue = .null
    private var setup = LocalSetupGate()
    private var setupRequestSent = false
    private var lastAssistant = ""
    private var spokenLanguage = "English"
    private var isOnboarding = true
    private var pendingSetupPrompt: String?
    private var deliveredSetupPrompt: (utterance: AVSpeechUtterance, kind: String)?
    private var currentSpeech: (utterance: AVSpeechUtterance, epoch: UUID)?
    private static let savedLocaleKey = "LocalSpeech.selectedLocale"

    override init() {
        super.init()
        speaker.delegate = self
        speaker.usesApplicationAudioSession = true
        lifecycleObservers = [
            NotificationCenter.default.addObserver(forName:UIApplication.didEnterBackgroundNotification,object:nil,queue:.main) { [weak self] _ in
                Task { @MainActor in
                    guard let self, self.active else { return }
                    self.fail("Voice paused while the app is in the background. Reconnect voice to continue.")
                }
            },
            NotificationCenter.default.addObserver(forName:AVAudioSession.interruptionNotification,object:nil,queue:.main) { [weak self] note in
                let began = (note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt) == AVAudioSession.InterruptionType.began.rawValue
                guard began else { return }
                Task { @MainActor in
                    guard let self, self.active else { return }
                    self.fail("Voice audio was interrupted. Reconnect voice to continue.")
                }
            }
        ]
    }

    deinit { lifecycleObservers.forEach { NotificationCenter.default.removeObserver($0) } }

    static func saveCapabilitySnapshot(locale selectedLocale: String? = nil,
                                       speechLocale requestedSpeechLocale: String? = nil,
                                       voice selectedVoice: AVSpeechSynthesisVoice? = nil,
                                       phase: String = "idle", provider: String = "local") {
        let locale = LocalSpeechSelection.canonicalLocale(selectedLocale
            ?? UserDefaults.standard.string(forKey:savedLocaleKey) ?? AVSpeechSynthesisVoice.currentLanguageCode())
        let recognizer = provider == "local" ? SFSpeechRecognizer(locale:Locale(identifier:locale)) : nil
        let voices = provider == "local" ? AVSpeechSynthesisVoice.speechVoices() : []
        let speechLocale = requestedSpeechLocale ?? locale
        let ranked = LocalSpeechSelection.rankedVoices(locale:speechLocale,voices:voices.map(voiceDescriptor))
        let selected = selectedVoice.map(voiceDescriptor) ?? ranked.first
        let audio = AVAudioSession.sharedInstance()
        func voiceJSON(_ voice: LocalSpeechVoice) -> JSONValue {
            .object(["identifier":.string(voice.identifier),"locale":.string(voice.locale),
                     "quality":.string(voice.quality.description),"novelty":.bool(voice.isNovelty)])
        }
        let snapshot: JSONValue = .object([
            "checked_at":.string(ISO8601DateFormatter().string(from:Date())),
            "assistant_provider":.string("claude"),"reasoning_location":.string("paired_server"),
            "cloud_text_enabled":.bool(true),"cloud_voice_enabled":.bool(provider != "local"),
            "speech_provider":.string(provider),
            "speech_locale":.string(locale),
            "speech_on_device_supported":.bool(recognizer?.supportsOnDeviceRecognition ?? false),
            "speech_available":.bool(recognizer?.isAvailable ?? false),
            "speech_authorization":.number(Double(SFSpeechRecognizer.authorizationStatus().rawValue)),
            "speech_phase":.string(phase),
            "tts_requested_locale":.string(speechLocale),
            "tts_selected_voice":selected.map(voiceJSON) ?? .null,
            "tts_available_correct_language_voices":.array(ranked.map(voiceJSON)),
            "tts_available_voices":.array(voices.map(voiceDescriptor).sorted { $0.identifier < $1.identifier }.map(voiceJSON)),
            "tts_utterance_volume":.number(1),
            "audio_category":.string(audio.category.rawValue),"audio_mode":.string(audio.mode.rawValue),
            // Port types avoid recording personalized headset names or device identifiers.
            "audio_output_routes":.array(audio.currentRoute.outputs.map { .string($0.portType.rawValue) }),
            "audio_output_volume":.number(Double(audio.outputVolume))])
        let root = FileManager.default.urls(for:.documentDirectory,in:.userDomainMask)[0]
        try? JSONEncoder().encode(snapshot).write(to:root.appendingPathComponent("LocalCapabilities.json"),options:.atomic)
    }

    private static func voiceDescriptor(_ voice: AVSpeechSynthesisVoice) -> LocalSpeechVoice {
        let quality: LocalSpeechVoiceQuality
        switch voice.quality {
        case .premium: quality = .premium
        case .enhanced: quality = .enhanced
        default: quality = .standard
        }
        return LocalSpeechVoice(identifier:voice.identifier,locale:voice.language,quality:quality,
                                isNovelty:voice.voiceTraits.contains(.isNoveltyVoice))
    }

    func start(client: BackendClient, path: String, onboarding: Bool) async throws {
        stop()
        let generation = epoch
        let microphone = await withCheckedContinuation { continuation in
            AVAudioApplication.requestRecordPermission { continuation.resume(returning:$0) }
        }
        guard !Task.isCancelled, generation == epoch else { throw CancellationError() }
        guard microphone else { throw LocalIntelligenceError.unavailable("Enable Microphone access to talk to the assistant.") }
        if onboarding, self.path != path { spokenLanguage = "English"; localeID = Locale.current.identifier }
        self.client = client; self.path = path; self.isOnboarding = onboarding
        let initialConfiguration = try await client.call(path)
        guard !Task.isCancelled, generation == epoch else { throw CancellationError() }
        configuration = initialConfiguration
        speechProvider = configuration["speech"]["provider"].string ?? "local"
        guard ["local","openai","elevenlabs"].contains(speechProvider) else {
            throw LocalIntelligenceError.unavailable("The server returned an unsupported speech provider.")
        }
        cloudSpeech = speechProvider != "local"
        if cloudSpeech {
            guard configuration["speech"]["ready"] == .bool(true) else {
                throw LocalIntelligenceError.unavailable(configuration["speech"]["error"].string
                    ?? "Cloud speech is not configured on the paired server. Reconnect voice after configuration.")
            }
        } else {
            let permission = await withCheckedContinuation { continuation in
                SFSpeechRecognizer.requestAuthorization { continuation.resume(returning:$0) }
            }
            guard !Task.isCancelled, generation == epoch else { throw CancellationError() }
            guard permission == .authorized else {
                throw LocalIntelligenceError.unavailable("Enable Speech Recognition in Settings to use local recovery voice.")
            }
        }
        setup = LocalSetupGate()
        setup.seedCaptureMode(configuration["context"]["capture_mode"].string ?? "")
        setup.restore(configuration["context"]["preferences"])
        // Restore the saved language before choosing either recognition or playback.
        let savedLanguage = configuration["context"]["preferences"]["language"].string?.trimmingCharacters(in:.whitespacesAndNewlines)
        if let savedLanguage, !savedLanguage.isEmpty { try selectLanguage(savedLanguage) }
        else {
            try selectLanguage(spokenLanguage)
            if cloudSpeech { transcriptionLanguage = nil } // Detect the first spoken language automatically.
        }
        active = true; responseInFlight = true
        Self.saveCapabilitySnapshot(locale:localeID,phase:"connecting",provider:speechProvider)
        let greeting: String
        if onboarding {
            if setup.phase == "readiness_prompt" { pendingSetupPrompt = "readiness" }
            greeting = try await localized(setup.phase == "readiness_prompt" ? "Your setup is confirmed. Are you ready to begin capture?" : "Hello. Which language would you like to speak?")
        } else {
            greeting = try await localized("Capture is ready. Move slowly across the room and keep each object fully in view. You can ask me about the inventory or make a correction.")
        }
        try checkCurrent(generation)
        responseInFlight = false
        onStatus?(cloudSpeech ? "Claude connected · cloud speech" : "Claude connected · local recovery voice",true)
        speak(greeting)
    }

    func stop() {
        active = false; epoch = UUID(); setupRequestSent = false
        guidanceTask?.cancel(); guidanceTask = nil
        guidanceGate = CloudGuidanceGate(); guidanceActivity = UUID()
        silenceTask?.cancel(); responseTask?.cancel(); responseTask = nil; responseInFlight = false
        speechTask?.cancel(); speechTask = nil
        let completion = cloudSpeechCompletion; cloudSpeechCompletion = nil
        completion?.resume(throwing:CancellationError())
        cloudPlaybackReceipt.cancel(); cloudPlaybackID = nil
        cloudPlayer?.stop(); cloudPlayer?.delegate = nil; cloudPlayer = nil
        toolExecutor?.invalidate()
        toolExecutor = nil; pendingSetupPrompt = nil; deliveredSetupPrompt = nil; currentSpeech = nil
        stopListening(); speaker.stopSpeaking(at:.immediate)
        try? AVAudioSession.sharedInstance().setActive(false,options:.notifyOthersOnDeactivation)
    }

    /// A user tap is an unambiguous interrupt signal, unlike microphone echo.
    /// Keep the setup gate and conversation, but invalidate all prior turn work.
    func interruptAndListen() {
        guard active else { return }
        let reconcile = isOnboarding && setup.interruptPendingAction(requestWasSent:setupRequestSent)
        let previousResponse = responseTask
        stop()
        active = true
        guard reconcile, let client else { listen(); return }
        responseInFlight = true
        onStatus?("Checking confirmed setup after interruption",true)
        let generation = epoch, target = path
        responseTask = Task { [weak self] in
            // Settle the cancelled transport before asking what the server accepted.
            await previousResponse?.value
            guard let self else { return }
            do {
                try self.checkCurrent(generation)
                let refreshed = try await client.call(target)
                try self.checkCurrent(generation)
                self.configuration = refreshed
                guard self.setup.reconcileAfterInterruption(refreshed["context"]) else {
                    throw LocalIntelligenceError.unavailable("The server returned incomplete setup state. Reconnect voice to check again.")
                }
                if refreshed["context"]["capture_requested"] == .bool(true) {
                    self.stop()
                    self.onStatus?("Capture is already requested. Waiting for the sweep to start.",false)
                    return
                }
                if let language = refreshed["context"]["preferences"]["language"].string, !language.isEmpty {
                    try self.selectLanguage(language)
                }
                self.responseInFlight = false
                self.listen() // The next answer still requires any undelivered prompt.
            } catch {
                guard !Task.isCancelled, self.active, self.epoch == generation else { return }
                self.fail(error.localizedDescription)
            }
        }
    }

    private var idleForGuidance: Bool {
        active && !isOnboarding && engine.isRunning && !speaker.isSpeaking
            && cloudPlayer == nil && speechTask == nil && !responseInFlight
            && !cloudHasSpeech && !(cloudRecorder?.isHearingSpeech ?? false) && lastPartial.isEmpty
    }

    private func cancelGuidanceForOwnerSpeech() {
        guidanceTask?.cancel(); guidanceTask = nil
        guidanceGate.cancelPending()
        // connected_speech observes the cancelled HTTP connection and releases
        // provider capacity before the utterance's silence window submits STT.
    }

    private func startGuidancePolling() {
        // Quiet recorder rollovers must not cancel a paid request or strand its
        // admission slot. Owner speech explicitly cancels and clears the task.
        guard active, !isOnboarding, guidanceTask == nil, let client else { return }
        let generation = epoch, target = path
        guidanceTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for:.seconds(8))
                guard !Task.isCancelled, let self, self.active, self.epoch == generation else { return }
                guard let request = self.guidanceGate.begin(now:ProcessInfo.processInfo.systemUptime,
                    idle:self.idleForGuidance,activity:self.guidanceActivity) else { continue }
                do {
                    let cue = try await client.guidance(target,requestID:request)
                    try self.checkCurrent(generation)
                    let shouldSpeak = self.guidanceGate.complete(request:request,activity:self.guidanceActivity,
                        idle:self.idleForGuidance,eventID:cue.eventID)
                    if shouldSpeak, !cue.reply.isEmpty {
                        if self.cloudSpeech {
                            // A proactive TTS request must not take the microphone
                            // away while the owner is still free to start talking.
                            let activity = self.guidanceActivity
                            let speech = try CloudSynthesisRequest(text:cue.reply,language:self.spokenLanguage,guidanceEventID:cue.eventID)
                            let audio = try await client.synthesize(target,request:speech)
                            try self.checkCurrent(generation)
                            guard activity == self.guidanceActivity, self.idleForGuidance else { continue }
                            try self.playCloudAudio(audio,text:cue.reply,prompt:nil,generation:generation,announce:true)
                        } else { self.speak(cue.reply) }
                    }
                } catch BackendError.staleGuidance {
                    guard !Task.isCancelled, self.active, self.epoch == generation else { return }
                    self.guidanceGate.cancelPending()
                    if !self.engine.isRunning { self.listen() }
                    continue // New evidence superseded this cue; keep listening and poll for a new event.
                } catch {
                    guard !Task.isCancelled, self.active, self.epoch == generation else { return }
                    self.guidanceGate.pause()
                    self.onTranscript?("system","Automatic guidance paused: \(error.localizedDescription)")
                    if !self.engine.isRunning { self.listen() }
                    return // No automatic retry after an uncertain paid outcome.
                }
            }
        }
    }

    func sayGuidance(_ text: String) {
        guard active, !speaker.isSpeaking, cloudPlayer == nil, speechTask == nil,
              !responseInFlight, !cloudHasSpeech, !(cloudRecorder?.isHearingSpeech ?? false), lastPartial.isEmpty else { return }
        // Native capture guidance is English; do not pronounce it with a Hindi (etc.) voice.
        speak(text,locale:cloudSpeech ? "en" : englishSpeechLocale())
    }

    private func selectLanguage(_ language: String, explicitLocale: String? = nil) throws {
        if cloudSpeech {
            spokenLanguage = language
            transcriptionLanguage = CloudSpeechLanguage.code(language,explicitLocale:explicitLocale)
            localeID = explicitLocale ?? transcriptionLanguage ?? language
            UserDefaults.standard.set(localeID,forKey:Self.savedLocaleKey)
            return
        }
        let preferred = [UserDefaults.standard.string(forKey:Self.savedLocaleKey), localeID,
                         Locale.current.identifier].compactMap { $0 } + Locale.preferredLanguages
        guard let locale = LocalSpeechSelection.recognitionLocale(language:language,explicitLocale:explicitLocale,
                preferredLocales:preferred,supportedLocales:SFSpeechRecognizer.supportedLocales().map(\.identifier),
                supportsOnDevice:{ SFSpeechRecognizer(locale:Locale(identifier:$0))?.supportsOnDeviceRecognition == true }) else {
            throw LocalIntelligenceError.unavailable("On-device speech is unavailable for \(language), or its speech locale does not match. Choose a supported language and reconnect voice.")
        }
        try selectRecognizer(locale)
        spokenLanguage = language
        UserDefaults.standard.set(locale,forKey:Self.savedLocaleKey)
    }

    private func englishSpeechLocale() -> String {
        LocalSpeechSelection.recognitionLocale(language:"English",
            preferredLocales:[localeID,Locale.current.identifier] + Locale.preferredLanguages,
            supportedLocales:AVSpeechSynthesisVoice.speechVoices().map(\.language)) ?? "en-US"
    }

    private func selectRecognizer(_ locale: String) throws {
        guard let recognizer = SFSpeechRecognizer(locale:Locale(identifier:locale)), recognizer.supportsOnDeviceRecognition else {
            throw LocalIntelligenceError.unavailable("On-device speech is unavailable for \(locale). Install that language in iPhone Settings or choose a supported device language, then reconnect.")
        }
        guard recognizer.isAvailable else { throw LocalIntelligenceError.unavailable("On-device speech is temporarily unavailable. Reconnect voice to retry.") }
        self.recognizer = recognizer; self.localeID = locale
    }

    private func listen() {
        guard active, !speaker.isSpeaking, cloudPlayer == nil, speechTask == nil, !responseInFlight else { return }
        if cloudSpeech { listenToCloud(); return }
        guard let recognizer else { return }
        stopListening()
        do {
            let audio = AVAudioSession.sharedInstance()
            // Recognition runs alone, without telephone voice processing or speaker output.
            try audio.setCategory(.playAndRecord,mode:.measurement,options:[.allowBluetoothHFP])
            try audio.setActive(true)
            let request = SFSpeechAudioBufferRecognitionRequest()
            request.requiresOnDeviceRecognition = true
            request.shouldReportPartialResults = true
            request.taskHint = .dictation
            let input = engine.inputNode, format = input.outputFormat(forBus:0)
            guard format.sampleRate > 0, format.channelCount > 0 else { throw LocalIntelligenceError.unavailable("Microphone audio format is unavailable. Reconnect voice.") }
            self.request = request
            let generation = epoch, listening = listenEpoch
            recognition = recognizer.recognitionTask(with:request) { [weak self] result, error in
                let text = result?.bestTranscription.formattedString
                let final = result?.isFinal ?? false
                Task { @MainActor in
                    guard let self, self.active, self.epoch == generation, self.listenEpoch == listening else { return }
                    if let text, !text.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty {
                        self.guidanceActivity = UUID()
                        self.cancelGuidanceForOwnerSpeech()
                        self.lastPartial = text
                        self.silenceTask?.cancel()
                        if final { self.submit(text) }
                        else {
                            self.silenceTask = Task { [weak self] in
                                try? await Task.sleep(for:.milliseconds(1400))
                                guard !Task.isCancelled, let self, self.listenEpoch == listening, self.epoch == generation else { return }
                                self.submit(text)
                            }
                        }
                    } else if error != nil {
                        self.fail("On-device speech stopped. Tap Reconnect voice to continue.")
                    }
                }
            }
            input.installTap(onBus:0,bufferSize:1024,format:format) { buffer, _ in request.append(buffer) }
            tapInstalled = true; engine.prepare(); try engine.start()
            Self.saveCapabilitySnapshot(locale:localeID,phase:"listening")
            onStatus?("Local recovery voice · listening · \(localeID) · Claude",true)
            startGuidancePolling()
        } catch { fail(error.localizedDescription) }
    }

    private func listenToCloud() {
        stopListening()
        do {
            let audio = AVAudioSession.sharedInstance()
            try audio.setCategory(.playAndRecord,mode:.measurement,options:[.allowBluetoothHFP])
            try audio.setActive(true)
            let input = engine.inputNode, format = input.outputFormat(forBus:0)
            let recorder = try CloudSpeechRecorder(inputFormat:format)
            cloudRecorder = recorder
            let generation = epoch, listening = listenEpoch
            input.installTap(onBus:0,bufferSize:1024,format:format) { [weak self] buffer, _ in
                guard let event = recorder.consume(buffer) else { return }
                Task { @MainActor in
                    guard let self, self.active, self.epoch == generation, self.listenEpoch == listening else { return }
                    switch event {
                    case .speechStarted:
                        self.guidanceActivity = UUID()
                        self.cancelGuidanceForOwnerSpeech()
                        self.cloudHasSpeech = true
                        self.onStatus?("Listening · pause when you finish · cloud speech + Claude",true)
                    case .utterance(let audio): self.transcribe(audio)
                    case .discarded: self.listen()
                    case .failure(let reason): self.fail(reason)
                    }
                }
            }
            tapInstalled = true; engine.prepare(); try engine.start()
            // The sample count bounds recordings; this also covers a stalled audio route.
            silenceTask = Task { [weak self] in
                try? await Task.sleep(for:.seconds(31))
                guard !Task.isCancelled, let self, self.active,
                      self.epoch == generation, self.listenEpoch == listening else { return }
                self.listen()
            }
            Self.saveCapabilitySnapshot(locale:localeID,phase:"listening",provider:speechProvider)
            onStatus?("Listening · cloud speech + Claude · \(spokenLanguage)",true)
            startGuidancePolling()
        } catch { fail(error.localizedDescription) }
    }

    private func transcribe(_ audio: Data) {
        guard active, !responseInFlight, let client else { return }
        stopListening(); responseInFlight = true
        onStatus?("Transcribing with cloud speech",true)
        let generation = epoch, target = path, language = transcriptionLanguage
        responseTask = Task { [weak self] in
            guard let self else { return }
            do {
                try self.checkCurrent(generation)
                let request = try CloudTranscriptionRequest(audio:audio,language:language)
                let transcription = try await client.transcribe(target,request:request)
                try self.checkCurrent(generation)
                self.responseInFlight = false
                guard !transcription.text.isEmpty else { self.listen(); return }
                self.submit(transcription.text)
            } catch {
                guard !Task.isCancelled, self.active, self.epoch == generation else { return }
                self.fail(error.localizedDescription)
            }
        }
    }

    private func speakCloud(_ text: String, language: String, completion: CheckedContinuation<Void, Error>? = nil) {
        guard active, let client else { completion?.resume(throwing:CancellationError()); return }
        let previousCompletion = cloudSpeechCompletion; cloudSpeechCompletion = completion
        previousCompletion?.resume(throwing:CancellationError())
        stopListening()
        speechTask?.cancel(); cloudPlaybackReceipt.cancel(); cloudPlaybackID = nil
        cloudPlayer?.stop(); cloudPlayer?.delegate = nil; cloudPlayer = nil
        let generation = epoch, target = path, prompt = pendingSetupPrompt
        pendingSetupPrompt = nil
        lastAssistant = text.trimmingCharacters(in:.whitespacesAndNewlines)
        onGuidance?(lastAssistant); onTranscript?("assistant",lastAssistant)
        onStatus?("Preparing cloud speech · tap Stop voice to interrupt",true)
        speechTask = Task { [weak self] in
            guard let self else { return }
            do {
                try self.checkCurrent(generation)
                let request = try CloudSynthesisRequest(text:text,language:language)
                let bytes = try await client.synthesize(target,request:request)
                try self.checkCurrent(generation)
                try self.playCloudAudio(bytes,text:text,prompt:prompt,generation:generation)
                self.speechTask = nil
            } catch {
                guard !Task.isCancelled, self.active, self.epoch == generation else { return }
                self.fail(error.localizedDescription)
            }
        }
    }

    private func playCloudAudio(_ bytes: Data, text: String, prompt: String?, generation: UUID, announce: Bool = false) throws {
        try checkCurrent(generation)
        stopListening()
        let audio = AVAudioSession.sharedInstance()
        // Playback chooses the loudspeaker without overriding a selected headset.
        try audio.setCategory(.playback,mode:.voicePrompt,options:[])
        try audio.setActive(true)
        let player = try AVAudioPlayer(data:bytes,fileTypeHint:AVFileType.wav.rawValue)
        guard player.duration > 0, player.prepareToPlay() else { throw CloudSpeechError.playbackUnavailable }
        player.volume = 1; player.delegate = self
        let playback = UUID()
        cloudPlayer = player; cloudPlaybackID = playback
        cloudPlaybackReceipt.begin(id:playback,generation:generation,prompt:prompt)
        guard player.play() else {
            cloudPlayer = nil; cloudPlaybackID = nil; cloudPlaybackReceipt.cancel()
            throw CloudSpeechError.playbackUnavailable
        }
        if announce {
            lastAssistant = text
            onGuidance?(text); onTranscript?("assistant",text)
        }
        Self.saveCapabilitySnapshot(locale:localeID,phase:"speaking",provider:speechProvider)
        onStatus?("Cloud speech · Claude reply · tap Interrupt & speak to respond",true)
    }

    nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor [weak self] in
            guard let self, self.active, self.cloudPlayer === player, let playback = self.cloudPlaybackID else { return }
            guard let delivery = self.cloudPlaybackReceipt.complete(id:playback,generation:self.epoch,success:flag) else {
                self.fail("Cloud speech playback was interrupted. Reconnect voice to continue.")
                return
            }
            self.cloudPlayer?.delegate = nil; self.cloudPlayer = nil; self.cloudPlaybackID = nil
            if delivery.prompt == "readback" { self.setup.readbackDelivered() }
            if delivery.prompt == "readiness" { self.setup.readinessDelivered() }
            if let completion = self.cloudSpeechCompletion {
                self.cloudSpeechCompletion = nil
                completion.resume()
                return // The handoff waiter now requests capture; never reopen the old microphone.
            }
            let generation = self.epoch
            // Avoid treating the speaker's final acoustic tail as a new utterance.
            try? await Task.sleep(for:.milliseconds(350))
            guard !Task.isCancelled, self.active, self.epoch == generation,
                  self.cloudPlayer == nil, self.speechTask == nil else { return }
            self.listen()
        }
    }

    nonisolated func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: Error?) {
        let reason = error?.localizedDescription ?? "Cloud speech audio could not be decoded."
        Task { @MainActor [weak self] in
            guard let self, self.active, self.cloudPlayer === player else { return }
            self.fail(reason)
        }
    }

    private func stopListening() {
        guidanceActivity = UUID()
        listenEpoch = UUID(); silenceTask?.cancel(); silenceTask = nil
        cloudRecorder?.cancel(); cloudRecorder = nil; cloudHasSpeech = false
        if engine.isRunning { engine.stop() }
        if tapInstalled { engine.inputNode.removeTap(onBus:0); tapInstalled = false }
        request?.endAudio(); recognition?.cancel(); recognition = nil; request = nil; lastPartial = ""
    }

    private func speak(_ text: String, locale: String? = nil) {
        guard active else { return }
        if cloudSpeech { speakCloud(text,language:locale ?? spokenLanguage); return }
        stopListening() // Microphone stays stopped throughout TTS, preventing echo tool calls.
        let speechLocale = locale ?? localeID
        let voices = AVSpeechSynthesisVoice.speechVoices()
        guard let choice = LocalSpeechSelection.preferredVoice(locale:speechLocale,voices:voices.map(Self.voiceDescriptor)),
              let voice = voices.first(where: { $0.identifier == choice.identifier }) else {
            fail("No speech voice is installed for \(speechLocale). Download a voice for that language in iPhone Settings, then reconnect voice.")
            return
        }
        do {
            let audio = AVAudioSession.sharedInstance()
            // Playback uses the loudspeaker when no external output is selected, and preserves
            // headphones/A2DP. Do not force a speaker override that would steal a headset route.
            try audio.setCategory(.playback,mode:.voicePrompt,options:[])
            try audio.setActive(true)
        } catch {
            fail("Speech audio is unavailable. Reconnect voice to retry.")
            return
        }
        lastAssistant = text.trimmingCharacters(in:.whitespacesAndNewlines)
        onGuidance?(lastAssistant); onTranscript?("assistant",lastAssistant)
        onStatus?("Local recovery voice · Claude reply · tap Stop voice to interrupt",true)
        let utterance = AVSpeechUtterance(string:lastAssistant)
        utterance.voice = voice
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate
        utterance.pitchMultiplier = 1
        utterance.volume = 1 // The system volume remains controlled by the user's buttons/slider.
        currentSpeech = (utterance,epoch)
        deliveredSetupPrompt = pendingSetupPrompt.map { (utterance,$0) }
        pendingSetupPrompt = nil
        Self.saveCapabilitySnapshot(locale:localeID,speechLocale:speechLocale,voice:voice,phase:"speaking")
        speaker.speak(utterance)
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        Task { @MainActor [weak self] in
            guard let self, self.active, !self.speaker.isSpeaking,
                  let current = self.currentSpeech, current.utterance === utterance,
                  current.epoch == self.epoch else { return }
            self.currentSpeech = nil
            if let prompt = self.deliveredSetupPrompt, prompt.utterance === utterance {
                if prompt.kind == "readback" { self.setup.readbackDelivered() }
                if prompt.kind == "readiness" { self.setup.readinessDelivered() }
                self.deliveredSetupPrompt = nil
            }
            let generation = self.epoch
            // Let the output tail decay before reopening the microphone.
            try? await Task.sleep(for:.milliseconds(350))
            guard self.active, self.epoch == generation, self.currentSpeech == nil else { return }
            self.listen()
        }
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        Task { @MainActor [weak self] in
            guard let self, self.active, let current = self.currentSpeech,
                  current.utterance === utterance, current.epoch == self.epoch else { return }
            // Cancellation cannot count as a delivered readback or readiness question.
            self.fail("Speech was interrupted. Tap Reconnect voice to continue.")
        }
    }

    private func submit(_ text: String) {
        guard active, !responseInFlight else { return }
        stopListening(); responseInFlight = true
        onTranscript?("user",String(text.prefix(1200))); onStatus?("Asking Claude",true)
        let generation = epoch
        responseTask = Task { [weak self] in
            guard let self else { return }
            do {
                let answer = try await self.answer(text,generation:generation)
                try self.checkCurrent(generation)
                self.responseInFlight = false
                if let answer { self.speak(answer) }
                else { self.stop() } // Cloud handoff already played, and onboarding is consumed.
            } catch {
                guard !Task.isCancelled, self.active, self.epoch == generation else { return }
                self.responseInFlight = false; self.pendingSetupPrompt = nil
                // Preserve the actual failure visibly; do not retry a failed cloud
                // speech request or disguise on-device output as the selected provider.
                if self.cloudSpeech { self.fail(error.localizedDescription) }
                else { self.speak(error.localizedDescription,locale:self.englishSpeechLocale()) }
            }
        }
    }

    private func localized(_ text: String) async throws -> String {
        guard Locale(identifier:localeID).language.languageCode?.identifier != "en" else { return text }
        return try await assistant(.translate,text:text,allowActions:false,generation:epoch).reply
    }

    private func fail(_ reason: String) {
        stop(); onStatus?(reason,false); onGuidance?(reason); onTranscript?("system",reason)
    }

    private func checkCurrent(_ generation: UUID) throws {
        try Task.checkCancellation()
        guard active, generation == epoch else { throw CancellationError() }
    }

    private func assistant(_ mode: ClaudeAssistantMode, text: String, allowActions: Bool = true,
                           previous: String? = nil, generation: UUID) async throws -> ClaudeAssistantTurn {
        try checkCurrent(generation)
        guard let client else { throw BackendError.disconnected }
        let request = ClaudeAssistantRequest(mode:mode,text:text,phase:setup.phase,preferences:setup.preferences,
            previous:previous ?? lastAssistant,language:spokenLanguage,allowActions:allowActions)
        let result = try await client.assistant(path,request:request)
        try checkCurrent(generation)
        return result
    }

    private func answer(_ text: String, generation: UUID) async throws -> String? {
        try checkCurrent(generation)
        guard let client else { throw BackendError.disconnected }
        if !isOnboarding {
            let currentConfiguration = try await client.call(path)
            try checkCurrent(generation)
            configuration = currentConfiguration
            let executor = LocalVoiceToolExecutor(client:client,path:path,
                allowed:Set(configuration["tools"].array.compactMap { $0["name"].string }))
            toolExecutor = executor
            defer {
                executor.invalidate()
                if toolExecutor === executor { toolExecutor = nil }
            }
            let result = try await assistant(.reply,text:text,generation:generation)
            guard !result.toolName.isEmpty else { return result.reply }
            // Stop also invalidates this executor on the same actor before a pending plan can run.
            try checkCurrent(generation)
            _ = try await executor.call(name:result.toolName,arguments:result.toolArguments)
            try checkCurrent(generation)
            let updatedConfiguration = try await client.call(path)
            try checkCurrent(generation)
            configuration = updatedConfiguration
            // The server reads fresh context after the validated action; a second action is forbidden.
            return try await assistant(.reply,text:text,allowActions:false,previous:result.reply,generation:generation).reply
        }

        let result = try await assistant(.setup,text:text,allowActions:false,generation:generation)
        try checkCurrent(generation)
        if !result.language.isEmpty || !result.speechLocale.isEmpty {
            try selectLanguage(result.language.isEmpty ? spokenLanguage : result.language,
                               explicitLocale:result.speechLocale.isEmpty ? nil : result.speechLocale)
        }
        let proposal: JSONValue = .object(["language":.string(result.language),"country":.string(result.country),
            "currency":.string(result.currency),"capture_mode":.string(result.captureMode)])
        let message: String
        switch setup.next(intent:result.intent,proposal:proposal) {
        case .readback(let value):
            pendingSetupPrompt = "readback"
            message = "I heard \(value["language"].string ?? ""), pricing in \(value["country"].string ?? "") using \(value["currency"].string ?? ""), and \(value["capture_mode"].string ?? "") capture. Is that correct?"
        case .configure(let value):
            var arguments = value
            if case .object(var fields) = value { fields["confirmed"] = .bool(true); arguments = .object(fields) }
            do {
                try checkCurrent(generation)
                setupRequestSent = true
                _ = try await client.call(path,body:.object(["name":.string("configure_session"),"arguments":arguments]))
                try checkCurrent(generation)
                setupRequestSent = false
                setup.configured(); pendingSetupPrompt = "readiness"
                message = "Your setup is confirmed. Are you ready to begin capture?"
            } catch {
                if generation == epoch { setupRequestSent = false; setup.configurationFailed() }
                throw error
            }
        case .requestCapture:
            do {
                if cloudSpeech {
                    try await CloudSpeechHandoff.request(
                        prepareAnnouncement: { try await self.localized("Starting capture. Move slowly and keep objects fully in view.") },
                        play: { text in
                            try self.checkCurrent(generation)
                            try await withCheckedThrowingContinuation { continuation in
                                self.speakCloud(text,language:self.spokenLanguage,completion:continuation)
                            }
                        },
                        checkCurrent: { try self.checkCurrent(generation) },
                        requestCapture: {
                            try self.checkCurrent(generation)
                            self.setupRequestSent = true
                            _ = try await client.call(self.path,body:.object(["name":.string("request_capture"),"arguments":.object(["ready":.bool(true)])]))
                        })
                    return nil
                }
                return try await LocalCaptureHandoff.request(
                    prepareAnnouncement: { try await self.localized("Starting capture. Move slowly and keep objects fully in view.") },
                    checkCurrent: { try self.checkCurrent(generation) },
                    requestCapture: {
                        try self.checkCurrent(generation)
                        self.setupRequestSent = true
                        _ = try await client.call(self.path,body:.object(["name":.string("request_capture"),"arguments":.object(["ready":.bool(true)])]))
                    })
            } catch {
                if generation == epoch { setupRequestSent = false; setup.retryReadiness() }
                throw error
            }
        case .askConfirmation:
            message = "Please confirm the language, pricing country, currency and capture mode I read back. Is that correct?"
        case .askReadiness:
            pendingSetupPrompt = "readiness"
            message = "Are you ready to begin capture?"
        case .askDetails:
            return result.reply // Claude already selected the user's language; do not translate it again.
        case .none:
            message = "Capture has already been requested."
        }
        return try await localized(message)
    }
}
