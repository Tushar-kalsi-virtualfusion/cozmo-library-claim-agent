import Accelerate
import CoreML
import CoreGraphics
import Foundation

/// Class-independent YOLO instance segmentation: coefficients × prototypes > 0.
/// Each texture contains only its instance and is cropped before rendering.
struct LocalMaskDecoder {
    private let prototypes: [Float]
    private let channels: Int
    private let width: Int
    private let height: Int

    init?(prototypes: [Float], channels: Int, width: Int, height: Int) {
        guard (1...256).contains(channels), (1...512).contains(width), (1...512).contains(height),
              prototypes.count == channels*width*height else { return nil }
        self.prototypes = prototypes; self.channels = channels; self.width = width; self.height = height
    }

    init?(tensor: MLMultiArray) {
        let shape = tensor.shape.map(\.intValue), strides = tensor.strides.map(\.intValue)
        guard shape == [1,32,160,160] else { return nil }
        let values: [Float]
        if tensor.dataType == .float32, Array(strides.suffix(3)) == [25600,160,1] {
            values = Array(UnsafeBufferPointer(start:tensor.dataPointer.assumingMemoryBound(to:Float.self),count:819200))
        } else {
            values = (0..<32).flatMap { c in (0..<160).flatMap { y in
                (0..<160).map { x in tensor[[0,NSNumber(value:c),NSNumber(value:y),NSNumber(value:x)]].floatValue }
            } }
        }
        self.init(prototypes:values,channels:32,width:160,height:160)
    }

    func decode(coefficients: [Float], box: [Double], imageWidth: Double, imageHeight: Double, rotation: Int) -> LocalObjectMask? {
        guard coefficients.count == channels, coefficients.allSatisfy(\.isFinite),
              box.count == 4, box.allSatisfy(\.isFinite), imageWidth.isFinite, imageHeight.isFinite,
              imageWidth > 0, imageHeight > 0 else { return nil }
        let scale = min(640/imageWidth,640/imageHeight)
        let contentW = imageWidth*scale, contentH = imageHeight*scale
        let padX = (640-contentW)/2, padY = (640-contentH)/2
        let left = max(padX,box[0]), top = max(padY,box[1])
        let right = min(640-padX,box[2]), bottom = min(640-padY,box[3])
        guard right > left, bottom > top else { return nil }
        // Same prototype-pixel crop convention as Ultralytics process_mask.
        let x0 = max(0,Int(ceil(left*Double(width)/640))), y0 = max(0,Int(ceil(top*Double(height)/640)))
        let x1 = min(width,Int(ceil(right*Double(width)/640))), y1 = min(height,Int(ceil(bottom*Double(height)/640)))
        guard x1 > x0, y1 > y0 else { return nil }
        var logits = [Float](repeating:0,count:width*height)
        vDSP_mmul(coefficients,1,prototypes,1,&logits,1,1,vDSP_Length(width*height),vDSP_Length(channels))
        var alpha = [UInt8](); alpha.reserveCapacity((x1-x0)*(y1-y0))
        for y in y0..<y1 { for x in x0..<x1 {
            let n = logits[y*width+x]
            alpha.append(n.isFinite && n > 0 ? 255 : 0)
        } }
        guard alpha.contains(255) else { return nil }
        let polygon = [(x0,y0),(x1,y0),(x1,y1),(x0,y1)].map { x,y in
            LocalGeometry.toRaw(x:(Double(x)*640/Double(width)-padX)/contentW,
                y:(Double(y)*640/Double(height)-padY)/contentH,rotation:rotation)
        }
        return LocalObjectMask(width:x1-x0,height:y1-y0,alpha:alpha,polygon:polygon)
    }
}

extension LocalObjectMask {
    func makeImage() -> CGImage? {
        guard width > 0, height > 0, alpha.count == width*height else { return nil }
        var rgba = [UInt8](repeating:0,count:width*height*4)
        for i in alpha.indices {
            let a = alpha[i]
            rgba[i*4] = UInt8(Float(a)*0.15); rgba[i*4+1] = a
            rgba[i*4+2] = UInt8(Float(a)*0.65); rgba[i*4+3] = a
        }
        guard let provider = CGDataProvider(data:Data(rgba) as CFData) else { return nil }
        return CGImage(width:width,height:height,bitsPerComponent:8,bitsPerPixel:32,bytesPerRow:width*4,
            space:CGColorSpaceCreateDeviceRGB(),bitmapInfo:CGBitmapInfo(rawValue:CGImageAlphaInfo.premultipliedLast.rawValue),
            provider:provider,decode:nil,shouldInterpolate:false,intent:.defaultIntent)
    }
}
