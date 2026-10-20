import CoreImage
import ImageIO
import Foundation

@main struct LocalMaskModelChecks {
    static func main() async throws {
        let detector = LocalObjectDetector(modelURL:URL(fileURLWithPath:CommandLine.arguments[1]))
        let output = URL(fileURLWithPath:CommandLine.arguments[2],isDirectory:true)
        try FileManager.default.createDirectory(at:output,withIntermediateDirectories:true)
        for path in CommandLine.arguments.dropFirst(3) {
            let image = CIImage(contentsOf:URL(fileURLWithPath:path))!
            let result = try await detector.detect(image:image,rotation:90)
            let masks = result.detections.compactMap(\.mask)
            precondition(!result.detections.isEmpty && !masks.isEmpty,"Detected objects must have instance masks without a server event")
            let stem = URL(fileURLWithPath:path).deletingPathExtension().lastPathComponent
            var report: [[String:Any]] = []
            for (index, object) in result.detections.enumerated() {
                guard let mask = object.mask, let cg = mask.makeImage() else { continue }
                precondition(mask.polygon.count == 4 && mask.alpha.contains(255))
                let file = output.appendingPathComponent("\(stem)-\(index).png")
                let destination = CGImageDestinationCreateWithURL(file as CFURL,"public.png" as CFString,1,nil)!
                CGImageDestinationAddImage(destination,cg,nil)
                precondition(CGImageDestinationFinalize(destination))
                report.append(["label":object.label,"confidence":object.confidence,"polygon":object.polygon,
                    "mask_polygon":mask.polygon,"mask_width":mask.width,"mask_height":mask.height,
                    "mask_pixels":mask.alpha.filter{$0>0}.count,"texture":file.lastPathComponent])
            }
            try JSONSerialization.data(withJSONObject:report,options:[.prettyPrinted,.sortedKeys])
                .write(to:output.appendingPathComponent(stem+".json"))
            print("\(stem): \(result.detections.count) proposals, \(masks.count) instance masks, \(Int(result.inference_ms)) ms")
        }
    }
}
