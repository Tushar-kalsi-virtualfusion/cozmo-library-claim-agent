import Foundation

struct LocalObjectMask: Sendable {
    let width: Int
    let height: Int
    let alpha: [UInt8]
    let polygon: [[Double]] // texture TL, TR, BR, BL in the original camera image
}

struct LocalObject: Codable, Sendable {
    let label: String
    let confidence: Double
    let polygon: [[Double]] // original AR camera buffer, normalized, top-left origin
    var mask: LocalObjectMask? = nil
    // Preview textures never enter the outbox or duplicate the uploaded JPEG.
    enum CodingKeys: String, CodingKey { case label, confidence, polygon }
}

struct LocalRecognition: Codable, Sendable {
    var model = "unconfigured"
    var detections: [LocalObject]
    var sharpness: Double
    var dark_fraction: Double
    var bright_fraction: Double
    var fingerprint: String
    var inference_ms: Double

    var clarityInstruction: String? {
        if dark_fraction > 0.65 { return "Please add some light so I can see the objects clearly." }
        if bright_fraction > 0.65 { return "Please turn away from the glare so I can see the objects clearly." }
        if sharpness < 35 { return "Please hold the phone steady for a clear photo." }
        if !detections.isEmpty && detections.allSatisfy({ object in
            object.polygon.contains { $0[0]<0.015 || $0[0]>0.985 || $0[1]<0.015 || $0[1]>0.985 }
        }) { return "Please step back a little and show the whole object in the camera." }
        return nil
    }
    var clear: Bool { sharpness >= 35 && dark_fraction < 0.65 && bright_fraction < 0.65 }
}

enum LocalContractError: Error { case invalid }

struct LocalModelContract: Decodable, Sendable {
    let version: Int
    let id: String
    let labels: [String]
    let input_size: Int
    let output_name: String
    let output_shape: [Int]
    let confidence_threshold: Double

    static func read(_ json: String) throws -> LocalModelContract {
        let value = try JSONDecoder().decode(Self.self,from:Data(json.utf8))
        guard value.version == 1, !value.id.isEmpty, value.id.count <= 100,
              !value.labels.isEmpty, value.labels.count <= 8192, Set(value.labels).count == value.labels.count,
              value.labels.allSatisfy({ !$0.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty && $0.count <= 80 }),
              value.input_size == 640, !value.output_name.isEmpty,
              [[1,300,6],[1,300,38]].contains(value.output_shape),
              value.confidence_threshold > 0, value.confidence_threshold <= 1 else { throw LocalContractError.invalid }
        return value
    }
}

enum LocalGeometry {
    static func toRaw(x: Double, y: Double, rotation: Int) -> [Double] {
        let angle = Double(rotation)*Double.pi/180, c = cos(angle).rounded(), s = sin(angle).rounded()
        return [max(0,min(1,(x-0.5)*c+(y-0.5)*s+0.5)),max(0,min(1,-(x-0.5)*s+(y-0.5)*c+0.5))]
    }

    static func decode(_ row: [Double], width: Double, height: Double, rotation: Int,
                       labels: [String], threshold: Double = 0.28) -> LocalObject? {
        guard row.count == 6, row.allSatisfy(\.isFinite), row[4]>=threshold, row[4]<=1,
              row[5]>=0, row[5].rounded() == row[5], row[5]<Double(labels.count), width>0, height>0 else { return nil }
        let scale = min(640/width,640/height), padX = (640-width*scale)/2, padY = (640-height*scale)/2
        let x1 = max(0,min(1,(row[0]-padX)/(width*scale))), y1 = max(0,min(1,(row[1]-padY)/(height*scale)))
        let x2 = max(0,min(1,(row[2]-padX)/(width*scale))), y2 = max(0,min(1,(row[3]-padY)/(height*scale)))
        guard x2-x1>0.005, y2-y1>0.005 else { return nil }
        let polygon = [(x1,y1),(x2,y1),(x2,y2),(x1,y2)].map { toRaw(x:$0.0,y:$0.1,rotation:rotation) }
        return LocalObject(label:labels[Int(row[5])],confidence:row[4],polygon:polygon)
    }

