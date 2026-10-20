import Foundation
import CoreGraphics
import CoreVideo

/// Synthetic moving textured object; no detector or tracker output is mocked.
@main struct PreviewVisionChecks {
    static func frame(offset: Int) -> CVPixelBuffer {
        var result: CVPixelBuffer?
        precondition(CVPixelBufferCreate(kCFAllocatorDefault,320,240,kCVPixelFormatType_32BGRA,
            [kCVPixelBufferIOSurfacePropertiesKey:[:]] as CFDictionary,&result) == kCVReturnSuccess)
        let buffer = result!
        CVPixelBufferLockBaseAddress(buffer,[])
        let bytes = CVPixelBufferGetBaseAddress(buffer)!.assumingMemoryBound(to:UInt8.self)
        let stride = CVPixelBufferGetBytesPerRow(buffer)
        for y in 0..<240 { for x in 0..<320 {
            let i = y*stride+x*4, px = x-offset
            let inside = (60..<140).contains(px) && (65..<145).contains(y)
            let texture = UInt8(40+((px*37+y*53+px*y*3) & 127))
            bytes[i] = inside ? texture : 15
            bytes[i+1] = inside ? 230 : 15
            bytes[i+2] = inside ? 240-texture : 15
            bytes[i+3] = 255
        } }
        CVPixelBufferUnlockBaseAddress(buffer,[])
        return buffer
    }
    static func grid(offset: Int) -> CVPixelBuffer {
        let buffer = frame(offset:400)
        CVPixelBufferLockBaseAddress(buffer,[])
        let bytes = CVPixelBufferGetBaseAddress(buffer)!.assumingMemoryBound(to:UInt8.self)
        let stride = CVPixelBufferGetBytesPerRow(buffer)
        for n in 0..<16 {
            let left = 12+(n%4)*70+offset, top = 8+(n/4)*55
            for y in 0..<38 { for x in 0..<44 {
                let i = (top+y)*stride+(left+x)*4
                let texture = UInt8(40+((x*37+y*53+x*y*3+n*5) & 127))
                bytes[i] = texture; bytes[i+1] = 230; bytes[i+2] = 240-texture
            } }
        }
        CVPixelBufferUnlockBaseAddress(buffer,[])
        return buffer
    }
    static func main() async throws {
        let source = frame(offset:0), tracker = LocalPreviewTracker(), video = PreviewFrameBuffer()
        let object = LocalObject(label:"textured object",confidence:0.9,
            polygon:[[60.0/320,65.0/240],[140.0/320,65.0/240],[140.0/320,145.0/240],[60.0/320,145.0/240]])
        for step in 0..<12 {
            _ = try await tracker.process(try await video.capture(source,at:100+Double(step)/15),seed:nil)
        }
        var elapsed: [Double] = []
        // Detection arrived 0.8 s late. The initial border must be caught up.
        for step in 1...12 {
            let time = 100.8+Double(step-1)/15
            var seed = step == 1 ? PreviewTrackingSeed(objects:[object],buffer:source,timestamp:100)
                : step == 6 ? PreviewTrackingSeed(objects:[],buffer:frame(offset:20),timestamp:101) : nil
            if [4,8,12].contains(step) {
                let dx = Double((step-2)*4)/320
                let previous = LocalObject(label:object.label,confidence:object.confidence,
                    polygon:object.polygon.map { [$0[0]+dx,$0[1]] })
                seed = PreviewTrackingSeed(objects:[previous],buffer:frame(offset:(step-2)*4),timestamp:time-2.0/15)
            }
            let started = ProcessInfo.processInfo.systemUptime
            let objects = try await tracker.process(try await video.capture(frame(offset:step*4),at:time),seed:seed)
            elapsed.append(ProcessInfo.processInfo.systemUptime-started)
            precondition(objects.count == 1, "Moving object lost at frame \(step)")
            let rect = PreviewGeometry.rect(objects[0])!
            let expected = Double(60+step*4)/320
            precondition(abs(rect.minX-expected) < 0.035, "Border failed to follow pixels at frame \(step): \(rect.minX) vs \(expected)")
        }
        let lost = try await tracker.process(try await video.capture(frame(offset:400),at:103),seed:nil)
        precondition(lost.isEmpty, "A long camera interruption must reset tracking")
        let panTracker = LocalPreviewTracker(), panVideo = PreviewFrameBuffer()
        for step in 0..<12 {
            _ = try await panTracker.process(try await panVideo.capture(frame(offset:step*8),at:200+Double(step)/15),seed:nil)
        }
        let panned = try await panTracker.process(try await panVideo.capture(frame(offset:96),at:200.8),
            seed:PreviewTrackingSeed(objects:[object],buffer:source,timestamp:200))
        precondition(panned.count == 1, "A delayed detection must follow intermediate frames during a pan")
        precondition(abs(PreviewGeometry.rect(panned[0])!.minX-156.0/320) < 0.035)
        let crowded = LocalPreviewTracker(), gridVideo = PreviewFrameBuffer(), original = grid(offset:0)
        let objects = (0..<16).map { n -> LocalObject in
            let x = Double(12+(n%4)*70)/320, y = Double(8+(n/4)*55)/240
            return LocalObject(label:"object \(n)",confidence:0.9,polygon:[[x,y],[x+44.0/320,y],
                [x+44.0/320,y+38.0/240],[x,y+38.0/240]])
        }
        for step in 0..<12 { _ = try await crowded.process(try await gridVideo.capture(grid(offset:step),at:300+Double(step)/15),seed:nil) }
        let began = ProcessInfo.processInfo.systemUptime
        let all = try await crowded.process(try await gridVideo.capture(grid(offset:12),at:300.8),
            seed:PreviewTrackingSeed(objects:objects,buffer:original,timestamp:300))
        precondition(all.count == 16, "Crowded view lost valid tracked objects")
        let catchup = ProcessInfo.processInfo.systemUptime-began
        let warm = ProcessInfo.processInfo.systemUptime
        let follow = try await crowded.process(try await gridVideo.capture(grid(offset:13),at:300.87),seed:nil)
        precondition(follow.count == 16)
        print("16-object catch-up \(Int(catchup*1000)) ms; next frame \(Int((ProcessInfo.processInfo.systemUptime-warm)*1000)) ms")
        // Camera collection continues while Vision replays a busy batch. Use
        // actual wall time so processing cannot hide self-created history gaps.
        let liveVideo = PreviewFrameBuffer(), liveTracker = LocalPreviewTracker()
        let epoch = ProcessInfo.processInfo.systemUptime
        var recorded: [PreviewVideoFrame] = []
        for _ in 0..<12 {
            recorded = try await liveVideo.capture(original,at:ProcessInfo.processInfo.systemUptime)
            try await Task.sleep(for:.milliseconds(60))
        }
        let snapshot = recorded
        let first = Task { try await liveTracker.process(snapshot,
            seed:PreviewTrackingSeed(objects:objects,buffer:original,timestamp:epoch)) }
        for _ in 0..<8 {
            recorded = try await liveVideo.capture(original,at:ProcessInfo.processInfo.systemUptime)
            try await Task.sleep(for:.milliseconds(60))
        }
        let firstResult = try await first.value
        precondition(firstResult.count == 16)
        let resumed = try await liveTracker.process(recorded,
            seed:PreviewTrackingSeed(objects:objects,buffer:original,timestamp:snapshot.last!.timestamp))
        precondition(resumed.count == 16, "Busy replay must not interrupt camera history or lose the next refresh")
        print("Vision preview: delayed/repeated detections, 12 moving frames, detector miss and interruption passed; warm mean \(Int(elapsed.dropFirst().reduce(0,+)/11*1000)) ms")
    }
}
