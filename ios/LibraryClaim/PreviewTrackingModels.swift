import Foundation
import CoreGraphics

enum PreviewGeometry {
    static func rect(_ object: LocalObject) -> CGRect? {
        guard object.polygon.count == 4,
              object.polygon.allSatisfy({ $0.count == 2 && $0.allSatisfy(\.isFinite) }) else { return nil }
        let x = object.polygon.map { $0[0] }, y = object.polygon.map { $0[1] }
        return valid(CGRect(x:x.min()!,y:y.min()!,width:x.max()!-x.min()!,height:y.max()!-y.min()!))
    }
    static func valid(_ rect: CGRect) -> CGRect? {
        guard [rect.minX,rect.minY,rect.width,rect.height].allSatisfy(\.isFinite),
              rect.width > 0.005, rect.height > 0.005 else { return nil }
        let clipped = rect.intersection(CGRect(x:0,y:0,width:1,height:1))
        guard !clipped.isNull, clipped.width > 0.005, clipped.height > 0.005,
              clipped.width*clipped.height >= rect.width*rect.height*0.5 else { return nil }
        return clipped
    }
    static func visionRect(_ raw: CGRect) -> CGRect {
        CGRect(x:raw.minX,y:1-raw.maxY,width:raw.width,height:raw.height)
    }
    static func rawRect(_ vision: CGRect) -> CGRect { visionRect(vision) }
    static func overlap(_ a: CGRect, _ b: CGRect) -> CGFloat {
        let i = a.intersection(b)
        if i.isNull { return 0 }
        let area = i.width*i.height
        return area/(a.width*a.height+b.width*b.height-area)
    }
}

struct PreviewTrack: Sendable {
    let id: UUID
    var label: String
    var confidence: Double
    var rect: CGRect
    var detectedAt: Double
    var trackedAt: Double
    var object: LocalObject {
        LocalObject(label:label,confidence:confidence,polygon:[
            [rect.minX,rect.minY],[rect.maxX,rect.minY],[rect.maxX,rect.maxY],[rect.minX,rect.maxY]])
    }
}

struct PreviewTrackStore {
    private(set) var tracks: [PreviewTrack] = []
    private var lastDetection = -Double.infinity
    private var lastFrame = -Double.infinity
    let limit: Int
    init(limit: Int = 24) { self.limit = max(1,limit) }
    mutating func merge(_ objects: [LocalObject], capturedAt: Double, at time: Double) {
        guard capturedAt.isFinite, time.isFinite, capturedAt > lastDetection,
              time >= capturedAt, time-capturedAt < 2, time >= lastFrame else { return }
        lastDetection = capturedAt
        expire(at:time)
        var matched = Set<UUID>()
        for object in objects.sorted(by:{ $0.confidence > $1.confidence }) {
            guard object.confidence.isFinite, (0...1).contains(object.confidence),
                  let rect = PreviewGeometry.rect(object) else { continue }
            let match = tracks.indices.filter { !matched.contains(tracks[$0].id) }
                .max { PreviewGeometry.overlap(tracks[$0].rect,rect) < PreviewGeometry.overlap(tracks[$1].rect,rect) }
            if let index = match, PreviewGeometry.overlap(tracks[index].rect,rect) > 0.35 {
                let previous = tracks[index].rect
                // Both boxes describe this same video frame. Blend detector noise,
                // never blend a past camera position into the current image.
                tracks[index].rect = CGRect(x:previous.minX*0.75+rect.minX*0.25,
                    y:previous.minY*0.75+rect.minY*0.25,
                    width:previous.width*0.75+rect.width*0.25,height:previous.height*0.75+rect.height*0.25)
                if object.label == tracks[index].label || object.confidence > tracks[index].confidence+0.15 {
                    tracks[index].label = object.label; tracks[index].confidence = object.confidence
                }
                tracks[index].detectedAt = capturedAt; tracks[index].trackedAt = time
                matched.insert(tracks[index].id)
            } else if tracks.count < limit {
                let track = PreviewTrack(id:UUID(),label:object.label,confidence:object.confidence,
                    rect:rect,detectedAt:capturedAt,trackedAt:time)
                tracks.append(track); matched.insert(track.id)
            }
        }
    }
    mutating func advance(_ observations: [UUID:CGRect], at time: Double) {
        guard time.isFinite, time > lastFrame else { return }
        lastFrame = time
        for index in tracks.indices {
            if let observation = observations[tracks[index].id], let rect = PreviewGeometry.valid(observation) {
                tracks[index].rect = rect; tracks[index].trackedAt = time
            }
        }
        expire(at:time)
    }
    private mutating func expire(at time: Double) {
        tracks.removeAll { time-$0.trackedAt >= 0.3 || time-$0.detectedAt >= 3.5 }
    }
}