    static func suppressDuplicates(_ objects: [LocalObject], preserving first: [LocalObject] = []) -> [LocalObject] {
        func bounds(_ object: LocalObject) -> [Double] {
            [object.polygon.map{$0[0]}.min()!,object.polygon.map{$0[1]}.min()!,
             object.polygon.map{$0[0]}.max()!,object.polygon.map{$0[1]}.max()!]
        }
        func overlaps(_ a: LocalObject, _ b: LocalObject) -> Bool {
            let x = bounds(a), y = bounds(b)
            let intersection = max(0,min(x[2],y[2])-max(x[0],y[0])) * max(0,min(x[3],y[3])-max(x[1],y[1]))
            let union = (x[2]-x[0])*(x[3]-x[1]) + (y[2]-y[0])*(y[3]-y[1]) - intersection
            return union > 0 && intersection/union > 0.65
        }
        var kept = first
        for object in objects.sorted(by:{$0.confidence > $1.confidence}) {
            if !kept.contains(where:{overlaps(object,$0)}) { kept.append(object) }
        }
        return Array(kept.sorted(by:{$0.confidence > $1.confidence}).prefix(300))
    }

    static func fromCrop(_ object: LocalObject, region: [Double], rotation: Int) -> LocalObject? {
        guard region.count == 4, region.allSatisfy(\.isFinite), region[0]>=0, region[1]>=0,
              region[2]>0, region[3]>0, region[0]+region[2]<=1, region[1]+region[3]<=1,
              object.polygon.count == 4, object.polygon.allSatisfy({$0.count == 2 && $0.allSatisfy({$0.isFinite && $0>0.01 && $0<0.99})}) else { return nil }
        // Reject tile-edge truncation and map upright crop coordinates into the
        // original calibrated AR image, never into a synthetic zoom camera.
        let polygon = object.polygon.map { toRaw(x:region[0]+$0[0]*region[2],y:region[1]+$0[1]*region[3],rotation:rotation) }
        var mapped = LocalObject(label:object.label,confidence:object.confidence,polygon:polygon)
        if let mask = object.mask {
            mapped.mask = LocalObjectMask(width:mask.width,height:mask.height,alpha:mask.alpha,
                polygon:mask.polygon.map { toRaw(x:region[0]+$0[0]*region[2],y:region[1]+$0[1]*region[3],rotation:rotation) })
        }
        return mapped
    }
}

enum LocalPreviewFreshness {
    static func isLive(capturedAt: Double, now: Double, source: [Float], current: [Float]) -> Bool {
        guard capturedAt.isFinite, now.isFinite, now >= capturedAt, now-capturedAt < 2,
              source.count == 16, current.count == 16,
              source.allSatisfy(\.isFinite), current.allSatisfy(\.isFinite) else { return false }
        let distanceSquared = (12...14).reduce(Float(0)) { $0 + pow(source[$1]-current[$1],2) }
        let rotationTrace = [0,1,2,4,5,6,8,9,10].reduce(Float(0)) { $0 + source[$1]*current[$1] }
        return distanceSquared < 0.0004 && (rotationTrace-1)/2 > cos(Float.pi/60)
    }
}

enum CaptureFrameGate {
    static func accepts(frameTime: Double, captureStart: Double, generation: String, currentGeneration: String) -> Bool {
        generation == currentGeneration && frameTime >= captureStart
    }
}

enum DepthReadiness {
    // Zero means absent; levels 1–3 permit bounded retries as usable coverage improves.
    static func level(valid: Int, total: Int) -> Int {
        guard total > 0 else { return 0 }
        guard valid >= 16 else { return 1 }
        let coverage = Double(valid)/Double(total)
        return coverage >= 0.75 ? 3 : coverage >= 0.25 ? 2 : 1
    }
}

struct ViewSelection {
    private var recent: [(hash: UInt64, pose: [Float], sharpness: Double, depthLevel: Int)] = []
    private var lastTime = -Double.infinity
    mutating func reset() { recent = []; lastTime = -Double.infinity }
    func shouldCapture(_ r: LocalRecognition, pose: [Float], time: Double, tracking: Bool, pending: Int, depthLevel: Int = 0) -> Bool {
        guard r.clear, tracking, pending < 4, time-lastTime >= (r.detections.isEmpty ? 10 : 4),
              let hash = UInt64(r.fingerprint,radix:16), pose.count == 16 else { return false }
        for old in recent where (hash ^ old.hash).nonzeroBitCount <= 3 {
            let displacement = (12...14).reduce(Float(0)) { $0 + pow(pose[$1]-old.pose[$1],2) }
            let rotation = [0,1,2,4,5,6,8,9,10].reduce(Float(0)) { $0 + pose[$1]*old.pose[$1] }
            if displacement < 0.0004 && rotation > 2.9987 && r.sharpness <= old.sharpness*1.3 && depthLevel <= old.depthLevel { return false }
        }
        return true
    }
    mutating func record(_ r: LocalRecognition, pose: [Float], time: Double, depthLevel: Int = 0) {
        lastTime = time
        recent.append((UInt64(r.fingerprint,radix:16) ?? 0,pose,r.sharpness,depthLevel))
        if recent.count>120 { recent.removeFirst() }
    }
}
