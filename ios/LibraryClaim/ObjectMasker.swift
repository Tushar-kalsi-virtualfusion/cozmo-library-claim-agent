import Foundation
import Vision
import CoreImage
import ImageIO

struct ForegroundMask: @unchecked Sendable {
    let image: CGImage
    let width: Int
    let height: Int
    let alpha: [UInt8]

    func covers(_ polygon: [[Double]]) -> Bool {
        guard polygon.count == 4 else { return false }
        // Sample the interior, not a corner that could lie outside the silhouette.
        for v in [0.25, 0.5, 0.75] {
            for u in [0.25, 0.5, 0.75] {
                let x = polygon[0][0]*(1-u)*(1-v)+polygon[1][0]*u*(1-v)+polygon[2][0]*u*v+polygon[3][0]*(1-u)*v
                let y = polygon[0][1]*(1-u)*(1-v)+polygon[1][1]*u*(1-v)+polygon[2][1]*u*v+polygon[3][1]*(1-u)*v
                let px = max(0,min(width-1,Int(x*Double(width)))), py = max(0,min(height-1,Int(y*Double(height))))
                if alpha[py*width+px] > 100 { return true }
            }
        }
        return false
    }
}

actor ObjectMasker {
    func segment(jpeg: Data) throws -> ForegroundMask? {
        try Task.checkCancellation()
        guard let source = CGImageSourceCreateWithData(jpeg as CFData,nil),
              let image = CGImageSourceCreateThumbnailAtIndex(source,0,[
                kCGImageSourceCreateThumbnailFromImageAlways:true,
                kCGImageSourceThumbnailMaxPixelSize:1024,
                kCGImageSourceCreateThumbnailWithTransform:false] as CFDictionary) else { return nil }
        let request = VNGenerateForegroundInstanceMaskRequest()
        try VNImageRequestHandler(cgImage:image,orientation:.up).perform([request])
        try Task.checkCancellation()
        guard let observation = request.results?.first, !observation.allInstances.isEmpty else { return nil }
        // One low-resolution texture is shared by all object quads; never retain AR pixel buffers.
        let buffer = try observation.generateMask(forInstances:observation.allInstances)
        CVPixelBufferLockBaseAddress(buffer,.readOnly)
        defer { CVPixelBufferUnlockBaseAddress(buffer,.readOnly) }
        guard CVPixelBufferGetPixelFormatType(buffer) == kCVPixelFormatType_OneComponent32Float,
              let base = CVPixelBufferGetBaseAddress(buffer) else { return nil }
        let width = CVPixelBufferGetWidth(buffer), height = CVPixelBufferGetHeight(buffer)
        let stride = CVPixelBufferGetBytesPerRow(buffer)
        var rgba = [UInt8](repeating:0,count:width*height*4), alpha = [UInt8](repeating:0,count:width*height)
        for y in 0..<height {
            let row = base.advanced(by:y*stride).assumingMemoryBound(to:Float.self)
            for x in 0..<width {
                let value = row[x].isFinite ? max(0,min(1,row[x])) : 0
                let a = UInt8(value*255), index = y*width+x
                alpha[index] = a
                // Premultiplied mint, with opacity applied by the renderer.
                rgba[index*4] = UInt8(Float(a)*0.15); rgba[index*4+1] = a
                rgba[index*4+2] = UInt8(Float(a)*0.65); rgba[index*4+3] = a
            }
        }
        guard let provider = CGDataProvider(data:Data(rgba) as CFData),
              let texture = CGImage(width:width,height:height,bitsPerComponent:8,bitsPerPixel:32,
                bytesPerRow:width*4,space:CGColorSpaceCreateDeviceRGB(),
                bitmapInfo:CGBitmapInfo(rawValue:CGImageAlphaInfo.premultipliedLast.rawValue),
                provider:provider,decode:nil,shouldInterpolate:true,intent:.defaultIntent) else { return nil }
        return ForegroundMask(image:texture,width:width,height:height,alpha:alpha)
    }
}
