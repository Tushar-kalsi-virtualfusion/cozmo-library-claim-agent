import CoreML
import CoreImage
import ImageIO
import CoreVideo

enum LocalDetectorError: LocalizedError {
    case missingModel, incompatibleModel, buffer
    var errorDescription: String? {
        switch self {
        case .missingModel: return "On-phone recognition model is missing from this build."
        case .incompatibleModel: return "On-phone recognition model output is incompatible."
        case .buffer: return "Camera image could not be prepared for recognition."
        }
    }
}

actor LocalObjectDetector {
    private var model: MLModel?
    private var contract: LocalModelContract?
    private var maskOutputName: String?
    private let modelURL: URL?
    private let context = CIContext(options:[.cacheIntermediates:false])
    init(modelURL: URL? = nil) { self.modelURL = modelURL }

    func detect(_ buffer: CVPixelBuffer, rotation: Int) throws -> LocalRecognition {
        try detect(image:CIImage(cvPixelBuffer:buffer),rotation:rotation)
    }

    func detect(image raw: CIImage, rotation: Int) throws -> LocalRecognition {
        if model == nil {
            guard let url = modelURL ?? Bundle.main.url(forResource:"household",withExtension:"mlmodelc") else { throw LocalDetectorError.missingModel }
            let config = MLModelConfiguration(); config.computeUnits = .all
            let loaded = try MLModel(contentsOf:url,configuration:config)
            let metadata = loaded.modelDescription.metadata[.creatorDefinedKey] as? [String:String] ?? [:]
            if let json = metadata["claim_contract"] {
                contract = try LocalModelContract.read(json)
            } else { throw LocalDetectorError.incompatibleModel }
            guard let contract, loaded.modelDescription.inputDescriptionsByName["image"]?.imageConstraint?.pixelsWide == contract.input_size,
                  loaded.modelDescription.inputDescriptionsByName["image"]?.imageConstraint?.pixelsHigh == contract.input_size,
                  loaded.modelDescription.outputDescriptionsByName[contract.output_name]?.multiArrayConstraint?.shape.map(\.intValue) == contract.output_shape else { throw LocalDetectorError.incompatibleModel }
            maskOutputName = loaded.modelDescription.outputDescriptionsByName.first {
                $0.value.multiArrayConstraint?.shape.map(\.intValue) == [1,32,160,160]
            }?.key
            guard contract.output_shape.last != 38 || maskOutputName != nil else { throw LocalDetectorError.incompatibleModel }
            model = loaded
        }
        guard let model, let contract else { throw LocalDetectorError.missingModel }
        let start = ProcessInfo.processInfo.systemUptime
        let orientation: CGImagePropertyOrientation = rotation == 90 ? .right : rotation == 180 ? .down : rotation == 270 ? .left : .up
        let image = raw.oriented(orientation)
        let full = LocalGeometry.suppressDuplicates(try proposals(image,model:model,contract:contract,rotation:rotation))
        var detail: [LocalObject] = []
        if contract.id.hasPrefix("yoloe-") {
            // Two overlapping detail views retain small-object proposals when the
            // full image is reduced to 640 pixels. All model classes are eligible.
            let regions: [[Double]] = image.extent.height >= image.extent.width
                ? [[0,0,1,0.6],[0,0.4,1,0.6]] : [[0,0,0.6,1],[0.4,0,0.6,1]]
            for region in regions {
                let r = CGRect(x:image.extent.minX+region[0]*image.extent.width,
                    y:image.extent.minY+(1-region[1]-region[3])*image.extent.height,
                    width:region[2]*image.extent.width,height:region[3]*image.extent.height)
                let close = try proposals(image.cropped(to:r),model:model,contract:contract,rotation:0)
                detail += close.compactMap { LocalGeometry.fromCrop($0,region:region,rotation:rotation) }
            }
        }
        let quality = quality(image)
        return LocalRecognition(model:contract.id,detections:LocalGeometry.suppressDuplicates(detail,preserving:full),sharpness:quality.0,dark_fraction:quality.1,
            bright_fraction:quality.2,fingerprint:quality.3,inference_ms:(ProcessInfo.processInfo.systemUptime-start)*1000)
    }

    private func proposals(_ image: CIImage, model: MLModel, contract: LocalModelContract, rotation: Int) throws -> [LocalObject] {
        let width = image.extent.width, height = image.extent.height, scale = min(640/width,640/height)
        let fit = image.transformed(by:CGAffineTransform(translationX:-image.extent.minX,y:-image.extent.minY))
            .transformed(by:CGAffineTransform(scaleX:scale,y:scale))
            .transformed(by:CGAffineTransform(translationX:(640-width*scale)/2,y:(640-height*scale)/2))
        let canvas = fit.composited(over:CIImage(color:CIColor(red:114/255,green:114/255,blue:114/255)).cropped(to:CGRect(x:0,y:0,width:640,height:640)))
        var output: CVPixelBuffer?
        guard CVPixelBufferCreate(kCFAllocatorDefault,640,640,kCVPixelFormatType_32BGRA,
            [kCVPixelBufferIOSurfacePropertiesKey:[:]] as CFDictionary,&output) == kCVReturnSuccess,
              let output else { throw LocalDetectorError.buffer }
        context.render(canvas,to:output)
        let result = try model.prediction(from:MLDictionaryFeatureProvider(dictionary:["image":MLFeatureValue(pixelBuffer:output)]))
        guard let tensor = result.featureValue(for:contract.output_name)?.multiArrayValue,
              tensor.shape.map(\.intValue) == contract.output_shape else { throw LocalDetectorError.incompatibleModel }
        let masks = maskOutputName.flatMap { result.featureValue(for:$0)?.multiArrayValue }
            .flatMap { LocalMaskDecoder(tensor:$0) }
        var detections: [LocalObject] = []
        for row in 0..<300 {
            let values = (0..<6).map { tensor[[0,NSNumber(value:row),NSNumber(value:$0)]].doubleValue }
            if var detection = LocalGeometry.decode(values,width:width,height:height,rotation:rotation,
                                                    labels:contract.labels,threshold:contract.confidence_threshold) {
                if let masks, contract.output_shape.last == 38 {
                    let coefficients = (6..<38).map { tensor[[0,NSNumber(value:row),NSNumber(value:$0)]].floatValue }
                    detection.mask = masks.decode(coefficients:coefficients,box:Array(values.prefix(4)),
                        imageWidth:width,imageHeight:height,rotation:rotation)
                }
                detections.append(detection)
            }
        }
        return detections
    }

    private func gray(_ image: CIImage, width: Int, height: Int) -> [UInt8] {
        let fit = image.transformed(by:CGAffineTransform(translationX:-image.extent.minX,y:-image.extent.minY))
            .transformed(by:CGAffineTransform(scaleX:CGFloat(width)/image.extent.width,y:CGFloat(height)/image.extent.height))
        var bytes = [UInt8](repeating:0,count:width*height)
        bytes.withUnsafeMutableBytes { pointer in
            context.render(fit,toBitmap:pointer.baseAddress!,rowBytes:width,bounds:CGRect(x:0,y:0,width:width,height:height),format:.L8,colorSpace:CGColorSpaceCreateDeviceGray())
        }
        return bytes
    }
    private func quality(_ image: CIImage) -> (Double,Double,Double,String) {
        let pixels = gray(image,width:256,height:256)
        var sum = 0.0, squares = 0.0
        for y in 1..<255 { for x in 1..<255 {
            let i = y*256+x
            let lap = Double(-4*Int(pixels[i])+Int(pixels[i-1])+Int(pixels[i+1])+Int(pixels[i-256])+Int(pixels[i+256]))
            sum += lap; squares += lap*lap
        } }
        let count = Double(254*254), small = gray(image,width:9,height:8)
        var hash: UInt64 = 0
        for y in 0..<8 { for x in 0..<8 { hash = (hash << 1) | (small[y*9+x] > small[y*9+x+1] ? 1 : 0) } }
        return (max(0,squares/count-pow(sum/count,2)),Double(pixels.filter{$0<15}.count)/Double(pixels.count),
            Double(pixels.filter{$0>249}.count)/Double(pixels.count),String(format:"%016llx",hash))
    }
}
