import Foundation
import simd

struct ObjectDetection: Identifiable, Sendable {
    let id: String
    let kind: String
    let label: String
    let category: String
    let polygon: [[Double]]
    let worldCorners: [SIMD3<Float>]
    let dimensions: JSONValue
    let measurementSource: String

    init?(_ value: JSONValue) {
        guard let id = value["id"].string, !id.isEmpty else { return nil }
        let polygon = value["polygon"].array.map { $0.array.compactMap(\.number) }
        guard polygon.count == 4, polygon.allSatisfy({ $0.count == 2 && $0.allSatisfy { $0.isFinite && (0...1).contains($0) } }) else { return nil }
        self.id = id; self.polygon = polygon
        kind = value["kind"].string ?? "item"
        category = displayCategory(value["category"].string ?? (kind == "book" ? "Book" : "Object"))
        let title = value["label"].string?.trimmingCharacters(in:.whitespacesAndNewlines) ?? ""
        label = title.isEmpty ? category : title
        let corners = value["world_corners"].array.map { $0.array.compactMap(\.number) }
        worldCorners = corners.count == 4 && corners.allSatisfy({ $0.count == 3 && $0.allSatisfy { $0.isFinite } })
            ? corners.map { SIMD3<Float>(Float($0[0]),Float($0[1]),Float($0[2])) } : []
        dimensions = value["dimensions"]
        measurementSource = value["measurement_source"].string ?? ""
    }

    var dimensionsText: String {
        for (a,b,name) in [("spine_length_cm","spine_thickness_cm","Spine"),("face_long_cm","face_short_cm","Face")] {
            if let x = dimensions[a].number, let y = dimensions[b].number, x > 0, y > 0 {
                return "\(name) \(dimension(x)) × \(dimension(y)) cm"
            }
        }
        return ""
    }
}

struct OverlayFrameGate {
    let sessionID: String
    private(set) var lastTimestamp = -Double.infinity
    mutating func accept(sessionID: String, timestamp: Double) -> Bool {
        guard sessionID == self.sessionID, timestamp.isFinite, timestamp > lastTimestamp else { return false }
        lastTimestamp = timestamp
        return true
    }
}

enum OverlayFreshness {
    static func deadline(capturedAt: Double) -> Double { capturedAt + 15 }
    static func isLive(capturedAt: Double?, now: Double) -> Bool {
        guard let capturedAt, capturedAt.isFinite, now.isFinite else { return false }
        return now >= capturedAt && now < deadline(capturedAt:capturedAt)
    }
}

enum OverlayGeometry {
    static func ray(point: [Double], metadata: FrameMetadata) -> (origin: SIMD3<Float>, direction: SIMD3<Float>)? {
        let k = metadata.intrinsics, t = metadata.camera_transform
        guard point.count == 2, k.count == 9, t.count == 16,
              point.allSatisfy(\.isFinite), k.allSatisfy(\.isFinite), t.allSatisfy(\.isFinite), k[0] > 0, k[4] > 0 else { return nil }
        let camera = SIMD4<Float>((Float(point[0])*Float(metadata.image_width)-k[6])/k[0],
            -(Float(point[1])*Float(metadata.image_height)-k[7])/k[4], -1, 0)
        let matrix = simd_float4x4(columns:(SIMD4(t[0],t[1],t[2],t[3]),SIMD4(t[4],t[5],t[6],t[7]),
            SIMD4(t[8],t[9],t[10],t[11]),SIMD4(t[12],t[13],t[14],t[15])))
        let transformed = matrix * camera
        return (SIMD3(t[12],t[13],t[14]), simd_normalize(SIMD3(transformed.x,transformed.y,transformed.z)))
    }

    static func intersections(polygon: [[Double]], metadata: FrameMetadata, planePoint: SIMD3<Float>, normal: SIMD3<Float>) -> [SIMD3<Float>] {
        guard polygon.count == 4 else { return [] }
        var points: [SIMD3<Float>] = []
        for point in polygon {
            guard let ray = ray(point:point, metadata:metadata) else { return [] }
            let denominator = simd_dot(ray.direction,normal)
            guard abs(denominator) > 0.15 else { return [] }
            let distance = simd_dot(planePoint-ray.origin,normal)/denominator
            guard distance.isFinite, distance > 0.15, distance < 5 else { return [] }
            points.append(ray.origin+ray.direction*distance)
        }
        return points
    }
}
