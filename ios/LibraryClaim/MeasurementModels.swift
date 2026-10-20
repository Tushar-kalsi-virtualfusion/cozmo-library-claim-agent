import Foundation
import simd

enum RoomSurfaceGeometry {
    /// RoomPlan's live rectangle is not the final floor outline or its area.
    static func floorCorners(_ local: [SIMD3<Float>], transform: simd_float4x4, final: Bool) -> [[Float]]? {
        guard final, local.count >= 3, local.allSatisfy({ $0.x.isFinite && $0.y.isFinite && $0.z.isFinite }) else { return nil }
        let corners = local.map { point -> [Float] in
            let world = transform * SIMD4(point.x,point.y,point.z,1)
            return [world.x,world.y,world.z]
        }
        return corners.allSatisfy({ $0.allSatisfy(\.isFinite) }) ? corners : nil
    }
}

/// One latest live snapshot, admitted only after a frame established server identity.
/// Finalization drains the current request and forbids every later live write.
struct LiveGeometryUploadGate {
    private var sessionID = ""
    private var acceptedFrame = false
    private var latest: Data?
    private var inFlight: Data?
    private var finalizing = false

    mutating func reset(sessionID: String) { self = Self(); self.sessionID = sessionID }
    mutating func offer(_ data: Data, sessionID: String) {
        guard !sessionID.isEmpty, sessionID == self.sessionID, !finalizing else { return }
        latest = data
    }
    mutating func frameAccepted(sessionID: String) {
        guard !sessionID.isEmpty, sessionID == self.sessionID else { return }
        acceptedFrame = true
    }
    mutating func next() -> Data? {
        guard acceptedFrame, !finalizing, inFlight == nil, let data = latest else { return nil }
        latest = nil; inFlight = data
        return data
    }
    mutating func completed(_ data: Data, sessionID: String, succeeded: Bool) {
        guard sessionID == self.sessionID, inFlight == data else { return }
        inFlight = nil
        if !succeeded, !finalizing, latest == nil { latest = data }
    }
    mutating func beginFinalization() { finalizing = true; latest = nil }
}

struct MeasuredFurniture: Identifiable {
    let id: String
    let category: String
    let width: Double
    let height: Double
    let depth: Double
    let corners: [SIMD3<Float>]
    init?(_ object: JSONValue) {
        let d = object["dimensions"].array.compactMap(\.number)
        let c = object["corners"].array.map { $0.array.compactMap(\.number) }
        guard let id = object["id"].string, !id.isEmpty,
              ["high","medium"].contains(object["confidence"].string ?? ""),
              d.count == 3, d.allSatisfy({$0.isFinite && $0 > 0.01 && $0 < 20}),
              c.count == 8, c.allSatisfy({$0.count == 3 && $0.allSatisfy(\.isFinite)}) else { return nil }
        self.id = id; category = displayCategory(object["category"].string ?? "Furniture")
        width = d[0]; height = d[1]; depth = d[2]
        corners = c.map { SIMD3(Float($0[0]),Float($0[1]),Float($0[2])) }
    }
    var dimensionsText: String {
        "L \(dimension(max(width,depth)*100)) · W \(dimension(min(width,depth)*100)) · H \(dimension(height*100)) cm"
    }
    var top: [SIMD3<Float>] { [2,6,7,3].map { corners[$0] } }
}

struct RoomMeasurementSummary {
    let value: JSONValue
    var size: String {
        [("length_m","L"),("width_m","W"),("height_m","H")].map { key,label in
            let n = value[key].number
            return label + " " + (n.flatMap { $0.isFinite && $0 > 0 ? String(format:"%.2f m",$0) : nil } ?? "pending")
        }.joined(separator:" · ")
    }
    var area: String {
        guard let n = value["floor_area_m2"].number, n.isFinite, n > 0 else { return "Floor area pending" }
        return String(format:"Floor %.2f m² · %.1f ft²",n,n*10.76391041671)
    }
}
