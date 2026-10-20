import ARKit
import CoreImage
import ImageIO
import Vision

enum CaptureError: LocalizedError {
    case unsupportedDevice, imageEncoding, permission, noFrames, background, modeMismatch
    var errorDescription: String? {
        switch self {
        case .unsupportedDevice: return "This capture mode needs a supported physical iPhone. Simulator supports conversation only."
        case .imageEncoding: return "Camera evidence could not be encoded."
        case .permission: return "Enable Camera, Microphone and Local Network access in Settings."
        case .background: return "Return to the app and reconnect voice before starting capture."
        case .modeMismatch: return "Capture preferences did not match the confirmed setup. Start a new conversation."
        case .noFrames: return "No camera evidence was captured. Start a new sweep."
        }
    }
}

actor FrameEncoder {
    private let context = CIContext()
    static func depthReadiness(_ frame: ARFrame, objects: [LocalObject]) -> Int {
        guard let scene = frame.sceneDepth ?? frame.smoothedSceneDepth, let confidence = scene.confidenceMap else { return 0 }
        let depth = scene.depthMap
        CVPixelBufferLockBaseAddress(depth,.readOnly); CVPixelBufferLockBaseAddress(confidence,.readOnly)
        defer { CVPixelBufferUnlockBaseAddress(depth,.readOnly); CVPixelBufferUnlockBaseAddress(confidence,.readOnly) }
        guard let d = CVPixelBufferGetBaseAddress(depth), let c = CVPixelBufferGetBaseAddress(confidence) else { return 0 }
        let width = CVPixelBufferGetWidth(depth), height = CVPixelBufferGetHeight(depth)
        guard CVPixelBufferGetWidth(confidence) == width, CVPixelBufferGetHeight(confidence) == height else { return 0 }
        let regions = objects.isEmpty ? [[[0.0,0.0],[1.0,0.0],[1.0,1.0],[0.0,1.0]]] : objects.map(\.polygon)
        var level = 3
        for polygon in regions {
            // Same expanded raw-image neighbourhood and valid range as the server.
            let x0 = max(0,Int(floor((polygon.map{$0[0]}.min() ?? 0)*Double(width)))-4)
            let x1 = min(width,Int(ceil((polygon.map{$0[0]}.max() ?? 1)*Double(width)))+4)
            let y0 = max(0,Int(floor((polygon.map{$0[1]}.min() ?? 0)*Double(height)))-4)
            let y1 = min(height,Int(ceil((polygon.map{$0[1]}.max() ?? 1)*Double(height)))+4)
            var valid = 0
            for y in y0..<y1 {
                let depths = d.advanced(by:y*CVPixelBufferGetBytesPerRow(depth)).assumingMemoryBound(to:Float.self)
                let confidences = c.advanced(by:y*CVPixelBufferGetBytesPerRow(confidence)).assumingMemoryBound(to:UInt8.self)
                for x in x0..<x1 where confidences[x] >= 2 && depths[x].isFinite && depths[x] > 0.15 && depths[x] < 5 { valid += 1 }
            }
            level = min(level,DepthReadiness.level(valid:valid,total:(x1-x0)*(y1-y0)))
        }
        return level
    }
    func encode(_ frame: ARFrame, id: String, elapsed: Double, sessionID: String, mode: CaptureMode,
                recognition: LocalRecognition? = nil, rotation: Int = 90) async throws -> FrameUpload {
        let buffer = frame.capturedImage
        let image = CIImage(cvPixelBuffer: buffer)
        guard let jpeg = context.jpegRepresentation(of: image, colorSpace: CGColorSpaceCreateDeviceRGB(),
                  options: [CIImageRepresentationOption(rawValue: kCGImageDestinationLossyCompressionQuality as String): 0.92]) else { throw CaptureError.imageEncoding }
        let k = frame.camera.intrinsics, t = frame.camera.transform
        var depth = Data(), confidence = Data(), width = 0, height = 0
        if mode == .lidar, let sceneDepth = frame.sceneDepth ?? frame.smoothedSceneDepth, let confidenceMap = sceneDepth.confidenceMap {
            width = CVPixelBufferGetWidth(sceneDepth.depthMap)
            height = CVPixelBufferGetHeight(sceneDepth.depthMap)
            depth = copyRows(sceneDepth.depthMap, elementSize: 4)
            confidence = copyRows(confidenceMap, elementSize: 1)
        }
        let tracking: String
        switch frame.camera.trackingState {
        case .normal: tracking = "normal"
        case .limited: tracking = "limited"
        case .notAvailable: tracking = "unavailable"
        }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = false
        request.automaticallyDetectsLanguage = true
        request.minimumTextHeight = 0.008
        let orientation: CGImagePropertyOrientation = rotation == 90 ? .right : rotation == 180 ? .down : rotation == 270 ? .left : .up
        try? VNImageRequestHandler(cvPixelBuffer: buffer, orientation: orientation).perform([request])
        let ocr: [OCRSpan] = (request.results ?? []).compactMap { observation in
            guard let candidate = observation.topCandidates(1).first, candidate.confidence >= 0.3 else { return nil }
            // Vision uses bottom-left image coordinates; the service uses top-left.
            let point = LocalGeometry.toRaw(x:observation.boundingBox.midX,y:1-observation.boundingBox.midY,rotation:rotation)
            return OCRSpan(text: candidate.string, center:point.map(Float.init), confidence: candidate.confidence)
        }
        var meta = FrameMetadata(id: id, timestamp_s: elapsed,
            image_width: CVPixelBufferGetWidth(buffer), image_height: CVPixelBufferGetHeight(buffer),
            intrinsics: (0..<3).flatMap { c in (0..<3).map { r in k[c][r] } },
            camera_transform: (0..<4).flatMap { c in (0..<4).map { r in t[c][r] } }, tracking: tracking,
            depth_width: width, depth_height: height, ar_session_id: sessionID, ocr: ocr)
        meta.local_recognition = recognition; meta.vision_rotation = rotation
        if sessionID != "diagnostic" {
            meta.local_analysis = await DeviceIntelligence.shared.analyze(ocr:ocr,recognition:recognition)
        }
        return FrameUpload(metadata: meta, jpeg_base64: jpeg.base64EncodedString(),
                           depth_base64: depth.base64EncodedString(), confidence_base64: confidence.base64EncodedString()).applyingCaptureMode(mode)
    }
    private func copyRows(_ buffer: CVPixelBuffer, elementSize: Int) -> Data {
        CVPixelBufferLockBaseAddress(buffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(buffer) else { return Data() }
        let width = CVPixelBufferGetWidth(buffer), height = CVPixelBufferGetHeight(buffer)
        let stride = CVPixelBufferGetBytesPerRow(buffer)
        var output = Data(capacity: width*height*elementSize)
        for row in 0..<height {
            output.append(base.advanced(by: row*stride).assumingMemoryBound(to: UInt8.self), count: width*elementSize)
        }
        return output
    }
}
