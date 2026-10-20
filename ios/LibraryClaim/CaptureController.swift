import SwiftUI
import RoomPlan
import ARKit
import AVFoundation
import Combine

@MainActor final class CaptureController: NSObject, ObservableObject, @MainActor RoomCaptureSessionDelegate {
    @Published var country = ""
    @Published var currency = ""
    @Published var language = ""
    @Published var captureMode: CaptureMode = .camera
    @Published var cameraView: ARSCNView?
    let overlays = ObjectOverlayController()
    @Published var packet: JSONValue = .null
    @Published var comparison: JSONValue = .null
    @Published var secondLocale: JSONValue = .null
    @Published var guidance = "Talk to the assistant to choose your language and pricing country. Capture uses this iPhone’s supported sensors."
    @Published var status = "Ready to talk"
    @Published var transcript = ""
    @Published var intelligenceStatus = "OpenAI Realtime"
    @Published var localVoiceStatus = "OpenAI Realtime voice"
    @Published var isCapturing = false
    @Published var isStarting = false
    @Published var isFinishing = false
    @Published var isVoiceConnected = false
    @Published var hasConversation = false
    @Published var pendingUploads = 0
    @Published var savedFrames = 0
    @Published var elapsed = 0.0
    @Published var packetURL: URL?
    @Published var reviewURL: URL?
    @Published var canRetryFinish = false
    @Published var canRetryHandoff = false
    @Published var detectedFloorCount = 0
    @Published var detectedWallCount = 0
    @Published var roomScanStatus = "Room scan starts when you are ready."
    @Published var roomScanCoaching = ""
    private var arSession: ARSession?
    private var roomCaptureSession: RoomCaptureSession?
    private let encoder = FrameEncoder()
    private let detector = LocalObjectDetector()
    private let diagnosticStore = DiagnosticPhotoStore()
    #if DEBUG
    private var diagnosticPhotos = DiagnosticPhotoSchedule(enabled:CommandLine.arguments.contains("--capture-diagnostics"))
    #else
    private var diagnosticPhotos = DiagnosticPhotoSchedule(enabled:false)
    #endif
    private var savingDiagnostic = false
    private let liveAudio = LiveAudio()
    private var voiceOutbox: RealtimeEventOutbox?
    private var audioSenderTask: Task<Void,Never>?
    private var voiceStartupTask: Task<Void,Never>?
    private var voiceConnection: UUID?
    private var awaitingVoiceInterrupt = false
    private var audioObservers: [NSObjectProtocol] = []
    private var recognitionTimer: Timer?
    private var isRecognizing = false
    private var selection = ViewSelection()
    private var lastClarityPrompt = -Double.infinity
    private var depthConfigurationAttempts = 0
    private var lastDepthConfiguration = -Double.infinity
    private var backend: BackendClient?
    private var outbox: EvidenceOutbox?
    private var directory: URL?
    private var timer: Timer?
    private var startedAt = 0.0
    private var frameNumber = 0
    private var lastFrameTime = -1.0
    private var sessionID = ""
    private var sweepID = ""
    private var onboardingID = ""
    private var preferences: JSONValue = .null
    private var captureRequestHandled = false
    private var pendingCaptureRequest = false
    private var comparisonRequested = false
    private var packetFinished = false
    private var downloadedPacket = false
    private var comparisonFinished = false
    private var captureIssues: [String] = []
    private var geometryData: Data?
    private var geometryUploadTask: Task<Void,Never>?
    private var liveGeometryUploads = LiveGeometryUploadGate()
    private var roomProcessingTask: Task<Void,Never>?
    private var lastRoomUpdate = -Double.infinity
    private var encodingTask: Task<Void,Never>?
    private var uploadingTask: Task<Void,Never>?
    private var receiverTask: Task<Void,Never>?
    private var eventEpoch = UUID()
    @Published private var voiceWanted = false
    private var isEncoding = false
    private var finalizationInFlight = false
    private var isConnectingVoice = false
    private var isForeground = true
    var isVoiceStarting: Bool { voiceWanted && !isVoiceConnected }

    override init() {
        super.init()
        captureMode = CaptureModePolicy.preferred(lidarSupported:lidarSupported)
        let center = NotificationCenter.default
        for name in [AVAudioSession.interruptionNotification, AVAudioSession.mediaServicesWereResetNotification] {
            audioObservers.append(center.addObserver(forName:name,object:nil,queue:.main) { [weak self] notification in
                MainActor.assumeIsolated {
                    guard let self else { return }
                    if notification.name == AVAudioSession.mediaServicesWereResetNotification {
                        // Rebuild even while voice is stopped: the next connection
                        // must never reuse objects invalidated by the audio service.
                        self.liveAudio.mediaServicesWereReset()
                        return
                    }
                    guard (notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt) == AVAudioSession.InterruptionType.began.rawValue,
                          self.voiceWanted else { return }
                    // Freeze the connection identity at notification receipt,
                    // before scheduling async cleanup on the actor.
                    let epoch = self.eventEpoch
                    Task { [weak self] in
                        await self?.failRealtime("Audio was interrupted. Tap Reconnect voice to continue.",epoch:epoch)
                    }
                }
            })
        }
    }

