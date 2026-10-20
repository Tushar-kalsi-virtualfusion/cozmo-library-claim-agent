import Foundation
import CoreImage

/// Opt-in real-photo regression; photo arguments stay outside source control.
@main struct HouseholdModelChecks {
    static func main() async throws {
        guard CommandLine.arguments.count == 4 else { fatalError("Usage: household-check MODEL PHOTO EXPECTED_LABEL") }
        let detector = LocalObjectDetector(modelURL:URL(fileURLWithPath:CommandLine.arguments[1]))
        guard let image = CIImage(contentsOf:URL(fileURLWithPath:CommandLine.arguments[2])) else { fatalError("Missing photo") }
        let result = try await detector.detect(image:image,rotation:90)
        let expected = CommandLine.arguments[3]
        guard result.detections.contains(where:{$0.label == expected}) else {
            print("FAIL: expected \(expected); found \(result.detections.map(\.label))")
            exit(1)
        }
        print("PASS: \(expected) detected on supplied photo with \(result.model)")
    }
}
