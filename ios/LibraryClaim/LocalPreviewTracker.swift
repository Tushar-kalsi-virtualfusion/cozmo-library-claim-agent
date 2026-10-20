import CoreImage
import CoreVideo
import Vision

struct PreviewTrackingSeed {
    let objects: [LocalObject]
    let buffer: CVPixelBuffer
    let timestamp: Double
}

struct PreviewVideoFrame {
    let timestamp: Double
    let image: CVPixelBuffer
}

/// Collection is independent of Vision replay so a busy tracker cannot create
/// holes in the video history that the next detector result needs.
actor PreviewFrameBuffer {
    private let context = CIContext(options:[.cacheIntermediates:false])
    private var history: [PreviewVideoFrame] = []
    func capture(_ buffer: CVPixelBuffer, at time: Double) throws -> [PreviewVideoFrame] {
        guard time.isFinite, time > (history.last?.timestamp ?? -.infinity) else { return history }
        let image = try PreviewImages.thumbnail(buffer,context:context)
        history.append(PreviewVideoFrame(timestamp:time,image:image))
        history.removeAll { time-$0.timestamp > 2 }
        if history.count > 32 { history.removeFirst(history.count-32) }
        return history
    }
}

/// Presentation-only tracking: never changes evidence, identities or measurements.
actor LocalPreviewTracker {
    private var store: PreviewTrackStore
    private var sequence = VNSequenceRequestHandler()
    private var requests: [UUID:VNTrackObjectRequest] = [:]
    private let context = CIContext(options:[.cacheIntermediates:false])
    private var lastFrame = -Double.infinity
    private var lastSeed = -Double.infinity

    init() {
        let request = VNTrackObjectRequest(detectedObjectObservation:VNDetectedObjectObservation(boundingBox:.zero))
        request.revision = VNTrackObjectRequestRevision1
        request.trackingLevel = .fast
        // A temporary detector catch-up sequence shares Vision's tracker pool.
        let supported = request.supportedNumber(ofTrackersAndReturnError:nil)
        store = PreviewTrackStore(limit:min(24,max(1,supported/2)))
    }

    func process(_ history: [PreviewVideoFrame], seed: PreviewTrackingSeed?) throws -> [LocalObject] {
        guard let frame = history.last else { return [] }
        let time = frame.timestamp, image = frame.image
        guard time.isFinite, time > lastFrame else { return store.tracks.map(\.object) }
        let frames = history.filter { $0.timestamp > lastFrame }
        var previous = lastFrame
        let complete = frames.allSatisfy { frame in
            defer { previous = frame.timestamp }
            return frame.timestamp-previous <= 0.25
        }
        // Replay every available intermediate frame, including frames collected
        // while a delayed detection was being processed. Real video gaps reset.
        if !complete {
            store = PreviewTrackStore(limit:store.limit); requests = [:]; sequence = VNSequenceRequestHandler()
        }
        lastFrame = time
        if !requests.isEmpty {
            for frame in frames {
                var positions: [UUID:CGRect] = [:]
                try sequence.perform(Array(requests.values),on:frame.image,orientation:.up)
                for (id,request) in requests {
                    if let observation = request.results?.first as? VNDetectedObjectObservation,
                       observation.confidence >= 0.3 {
                        positions[id] = PreviewGeometry.rawRect(observation.boundingBox)
                        request.inputObservation = observation
                    }
                }
                store.advance(positions,at:frame.timestamp)
            }
        } else { store.advance([:],at:time) }
        let alive = Set(store.tracks.map(\.id))
        requests = requests.filter { alive.contains($0.key) }

        if let seed, seed.timestamp > lastSeed, seed.timestamp <= time, time-seed.timestamp < 2 {
            lastSeed = seed.timestamp
            // Inference finishes on an older image. Track its detections from that
            // exact image to this frame before matching them to the live tracks.
            let objects = try catchUp(seed,history:history,at:time)
            store.merge(objects,capturedAt:seed.timestamp,at:time)
            if !objects.isEmpty {
                requests = [:]; sequence = VNSequenceRequestHandler()
                for track in store.tracks {
                    requests[track.id] = makeRequest(track.rect)
                }
                if !requests.isEmpty {
                    try sequence.perform(Array(requests.values),on:image,orientation:.up)
                    for request in requests.values {
                        if let observation = request.results?.first as? VNDetectedObjectObservation {
                            request.inputObservation = observation
                        }
                    }
                }
            }
        }
        return store.tracks.map(\.object)
    }

    private func catchUp(_ seed: PreviewTrackingSeed, history: [PreviewVideoFrame], at time: Double) throws -> [LocalObject] {
        let candidates = seed.objects.sorted { $0.confidence > $1.confidence }.compactMap { object -> (LocalObject,VNTrackObjectRequest)? in
            guard let rect = PreviewGeometry.rect(object) else { return nil }
            return (object,makeRequest(rect))
        }.prefix(store.limit)
        guard !candidates.isEmpty else { return [] }
        let handler = VNSequenceRequestHandler()
        let batch = candidates.map { $0.1 }
        try handler.perform(batch,on:try PreviewImages.thumbnail(seed.buffer,context:context),orientation:.up)
        var previous = seed.timestamp
        for frame in history where frame.timestamp > seed.timestamp {
            // Missing video cannot be repaired by attaching a stale box to new
            // pixels. Wait for the next supported detection instead.
            guard frame.timestamp-previous <= 0.25 else { return [] }
            for request in batch {
                if let observation = request.results?.first as? VNDetectedObjectObservation { request.inputObservation = observation }
            }
            try handler.perform(batch,on:frame.image,orientation:.up)
            previous = frame.timestamp
        }
        return candidates.compactMap { object,request in
            guard let observation = request.results?.first as? VNDetectedObjectObservation,
                  observation.confidence >= 0.3,
                  let rect = PreviewGeometry.valid(PreviewGeometry.rawRect(observation.boundingBox)) else { return nil }
            return PreviewTrack(id:UUID(),label:object.label,confidence:object.confidence,
                rect:rect,detectedAt:seed.timestamp,trackedAt:time).object
        }
    }

    private func makeRequest(_ rect: CGRect) -> VNTrackObjectRequest {
        let request = VNTrackObjectRequest(detectedObjectObservation:VNDetectedObjectObservation(boundingBox:PreviewGeometry.visionRect(rect)))
        // Revision 2 ignores trackingLevel. Revision 1's fast tracker keeps the
        // live preview responsive; the detector regularly corrects its boxes.
        request.revision = VNTrackObjectRequestRevision1
        request.trackingLevel = .fast
        return request
    }

}

private enum PreviewImages {
    static func thumbnail(_ buffer: CVPixelBuffer, context: CIContext) throws -> CVPixelBuffer {
        let width = CVPixelBufferGetWidth(buffer), height = CVPixelBufferGetHeight(buffer)
        let scale = min(1,640/Double(max(width,height)))
        var output: CVPixelBuffer?
        guard CVPixelBufferCreate(kCFAllocatorDefault,Int(Double(width)*scale),Int(Double(height)*scale),kCVPixelFormatType_32BGRA,
            [kCVPixelBufferIOSurfacePropertiesKey:[:]] as CFDictionary,&output) == kCVReturnSuccess, let output else {
            throw NSError(domain:"PreviewTracker",code:1)
        }
        context.render(CIImage(cvPixelBuffer:buffer).transformed(by:CGAffineTransform(scaleX:scale,y:scale)),to:output)
        return output
    }
}
