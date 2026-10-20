import Foundation
import CoreImage

@main struct Smoke {
    static func main() async throws {
        let detector = LocalObjectDetector(modelURL:URL(fileURLWithPath:CommandLine.arguments[1]))
        for path in CommandLine.arguments.dropFirst(2) {
            guard let image = CIImage(contentsOf:URL(fileURLWithPath:path)) else { fatalError("Image missing") }
            let result = try await detector.detect(image:image,rotation:90)
            print(URL(fileURLWithPath:path).lastPathComponent, String(data:try JSONEncoder().encode(result),encoding:.utf8)!)
        }
    }
}
