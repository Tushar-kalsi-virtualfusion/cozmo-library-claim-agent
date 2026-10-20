import Foundation
import simd

@main struct OverlayChecks {
    static func main() throws {
        let book = InventoryRow(value:.object(["id":.string("b1"), "title":.string("")]))
        precondition(book.title == "Book")
        let item = InventoryRow(value:.object(["id":.string("i1"), "category":.string("desk lamp")]))
        precondition(item.title == "Desk lamp")
        precondition(book.dimensionsText == "Dimensions not measured")
        let measured = InventoryRow(value:.object(["spine_height_cm":.number(20), "spine_thickness_cm":.number(3)]))
        precondition(measured.dimensionsText == "Spine 20 × 3 cm")
        let partial = InventoryRow(value:.object(["category":.string("chair"), "dimensions_cm":.object(["w":.number(40), "h":.null, "d":.number(30)])]))
        precondition(partial.dimensionsText.contains("W 40") && partial.dimensionsText.contains("D 30"))
        precondition(!partial.dimensionsText.contains("H 0"))
        var gate = OverlayFrameGate(sessionID:"current")
        precondition(!gate.accept(sessionID:"old", timestamp:100))
        precondition(gate.accept(sessionID:"current", timestamp:5))
        precondition(!gate.accept(sessionID:"current", timestamp:4))
        precondition(!gate.accept(sessionID:"current", timestamp:5))
        precondition(!gate.accept(sessionID:"current", timestamp:.nan))
        precondition(OverlayFreshness.deadline(capturedAt:100) == 115)
        precondition(OverlayFreshness.isLive(capturedAt:100,now:110))
        precondition(!OverlayFreshness.isLive(capturedAt:100,now:120))
        precondition(!OverlayFreshness.isLive(capturedAt:nil,now:110))
        precondition(!OverlayFreshness.isLive(capturedAt:100,now:99))
        let meta = FrameMetadata(id:"f1",timestamp_s:1,image_width:1000,image_height:1000,
            intrinsics:[1000,0,0,0,1000,0,500,500,1],
            camera_transform:[1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1],tracking:"normal",
            depth_width:0,depth_height:0,ar_session_id:"current",ocr:[])
        let polygon = [[0.4,0.4],[0.6,0.4],[0.6,0.6],[0.4,0.6]]
        let quad = OverlayGeometry.intersections(polygon:polygon, metadata:meta,
            planePoint:SIMD3<Float>(0,0,-2), normal:SIMD3<Float>(0,0,1))
        precondition(quad.count == 4)
        precondition(simd_distance(quad[0], SIMD3<Float>(-0.2,0.2,-2)) < 0.0001)
        precondition(simd_distance(quad[2], SIMD3<Float>(0.2,-0.2,-2)) < 0.0001)
        precondition(OverlayGeometry.intersections(polygon:polygon, metadata:meta,
            planePoint:SIMD3<Float>(0,0,2), normal:SIMD3<Float>(0,0,1)).isEmpty)
        let raw: JSONValue = .object(["id":.string("b1"),"kind":.string("book"),"label":.string(""),
            "polygon":.array(polygon.map { .array($0.map { .number($0) }) }),
            "dimensions":.object(["face_long_cm":.number(30),"face_short_cm":.number(20)])])
        let detection = ObjectDetection(raw)
        precondition(detection?.label == "Book")
        precondition(detection?.dimensionsText == "Face 30 × 20 cm")
        precondition(ObjectDetection(.object(["polygon":.array([])])) == nil)
        print("Overlay checks passed: labels, unknown dimensions, session ordering and calibrated plane projection")
    }
}
