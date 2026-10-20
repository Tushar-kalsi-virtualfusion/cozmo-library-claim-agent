import Foundation

@main struct CaptureModeChecks {
    static func main() throws {
        let metadata = FrameMetadata(id:"same-frame",timestamp_s:2,image_width:1920,image_height:1440,
            intrinsics:[1,0,0,0,1,0,0,0,1],camera_transform:Array(repeating:0,count:16),tracking:"normal",
            depth_width:256,depth_height:192,ar_session_id:"same-session",ocr:[])
        let original = FrameUpload(metadata:metadata,jpeg_base64:"same-jpeg",depth_base64:"private-depth",confidence_base64:"private-confidence")
        let camera = original.applyingCaptureMode(.camera)
        let lidar = original.applyingCaptureMode(.lidar)
        precondition(camera.metadata.depth_width == 0 && camera.metadata.depth_height == 0)
        precondition(camera.depth_base64.isEmpty && camera.confidence_base64.isEmpty)
        precondition(camera.jpeg_base64 == original.jpeg_base64)
        precondition(camera.metadata.intrinsics == original.metadata.intrinsics)
        precondition(camera.metadata.camera_transform == original.metadata.camera_transform)
        precondition(camera.metadata.id == original.metadata.id && camera.metadata.ar_session_id == original.metadata.ar_session_id)
        precondition(lidar.depth_base64 == original.depth_base64 && lidar.metadata.depth_width == 256)
        let encoded = try JSONEncoder().encode(camera)
        precondition(!String(decoding:encoded,as:UTF8.self).contains("private-depth"))
        print("Capture modality checks passed: camera strips depth/confidence and preserves JPEG/calibration/provenance; LiDAR retains depth")
    }
}