    deinit { audioObservers.forEach(NotificationCenter.default.removeObserver) }

    var rows: [InventoryRow] { (packet["books"].array + packet["items"].array).map { InventoryRow(value:$0) } }
    /// Reading saved inventories never adopts their capture session or coordinates.
    func savedInventoryClient() throws -> BackendClient {
        if let backend { return backend }
        guard let address = Bundle.main.object(forInfoDictionaryKey:"ClaimServerURL") as? String,
              let token = Bundle.main.object(forInfoDictionaryKey:"ClaimPairToken") as? String,
              !token.isEmpty, !token.contains("$(") else { throw BackendError.invalidAddress }
        return try BackendClient(address:address,token:token)
    }
    var count: Int { Int(packet["totals"]["book_count"].number ?? 0) }
    var reviewCount: Int { packet["review_queue"].array.count }
    var hasCapture: Bool { !sweepID.isEmpty }
    var canStartAgain: Bool { hasConversation && !canRetryHandoff && (!captureRequestHandled || !sweepID.isEmpty) && !isStarting && !isCapturing && !isFinishing && (sweepID.isEmpty || (packetFinished && pendingUploads == 0)) }
    var cameraSupported: Bool {
        #if targetEnvironment(simulator)
        return false
        #else
        return ARWorldTrackingConfiguration.isSupported && AVCaptureDevice.default(for:.video) != nil
        #endif
    }
    var lidarSupported: Bool {
        cameraSupported && RoomCaptureSession.isSupported && ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)
    }
    private var device: String { UIDevice.current.model + " · iOS " + UIDevice.current.systemVersion }

    func prepareCameraPreview() async {
        guard isForeground, cameraSupported, cameraView == nil else { return }
        guard await AVCaptureDevice.requestAccess(for:.video) else {
            guidance = "Enable Camera access in Settings to see and capture the room."
            return
        }
        guard isForeground, cameraView == nil else { return }
        let session = ARSession(), view = ARSCNView(frame:.zero)
        view.session = session; view.automaticallyUpdatesLighting = true
        arSession = session; overlays.attach(view:view); overlays.reset(sessionID:"")
        cameraView = view
        captureMode = CaptureModePolicy.preferred(lidarSupported:lidarSupported)
        session.run(cameraConfiguration(mode:captureMode))
        recognitionTimer?.invalidate()
        recognitionTimer = Timer.scheduledTimer(withTimeInterval:0.25,repeats:true) { [weak self] _ in
            Task { @MainActor in self?.recognizePreview() }
        }
    }

    private func cameraConfiguration(mode: CaptureMode) -> ARWorldTrackingConfiguration {
        let config = ARWorldTrackingConfiguration()
        config.frameSemantics = mode == .lidar ? [.sceneDepth] : []
        config.worldAlignment = .gravity
        config.planeDetection = [.horizontal,.vertical]
        if let format = ARWorldTrackingConfiguration.supportedVideoFormats.filter({ $0.framesPerSecond <= 30 })
            .max(by:{ $0.imageResolution.width*$0.imageResolution.height < $1.imageResolution.width*$1.imageResolution.height }) {
            config.videoFormat = format
        }
        return config
    }

    func start() async {
        guard isForeground, !isStarting && !hasConversation else { return }
        isStarting = true
        defer { isStarting = false; resumePendingCapture() }
        do {
            let microphone = await withCheckedContinuation { continuation in
                AVAudioApplication.requestRecordPermission { continuation.resume(returning:$0) }
            }
            guard microphone else { throw CaptureError.permission }
            await prepareCameraPreview()
            guard isForeground else { throw CaptureError.background }
            guard let address = Bundle.main.object(forInfoDictionaryKey:"ClaimServerURL") as? String,
                  let token = Bundle.main.object(forInfoDictionaryKey:"ClaimPairToken") as? String,
                  !token.isEmpty, !token.contains("$(") else { throw BackendError.invalidAddress }
            let client = try BackendClient(address:address,token:token)
            let created = try await client.call("api/onboarding",body:.object([
                "device":.string(device), "capture_supported":.bool(lidarSupported), "camera_supported":.bool(cameraSupported)]))
            guard let id = created["id"].string else { throw BackendError.disconnected }
            backend = client; onboardingID = id; hasConversation = true
            status = "Connecting assistant"
            await connectVoice()
        } catch { status = "Could not connect"; guidance = error.localizedDescription }
    }

    private func beginCapture() async {
        guard isForeground, !isStarting, (!captureRequestHandled || canRetryHandoff), !isCapturing, sweepID.isEmpty,
              preferences["confirmed"] == .bool(true), let backend else { return }
        guard let selection = preferences["capture_mode"].string,
              cameraSupported, CaptureModePolicy.accepts(selection,lidarSupported:lidarSupported) else {
            guidance = "The confirmed capture mode does not match this iPhone’s sensors. Confirm setup again on a supported physical iPhone."
            return
        }
        let recoveringHandoff = canRetryHandoff
        captureRequestHandled = true
        canRetryHandoff = false
        isStarting = true
        defer { isStarting = false }
        do {
            guard await AVCaptureDevice.requestAccess(for:.video) else { throw CaptureError.permission }
            guard isForeground else { throw CaptureError.background }
            await stopVoice(closeEvents:true)
            guard isForeground else { throw CaptureError.background }
            let created = try await backend.call("api/sweeps",body:.object([
                "device":.string(device), "onboarding_id":.string(onboardingID)]))
            guard let id = created["sweep"]["id"].string else { throw BackendError.disconnected }
            sweepID = id
            packet = created
            reviewURL = await backend.reviewURL(sweep:id)
            if recoveringHandoff {
                captureIssues.append("Recovered the original confirmed sweep after a handoff retry; server creation time may precede physical camera startup")
            }
            guard let modeName = created["sweep"]["capture_mode"].string,
                  let mode = CaptureMode(rawValue:modeName) else { throw BackendError.disconnected }
            comparisonRequested = created["sweep"]["comparison_requested"] == .bool(true)
            // The server owns the selected modality. Reject a response the hardware cannot run.
            guard mode != .lidar || lidarSupported else { throw CaptureError.unsupportedDevice }
            let expectedMode = CaptureModePolicy.preferred(lidarSupported:lidarSupported)
            guard mode == expectedMode, comparisonRequested == (selection == "comparison") else { throw CaptureError.modeMismatch }
            captureMode = mode
            guard created["completion"].string == "capturing" else {
                captureIssues.append("The recovered sweep was interrupted by a server restart; physical capture was not resumed")
                await finalize()
                return
            }
            guard isForeground else { throw CaptureError.background }
            sessionID = UUID().uuidString
            liveGeometryUploads.reset(sessionID:sessionID)
            detectedFloorCount = 0; detectedWallCount = 0; roomScanCoaching = ""
            let root = FileManager.default.urls(for:.documentDirectory,in:.userDomainMask)[0]
                .appendingPathComponent("Captures/"+id,isDirectory:true)
            let box = try EvidenceOutbox(root:root)
            outbox = box; directory = root
            try JSONEncoder().encode(created).write(to:root.appendingPathComponent("session.json"),options:.atomic)
            packet = created; reviewURL = await backend.reviewURL(sweep:id)
            await prepareCameraPreview()
            guard let session = arSession else { throw CaptureError.unsupportedDevice }
            overlays.reset(sessionID:sessionID)
            self.selection.reset(); depthConfigurationAttempts = 0
            session.run(cameraConfiguration(mode:mode),options:[.resetTracking,.removeExistingAnchors])
            if mode == .lidar {
                // The visible camera, RoomPlan and evidence use the same world origin.
                let roomSession = RoomCaptureSession(arSession:session)
                roomSession.delegate = self; roomCaptureSession = roomSession
                roomSession.run(configuration:RoomCaptureSession.Configuration())
            }
            // The capture clock starts only after spoken readiness and physical AR startup.
            startedAt = ProcessInfo.processInfo.systemUptime
            isCapturing = true; status = mode == .camera ? "Capturing · camera" : "Capturing · LiDAR"
            roomScanStatus = mode == .lidar ? "Looking for floor and wall surfaces." : "Camera fallback · physical room dimensions unavailable."
            guidance = mode == .lidar ? "Scan the floor, every wall and ceiling edge, then walk slowly around the table and furniture to measure all sides." : "Move slowly across the room. Keep each object fully in view; camera-only capture cannot supply physical dimensions."
            ensureDepthConfiguration()
            let sid = sessionID
            uploadingTask = Task { [weak self] in await self?.uploadLoop(client:backend,box:box,id:id,sessionID:sid) }
            await connectVoice()
        } catch {
            status = "Capture could not start"
            guidance = error.localizedDescription
            if !sweepID.isEmpty {
                captureIssues.append("Capture startup failed: \(error.localizedDescription)")
                timer?.invalidate(); timer = nil; arSession?.pause(); isCapturing = false
                await finalize()
            } else if case BackendError.handoffUnavailable = error {
                // Server checked persisted handoffs before confirming none exists.
                // It is now safe to discard this expired conversation and start again.
                canRetryHandoff = false; captureRequestHandled = false
                status = "Conversation expired"
                guidance += " Tap Start again."
            } else {
                canRetryHandoff = true
                guidance += " Tap Retry capture handoff to recover the same confirmed sweep."
            }
        }
    }

    func retryCaptureHandoff() async { await beginCapture() }

    private func recognizePreview() {
        guard isForeground, !isRecognizing, (!hasCapture || isCapturing),
              let session = arSession, let frame = session.currentFrame else { return }
        isRecognizing = true
        let captureGeneration = sessionID
        let orientation = cameraView?.window?.windowScene?.effectiveGeometry.interfaceOrientation ?? .portrait
        let rotation = orientation == .portrait ? 90 : orientation == .portraitUpsideDown ? 270 : orientation == .landscapeLeft ? 180 : 0
        Task { [weak self, detector] in
            defer { self?.isRecognizing = false }
            do {
                let result = try await detector.detect(frame.capturedImage,rotation:rotation)
                guard let self, self.isForeground, self.arSession === session,
                      (!self.hasCapture || self.isCapturing),
                      CaptureFrameGate.accepts(frameTime:frame.timestamp,captureStart:self.isCapturing ? self.startedAt : 0,
                                               generation:captureGeneration,currentGeneration:self.sessionID) else { return }
                self.overlays.showLocal(result,frame:frame)
                self.captureDiagnostic(frame,recognition:result,rotation:rotation)
                if self.isCapturing {
                    self.elapsed = ProcessInfo.processInfo.systemUptime-self.startedAt
                    self.ensureDepthConfiguration()
                    if let message = result.clarityInstruction { self.requestClearView(message) }
                    self.sampleFrame(frame,recognition:result,rotation:rotation)
                }
            } catch { self?.overlays.localStatus = error.localizedDescription }
        }
    }

    private func captureDiagnostic(_ frame: ARFrame, recognition: LocalRecognition, rotation: Int) {
        guard !savingDiagnostic, let number = diagnosticPhotos.reserve(time:frame.timestamp,foreground:isForeground,hasConversation:hasConversation) else { return }
        savingDiagnostic = true
        status = "Photo check · \(number) of 6"
        guidance = "Keep the table, router or charger fully in view. Diagnostic photos stay on this device until copied for testing."
        Task { [weak self, encoder, diagnosticStore] in
            defer { self?.savingDiagnostic = false }
            do {
                let payload = try await encoder.encode(frame,id:String(format:"photo%02d",number),elapsed:frame.timestamp,
                    sessionID:"diagnostic",mode:.camera,recognition:recognition,rotation:rotation)
                try await diagnosticStore.save(payload)
                if number == 6 { self?.status = "Photo check complete · 6 photos saved" }
            } catch { self?.status = "Photo check failed: \(error.localizedDescription)" }
        }
    }

    private func ensureDepthConfiguration() {
        guard captureMode == .lidar, isCapturing, let session = arSession,
              let configuration = session.configuration?.copy() as? ARWorldTrackingConfiguration else { return }
        let now = ProcessInfo.processInfo.systemUptime
        // RoomPlan owns the configuration. Preserve it and add depth after it starts.
        if !configuration.frameSemantics.contains(.sceneDepth), depthConfigurationAttempts < 2, now-lastDepthConfiguration > 2 {
            configuration.frameSemantics.insert(.sceneDepth)
            session.run(configuration)
            depthConfigurationAttempts += 1; lastDepthConfiguration = now
        }
        let available = session.currentFrame?.sceneDepth != nil || session.currentFrame?.smoothedSceneDepth != nil
        overlays.depthStatus = available ? "LiDAR depth available" : "Waiting for aligned LiDAR depth · dimensions pending"
    }

    private func requestClearView(_ message: String) {
        let now = ProcessInfo.processInfo.systemUptime
        guard now-lastClarityPrompt > 15 else { return }
        lastClarityPrompt = now; guidance = message
        if voiceWanted, isVoiceConnected {
            voiceOutbox?.offer(.object(["type":.string("capture_feedback"),"text":.string(message)]))
        }
    }

    private func sampleFrame(_ frame: ARFrame, recognition: LocalRecognition, rotation: Int) {
        guard isCapturing, !isEncoding,
              frame.timestamp >= startedAt, frame.timestamp != lastFrameTime, let outbox else { return }
        let pose = (0..<4).flatMap { c in (0..<4).map { r in frame.camera.transform[c][r] } }
        let tracking: Bool
        if case .normal = frame.camera.trackingState { tracking = true } else { tracking = false }
        let depthLevel = captureMode == .lidar ? FrameEncoder.depthReadiness(frame,objects:recognition.detections) : 0
        guard selection.shouldCapture(recognition,pose:pose,time:frame.timestamp,tracking:tracking,pending:pendingUploads,depthLevel:depthLevel) else { return }
        lastFrameTime = frame.timestamp; frameNumber += 1
        elapsed = ProcessInfo.processInfo.systemUptime-startedAt
        let id = String(format:"f%05d",frameNumber)
        let captureElapsed = max(0,frame.timestamp-startedAt), sid = sessionID, mode = captureMode
        isEncoding = true
        encodingTask = Task { [weak self, encoder] in
            do {
                let payload = try await encoder.encode(frame,id:id,elapsed:captureElapsed,sessionID:sid,mode:mode,recognition:recognition,rotation:rotation)
                self?.overlays.remember(payload,capturedAt:frame.timestamp)
                try await outbox.save(JSONEncoder().encode(payload),id:id)
                self?.selection.record(recognition,pose:pose,time:frame.timestamp,depthLevel:depthLevel)
                self?.savedFrames += 1
                self?.pendingUploads = (try? await outbox.pendingCount()) ?? 0
            } catch {
                self?.captureIssues.append("Frame \(id) could not be saved: \(error.localizedDescription)")
                self?.guidance = "Camera evidence could not be saved. End the sweep and check storage."
            }
            self?.isEncoding = false
        }
    }

    private func uploadLoop(client: BackendClient, box: EvidenceOutbox, id: String, sessionID sid: String) async {
        while !Task.isCancelled {
            do {
                if let file = try await box.nextPending() {
                    let response = try await client.rawCall("api/sweeps/\(id)/frames",body:Data(contentsOf:file))
                    if response["accepted"] == .bool(false) {
                        overlays.serverStatus = "Server is processing earlier photos · upload waiting"
                        try await Task.sleep(for:.seconds(min(15,max(1,response["retry_after_s"].number ?? 3))))
                        continue
                    }
                    guard !Task.isCancelled, sessionID == sid, sweepID == id else { return }
                    if response["accepted"] == .bool(true) {
                        liveGeometryUploads.frameAccepted(sessionID:sid)
                        flushLiveGeometry()
                    }
                    overlays.serverStatus = response["duplicate"] == .bool(true) ? "Repeat photo skipped" : "Clear photo sent · server recognizing"
                    try await box.acknowledge(file)
                    pendingUploads = try await box.pendingCount()
                } else { try await Task.sleep(for:.milliseconds(250)) }
            } catch is CancellationError { return }
            catch BackendError.backpressure(let delay) {
                overlays.serverStatus = "Server is processing earlier photos · upload waiting"
                try? await Task.sleep(for:.seconds(delay))
            }
            catch {
                guidance = "Upload paused: \(error.localizedDescription). Frames remain saved on this phone; reconnect to the same Wi-Fi."
                try? await Task.sleep(for:.seconds(3))
            }
        }
    }

    func endCapture() {
        guard isCapturing else { return }
        isCapturing = false; isFinishing = true; status = "Finishing capture"
        elapsed = ProcessInfo.processInfo.systemUptime-startedAt
        timer?.invalidate(); timer = nil
        overlays.stop()
        liveGeometryUploads.beginFinalization()
        if captureMode == .camera {
            arSession?.pause()
            Task { await finalize() }
        } else {
            roomScanStatus = "Processing the captured floor outline."
            roomScanCoaching = ""
            roomCaptureSession?.stop(pauseARSession:true)
        }
    }

    func captureSession(_ session: RoomCaptureSession, didUpdate room: CapturedRoom) {
        guard session === roomCaptureSession, captureMode == .lidar, isCapturing, isForeground else { return }
        detectedFloorCount = room.floors.count; detectedWallCount = room.walls.count
        roomScanStatus = room.floors.isEmpty
            ? "Looking for the floor · scan where the floor meets each wall."
            : "Floor detected · its outline and area are calculated when you end the sweep."
        guard ProcessInfo.processInfo.systemUptime-lastRoomUpdate >= 3 else { return }
        lastRoomUpdate = ProcessInfo.processInfo.systemUptime
        guard let data = try? RoomGeometry.encode(room,sessionID:sessionID,issues:captureIssues,final:false),
              let value = try? JSONDecoder().decode(JSONValue.self,from:data) else { return }
        overlays.showFurniture(value["objects"].array.compactMap(MeasuredFurniture.init))
        liveGeometryUploads.offer(data,sessionID:sessionID)
        flushLiveGeometry()
    }

    private func flushLiveGeometry() {
        guard isCapturing, geometryUploadTask == nil, let backend,
              let data = liveGeometryUploads.next() else { return }
        let id = sweepID, sid = sessionID
        geometryUploadTask = Task { [weak self] in
            var succeeded = false
            do {
                try Task.checkCancellation()
                _ = try await backend.rawCall("api/sweeps/\(id)/geometry",body:data)
                succeeded = true
            } catch { }
            guard let self, self.sessionID == sid else { return }
            self.liveGeometryUploads.completed(data,sessionID:sid,succeeded:succeeded)
            self.geometryUploadTask = nil
            if succeeded, !Task.isCancelled { self.flushLiveGeometry() }
            else if !Task.isCancelled { self.overlays.depthStatus = "Live room upload pending · final geometry will retry" }
        }
    }

    func captureSession(_ session: RoomCaptureSession, didProvide instruction: RoomCaptureSession.Instruction) {
        guard session === roomCaptureSession, captureMode == .lidar, isCapturing, isForeground else { return }
        switch instruction {
        case .normal: roomScanCoaching = ""
        case .moveCloseToWall: roomScanCoaching = "Move closer to the wall and show its bottom edge."
        case .moveAwayFromWall: roomScanCoaching = "Step back so the wall and its edges fit in view."
        case .slowDown: roomScanCoaching = "Move the iPhone more slowly."
        case .turnOnLight: roomScanCoaching = "Add light so the floor and wall edges are visible."
        case .lowTexture: roomScanCoaching = "Aim at a corner or an edge with visible detail."
        @unknown default: roomScanCoaching = "Continue slowly around the room’s edges."
        }
    }

    func captureSession(_ session: RoomCaptureSession, didEndWith data: CapturedRoomData, error: Error?) {
        guard session === roomCaptureSession, captureMode == .lidar else { return }
        // RoomPlan can stop itself (tracking/thermal error), not only through End sweep.
        isCapturing = false; timer?.invalidate(); timer = nil; arSession?.pause(); overlays.stop()
        liveGeometryUploads.beginFinalization()
        roomScanStatus = "Processing the captured floor outline."; roomScanCoaching = ""
        elapsed = ProcessInfo.processInfo.systemUptime-startedAt
        let sid = sessionID
        roomProcessingTask = Task { [weak self] in
            guard let self else { return }
            defer { if self.sessionID == sid { self.roomProcessingTask = nil } }
            do {
                if let error { captureIssues.append("RoomPlan failed: \(error.localizedDescription)") }
                let room = try await RoomBuilder(options:[]).capturedRoom(from:data)
                guard !Task.isCancelled, sessionID == sid else { return }
                detectedFloorCount = room.floors.count; detectedWallCount = room.walls.count
                geometryData = try RoomGeometry.encode(room,sessionID:sessionID,issues:captureIssues)
                if let directory, let geometryData { try geometryData.write(to:directory.appendingPathComponent("room.json"),options:.atomic) }
                let result = geometryData.flatMap { try? JSONDecoder().decode(JSONValue.self,from:$0) }
                roomScanStatus = result?["floors"].array.isEmpty == false
                    ? "Floor outline processed · measurements awaiting upload validation."
                    : "Floor outline unavailable · floor area remains unknown."
            } catch {
                guard !Task.isCancelled, sessionID == sid else { return }
                captureIssues.append("Geometry export failed: \(error.localizedDescription)")
                roomScanStatus = "Room processing failed · floor area remains unknown."
            }
            guard !Task.isCancelled, sessionID == sid else { return }
            await finalize()
        }
    }

    func finalize() async {
        guard !isCapturing, !finalizationInFlight, !sweepID.isEmpty, let backend else { return }
        finalizationInFlight = true
        liveGeometryUploads.beginFinalization()
        isFinishing = true; canRetryFinish = false
        defer { isFinishing = false; finalizationInFlight = false }
        await encodingTask?.value
        status = "Uploading saved evidence"
        let deadline = Date().addingTimeInterval(60)
        while await pendingEvidenceCount() > 0 && Date() < deadline {
            try? await Task.sleep(for:.milliseconds(300))
        }
        guard await pendingEvidenceCount() == 0 else {
            guidance = "Some evidence is still on the phone. Keep this app open, reconnect Wi-Fi, then tap Retry finish."
            status = "Awaiting uploads"; canRetryFinish = true; return
        }
        do {
            if savedFrames == 0, !captureIssues.contains("No camera evidence was captured") {
                captureIssues.append("No camera evidence was captured")
            }
            if !packetFinished {
                await geometryUploadTask?.value
                if captureMode == .lidar, let geometryData {
                    _ = try await backend.rawCall("api/sweeps/\(sweepID)/geometry",body:geometryData)
                }
                status = "Building claim packet"
                packet = try await backend.call("api/sweeps/\(sweepID)/finish",body:.object([
                    "duration_s":.number(elapsed), "capture_issues":.array(captureIssues.map { .string($0) })]),timeout:300)
                packetFinished = true
                if captureMode == .lidar {
                    roomScanStatus = packet["room"]["floor_area_m2"].number != nil
                        ? "Floor area calculated from the captured outline."
                        : "Floor outline unavailable or incomplete · floor area remains unknown."
                }
            }
            if !downloadedPacket {
                packetURL = try await backend.downloadPacket(sweep:sweepID)
                downloadedPacket = true
            }
            if comparisonRequested && !comparisonFinished {
                status = "Comparing camera and LiDAR responses"
                if savedFrames == 0 {
                    comparison = .object(["status":.string("unavailable"), "reason":.string("No camera evidence was captured")])
                } else {
                    do {
                        comparison = try await backend.cameraComparison(sweep:sweepID)
                    } catch BackendError.comparisonConflict(.unavailable, let reason) {
                        comparison = .object(["status":.string("unavailable"), "reason":.string(reason)])
                    }
                }
                comparisonFinished = true
            }
            uploadingTask?.cancel()
            status = "Packet ready · \(reviewCount) review entries"
            guidance = "Review unknown facts and exclusions before using this packet."
        } catch {
            status = "Finish needs attention"; guidance = error.localizedDescription; canRetryFinish = true
        }
    }

    private func pendingEvidenceCount() async -> Int {
        guard let outbox else { return 0 }
        return (try? await outbox.pendingCount()) ?? 1
    }

    func connectVoice() async {
        guard isForeground, !isConnectingVoice, !isVoiceStarting, let backend, hasConversation else { return }
        RealtimeVoiceDiagnostics.shared.record("voice_connect")
        isConnectingVoice = true
        defer { isConnectingVoice = false; resumePendingCapture() }
        await stopVoice(closeEvents:true)
        guard isForeground else { return }
        if sweepID.isEmpty { status = "Connecting OpenAI voice" }
        localVoiceStatus = "Connecting OpenAI Realtime"
        voiceWanted = true
        let epoch = eventEpoch
        do {
            let health = try await backend.call("api/health")
            guard isForeground, voiceWanted, eventEpoch == epoch else { return }
            guard RealtimeVoiceCapability.available(health) else { throw RealtimeVoiceError.unavailable }
            let microphone = await withCheckedContinuation { continuation in
                AVAudioApplication.requestRecordPermission { continuation.resume(returning:$0) }
            }
            guard isForeground, voiceWanted, eventEpoch == epoch else { return }
            guard microphone else { throw CaptureError.permission }
            let connection: UUID
            if sweepID.isEmpty { connection = try await backend.connect(onboarding:onboardingID,startVoice:false) }
            else { connection = try await backend.connect(sweep:sweepID,startVoice:false) }
            guard isForeground, voiceWanted, eventEpoch == epoch else {
                await backend.closeVoice(connection:connection); return
            }
            voiceConnection = connection
            let outbox = RealtimeEventOutbox { [weak self] error in
                Task { @MainActor [weak self] in await self?.failRealtime(error.localizedDescription,epoch:epoch,closeEvents:true) }
            }
            voiceOutbox = outbox
            try await liveAudio.start(onPCM:{ outbox.offerPCM($0) },onPlayed:{ outbox.offer($0) },onFailure:{ outbox.fail($0) },onDrained:{ [weak self] in
                MainActor.assumeIsolated {
                    guard let self, self.eventEpoch == epoch, self.voiceWanted, self.isForeground else { return }
                    self.localVoiceStatus = "Listening · OpenAI Realtime"
                }
            })
            guard isForeground, voiceWanted, eventEpoch == epoch else { return }
            audioSenderTask = Task { [weak self] in
                do {
                    for try await event in outbox.events {
                        try Task.checkCancellation()
                        try outbox.checkOpen()
                        try await backend.send(event,connection:connection)
                    }
                } catch {
                    guard !Task.isCancelled else { return }
                    await self?.failRealtime(error.localizedDescription,epoch:epoch,closeEvents:true)
                }
            }
            receiverTask = Task { [weak self] in
                do {
                    while !Task.isCancelled {
                        let event = try await backend.receive(connection:connection)
                        guard let self, self.eventEpoch == epoch, !Task.isCancelled else { return }
                        self.handle(event)
                    }
                } catch {
                    guard !Task.isCancelled else { return }
                    await self?.failRealtime("Voice disconnected. Tap Reconnect voice to continue.",epoch:epoch,closeEvents:true)
                }
            }
            voiceStartupTask = Task { [weak self] in
                do { try await Task.sleep(for:.seconds(25)) } catch { return }
                guard let self, !self.isVoiceConnected else { return }
                await self.failRealtime("OpenAI voice did not become ready. Tap Reconnect voice to try again.",epoch:epoch)
            }
            outbox.offer(.object(["type":.string("start_voice")]))
            RealtimeVoiceDiagnostics.shared.record("voice_start_queued")
            intelligenceStatus = "OpenAI Realtime"
        } catch {
            await failRealtime(error.localizedDescription,epoch:epoch,closeEvents:true)
        }
    }

    private func failRealtime(_ message: String, epoch: UUID, closeEvents: Bool = false) async {
        guard eventEpoch == epoch, voiceWanted else { return }
        RealtimeVoiceDiagnostics.shared.record("voice_failed",["reason":String(message.prefix(300))])
        await stopVoice(closeEvents:closeEvents)
        // A newer connection must never inherit an old connection's failure.
        guard !voiceWanted else { return }
        localVoiceStatus = message; guidance = message
        if sweepID.isEmpty { status = "Voice needs attention" }
    }

    private func resumePendingCapture() {
        guard pendingCaptureRequest, isForeground, !isStarting, !isConnectingVoice else { return }
        pendingCaptureRequest = false
        let epoch = eventEpoch
        Task {
            guard eventEpoch == epoch, voiceWanted else { return }
            await beginCapture()
        }
    }

    private func handle(_ event: JSONValue) {
        switch event["type"].string {
        case "recognition_status":
            overlays.serverStatus = event["text"].string ?? "Server recognition pending"
        case "setup_confirmed":
            let setup = event["preferences"]
            guard setup["confirmed"] == .bool(true) else { return }
            preferences = setup
            language = setup["language"].string ?? ""
            country = setup["country"].string ?? ""; currency = setup["currency"].string ?? ""
            status = "Setup confirmed · \(language) · \(country) · \(currency)"
        case "capture_requested":
            guard voiceWanted else { return }
            pendingCaptureRequest = true
            resumePendingCapture()
        case "inventory", "packet_ready": packet = event["packet"]
        case "frame_overlays":
            if isCapturing, let arSession { overlays.accept(event,session:arSession) }
        case "comparison_ready": comparison = event["report"] != .null ? event["report"] : event["result"]
        case "second_locale": secondLocale = event["result"]
        case "guidance": guidance = event["text"].string ?? ""
        case "voice_error":
            guard voiceWanted else { return }
            let message = event["text"].string ?? "OpenAI voice stopped. Reconnect voice to continue."
            let epoch = eventEpoch
            Task { await failRealtime(message,epoch:epoch) }
        case "voice_ready":
            guard voiceWanted, isForeground else { return }
            RealtimeVoiceDiagnostics.shared.record("voice_ready")
            voiceStartupTask?.cancel(); voiceStartupTask = nil
            isVoiceConnected = true; voiceOutbox?.enableMicrophone()
            localVoiceStatus = "Listening · OpenAI Realtime"
            if sweepID.isEmpty { status = "Talk to set up your sweep" }
        case "audio":
            guard voiceWanted, isVoiceConnected, isForeground, !awaitingVoiceInterrupt else { return }
            do {
                try liveAudio.play(RealtimeAudioChunk(event))
                localVoiceStatus = "Speaking · OpenAI Realtime · you can interrupt"
            } catch { voiceOutbox?.fail(error) }
        case "interrupted":
            guard voiceWanted else { return }
            RealtimeVoiceDiagnostics.shared.record("voice_interrupted")
            liveAudio.interrupt(); awaitingVoiceInterrupt = false
            localVoiceStatus = "Listening · OpenAI Realtime"
        case "turn_complete":
            // Generation ending is not proof that the queued audio was heard.
            // Only LiveAudio's dataPlayedBack callback sends playback receipts.
            break
        case "transcript":
            transcript += "\(event["role"].string ?? ""): \(event["text"].string ?? "")\n"
            if transcript.count > 6000 { transcript = String(transcript.suffix(6000)) }
        default: break
        }
    }

    func interruptAndListen() {
        guard isForeground, voiceWanted, isVoiceConnected else { return }
        liveAudio.interrupt()
        awaitingVoiceInterrupt = true
        voiceOutbox?.offer(.object(["type":.string("interrupt_voice")]))
        localVoiceStatus = "Listening · OpenAI Realtime"
    }

    private func stopRealtimeAudio() {
        voiceOutbox?.finish(); voiceOutbox = nil
        audioSenderTask?.cancel(); audioSenderTask = nil
        voiceStartupTask?.cancel(); voiceStartupTask = nil
        liveAudio.stop(); awaitingVoiceInterrupt = false
        isVoiceConnected = false; voiceWanted = false
    }

    func stopVoice(closeEvents: Bool = false) async {
        let connection = voiceConnection
        stopRealtimeAudio()
        localVoiceStatus = "OpenAI voice stopped"
        if sweepID.isEmpty && !isStarting { status = canRetryHandoff ? "Capture handoff needs attention" : "Voice stopped" }
        if closeEvents || !isForeground {
            eventEpoch = UUID(); receiverTask?.cancel(); receiverTask = nil; voiceConnection = nil
            await backend?.closeVoice(connection:connection)
        } else if let connection {
            try? await backend?.stopVoice(connection:connection)
        }
        // Stopping voice leaves inventory and overlays on the event-only socket.
    }

    func startAgain() async {
        guard canStartAgain else { return }
        await stopVoice(closeEvents:true)
        uploadingTask?.cancel(); await uploadingTask?.value
        geometryUploadTask?.cancel(); await geometryUploadTask?.value; geometryUploadTask = nil
        roomProcessingTask?.cancel(); await roomProcessingTask?.value; roomProcessingTask = nil
        liveGeometryUploads.reset(sessionID:"")
        await encodingTask?.value
        arSession?.pause(); roomCaptureSession?.delegate = nil; roomCaptureSession = nil
        arSession = nil; cameraView = nil; overlays.reset(sessionID:"")
        backend = nil; outbox = nil; directory = nil
        sweepID = ""; onboardingID = ""; hasConversation = false; preferences = .null
        captureRequestHandled = false; pendingCaptureRequest = false; canRetryHandoff = false; comparisonRequested = false
        packetFinished = false; downloadedPacket = false; comparisonFinished = false
        packet = .null; comparison = .null; secondLocale = .null; packetURL = nil; reviewURL = nil
        savedFrames = 0; pendingUploads = 0; frameNumber = 0; elapsed = 0
        detectedFloorCount = 0; detectedWallCount = 0; roomScanCoaching = ""
        roomScanStatus = "Room scan starts when you are ready."
        lastFrameTime = -1; captureIssues = []; geometryData = nil; lastRoomUpdate = -Double.infinity; canRetryFinish = false
        transcript = ""; language = ""; country = ""; currency = ""
        await start()
    }

    func foregrounded() {
        isForeground = true
        intelligenceStatus = "OpenAI Realtime"
        if !hasCapture {
            captureMode = CaptureModePolicy.preferred(lidarSupported:lidarSupported)
            if let arSession { arSession.run(cameraConfiguration(mode:captureMode)) }
            else { Task { await prepareCameraPreview() } }
        }
    }

    func interrupted() {
        isForeground = false
        stopRealtimeAudio()
        let connection = voiceConnection
        eventEpoch = UUID(); receiverTask?.cancel(); receiverTask = nil; voiceConnection = nil
        localVoiceStatus = "OpenAI voice stopped"
        Task { [backend] in if let connection { await backend?.closeVoice(connection:connection) } }
        if isCapturing {
            captureIssues.append("App left foreground during the sweep; coverage may be incomplete")
            endCapture()
        } else { arSession?.pause(); overlays.stop() }
    }
}
