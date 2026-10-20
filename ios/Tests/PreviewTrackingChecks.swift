import Foundation
import CoreGraphics

@main struct PreviewTrackingChecks {
    static func object(_ x: Double, label: String = "table") -> LocalObject {
        LocalObject(label:label,confidence:0.8,polygon:[[x,0.2],[x+0.3,0.2],[x+0.3,0.5],[x,0.5]])
    }
    static func main() {
        var store = PreviewTrackStore()
        store.merge([object(0.1)],capturedAt:100,at:100.8)
        precondition(store.tracks.count == 1)
        let id = store.tracks[0].id
        // A camera pan moves the image: the same border must follow that motion.
        let moved = CGRect(x:0.22,y:0.2,width:0.3,height:0.3)
        store.advance([id:moved],at:100.9)
        precondition(store.tracks[0].id == id && abs(store.tracks[0].rect.minX-0.22) < 1e-9)
        // An empty inference must not erase a still-confident visual track.
        store.merge([],capturedAt:100.2,at:100.9)
        precondition(store.tracks.count == 1 && store.tracks[0].id == id)
        store.advance([id:moved],at:101)
        store.merge([object(0.225)],capturedAt:100.3,at:101)
        precondition(store.tracks.count == 1 && store.tracks[0].id == id)
        precondition(abs(store.tracks[0].rect.minX-0.22) < 0.003, "Detector jitter must be damped")
        // Adjacent same-class objects remain distinct; one cannot steal two matches.
        store.merge([object(0.225),object(0.65)],capturedAt:100.4,at:101.1)
        precondition(store.tracks.count == 2)
        // Old/future inference must never rewind the display.
        store.merge([object(0.01)],capturedAt:100.1,at:101.1)
        store.merge([object(0.01)],capturedAt:102,at:101.1)
        precondition(store.tracks.count == 2)
        store.advance([:],at:101.2)
        precondition(store.tracks.count == 2, "A single weak tracker frame gets a short grace period")
        store.advance([:],at:101.5)
        precondition(store.tracks.isEmpty, "Lost objects must not leave frozen borders")

        store.merge([object(0.1)],capturedAt:102,at:102)
        let next = store.tracks[0].id
        for i in 1...40 { store.advance([next:moved],at:102+Double(i)/10) }
        precondition(store.tracks.isEmpty, "Visual drift cannot survive indefinitely without detection")
        store.merge([object(0.1)],capturedAt:102.1,at:106)
        precondition(store.tracks.isEmpty, "Overdue inference is discarded")
        store.merge([LocalObject(label:"invalid",confidence:1,polygon:[[.nan,0]])],capturedAt:106,at:106)
        precondition(store.tracks.isEmpty)
        let raw = CGRect(x:0.1,y:0.2,width:0.3,height:0.4)
        let vision = PreviewGeometry.visionRect(raw)
        precondition(abs(vision.minY-0.4) < 1e-9)
        precondition(abs(PreviewGeometry.rawRect(vision).minY-raw.minY) < 1e-9)
        precondition(abs(PreviewGeometry.rect(object(0.1))!.width-0.3) < 1e-9)
        print("Preview tracking: moving borders, missed detections, stable identity, jitter, lost/stale expiry and coordinates passed")
    }
}
