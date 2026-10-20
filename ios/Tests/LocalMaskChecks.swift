import Foundation

@main struct LocalMaskChecks {
    static func main() throws {
        // Two independent prototype channels; only the selected channel may
        // contribute to each object's silhouette (not a union foreground mask).
        let prototypes: [Float] = [8,-8,-8,-8, 8,8,-8,-8, 8,8,8,-8, 8,8,8,8]
            + [-8,-8,-8,8, -8,-8,8,8, -8,8,8,8, 8,8,8,8]
        let decoder = LocalMaskDecoder(prototypes:prototypes,channels:2,width:4,height:4)!
        let mask = decoder.decode(coefficients:[1,0],box:[0,0,640,640],imageWidth:640,imageHeight:640,rotation:0)!
        precondition(mask.alpha == [255,0,0,0,255,255,0,0,255,255,255,0,255,255,255,255])
        let second = decoder.decode(coefficients:[0,1],box:[320,0,640,640],imageWidth:640,imageHeight:640,rotation:0)!
        precondition(second.width == 2 && second.height == 4 && second.alpha == [0,255,255,255,255,255,255,255])
        precondition(second.polygon == [[0.5,0],[1,0],[1,1],[0.5,1]])
        let letterbox = decoder.decode(coefficients:[1,0],box:[0,0,640,640],imageWidth:640,imageHeight:320,rotation:90)!
        precondition(letterbox.width == 4 && letterbox.height == 2)
        precondition(letterbox.polygon == [[0,1],[0,0],[1,0],[1,1]])
        precondition(decoder.decode(coefficients:[.nan,0],box:[0,0,640,640],imageWidth:640,imageHeight:640,rotation:0) == nil)
        precondition(decoder.decode(coefficients:[1],box:[0,0,640,640],imageWidth:640,imageHeight:640,rotation:0) == nil)
        precondition(decoder.decode(coefficients:[0,0],box:[0,0,640,640],imageWidth:640,imageHeight:640,rotation:0) == nil)
        precondition(LocalMaskDecoder(prototypes:[],channels:2,width:4,height:4) == nil)
        var object = LocalObject(label:"arbitrary class",confidence:0.8,polygon:[[0.1,0.1],[0.9,0.1],[0.9,0.9],[0.1,0.9]])
        object.mask = mask
        let mapped = LocalGeometry.fromCrop(object,region:[0,0.4,1,0.6],rotation:90)!
        precondition(mapped.mask?.alpha == mask.alpha)
        precondition(mapped.mask?.polygon == [[0.4,1],[0.4,0],[1,0],[1,1]])
        let json = try JSONSerialization.jsonObject(with:JSONEncoder().encode(object)) as! [String:Any]
        precondition(Set(json.keys) == Set(["label","confidence","polygon"]))
        let decoded = try JSONDecoder().decode(LocalObject.self,from:JSONEncoder().encode(object))
        precondition(decoded.mask == nil)

        let pose: [Float] = [1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1]
        precondition(LocalPreviewFreshness.isLive(capturedAt:100,now:101,source:pose,current:pose))
        precondition(!LocalPreviewFreshness.isLive(capturedAt:100,now:103,source:pose,current:pose))
        precondition(!LocalPreviewFreshness.isLive(capturedAt:100,now:99,source:pose,current:pose))
        var moved = pose; moved[12] = 0.05
        precondition(!LocalPreviewFreshness.isLive(capturedAt:100,now:101,source:pose,current:moved))
        let turned: [Float] = [0.98480775,0,-0.17364818,0,0,1,0,0,0.17364818,0,0.98480775,0,0,0,0,1]
        precondition(!LocalPreviewFreshness.isLive(capturedAt:100,now:101,source:pose,current:turned))
        print("Local masks: instance separation, letterbox/crop/rotation, upload exclusion and moving/stale frame rejection passed")
    }
}
