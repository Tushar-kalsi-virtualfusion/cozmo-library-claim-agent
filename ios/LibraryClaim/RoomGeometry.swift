import RoomPlan
import simd

enum RoomGeometry {
    static func encode(_ room: CapturedRoom, sessionID: String, issues: [String], final: Bool = true) throws -> Data {
        var notes = issues
        if !final { notes.append("Floor outline and area are finalized when the sweep ends") }
        func surface(_ s: CapturedRoom.Surface, floor: Bool = false) -> [String: Any]? {
            var local = s.polygonCorners
            if s.curve != nil {
                notes.append("Curved surface \(s.identifier) needs review; planar area is not substituted")
                return nil
            }
            if floor {
                guard let corners = RoomSurfaceGeometry.floorCorners(local,transform:s.transform,final:final) else {
                    if final { notes.append("Floor polygon is unavailable; bounding rectangle was not substituted") }
                    return nil
                }
                return ["id":s.identifier.uuidString, "corners":corners, "confidence":String(describing:s.confidence)]
            }
            if local.count < 3 {
                let x = s.dimensions.x/2, y = s.dimensions.y/2
                local = [SIMD3(-x,-y,0), SIMD3(x,-y,0), SIMD3(x,y,0), SIMD3(-x,y,0)]
            }
            let corners = local.map { p -> [Float] in
                let w = s.transform * SIMD4(p.x,p.y,p.z,1)
                return [w.x,w.y,w.z]
            }
            return ["id":s.identifier.uuidString, "corners":corners, "confidence":String(describing:s.confidence)]
        }
        let walls = room.walls.compactMap { surface($0) }
        func closestWall(_ position: SIMD3<Float>) -> String? {
            var nearest: (Float, String)?
            for wall in room.walls {
                let origin = SIMD3(wall.transform.columns.3.x, wall.transform.columns.3.y, wall.transform.columns.3.z)
                let normal = SIMD3(wall.transform.columns.2.x, wall.transform.columns.2.y, wall.transform.columns.2.z)
                let distance = abs(simd_dot(position-origin, normal))
                if distance < 0.35 && (nearest == nil || distance < nearest!.0) { nearest = (distance,wall.identifier.uuidString) }
            }
            return nearest?.1
        }
        let openings = (room.doors + room.windows + room.openings).compactMap { s -> [String: Any]? in
            guard var value = surface(s) else { return nil }
            let t = s.transform.columns.3
            if let wall = closestWall(SIMD3(t.x,t.y,t.z)) { value["wall_id"] = wall }
            else { notes.append("Opening could not be associated with a wall") }
            return value
        }
        let objects: [[String: Any]] = room.objects.map { object in
            let t = object.transform, d = object.dimensions, c = t.columns.3
            var corners: [[Float]] = []
            for x in [Float(-1),Float(1)] { for y in [Float(-1),Float(1)] { for z in [Float(-1),Float(1)] {
                let p = t * SIMD4(x*d.x/2,y*d.y/2,z*d.z/2,1)
                corners.append([p.x,p.y,p.z])
            } } }
            return ["id":object.identifier.uuidString, "category":String(describing:object.category),
                    "center":[c.x,c.y,c.z], "dimensions":[d.x,d.y,d.z], "corners":corners,
                    "confidence":String(describing:object.confidence)]
        }
        let floors = room.floors.compactMap { surface($0, floor:true) }
        let value: [String: Any] = ["ar_session_id":sessionID,
            "floors":floors, "walls":walls,
            "openings":openings, "objects":objects, "shelving":[], "capture_issues":notes,
            "detected_floor_count":room.floors.count, "detected_wall_count":room.walls.count,
            "capture_complete":final,
            "source":final ? "RoomPlan final CapturedRoom from the same ARSession as RGB/depth frames" : "RoomPlan live estimate from the capture ARSession"]
        return try JSONSerialization.data(withJSONObject:value, options:[.sortedKeys])
    }
}
