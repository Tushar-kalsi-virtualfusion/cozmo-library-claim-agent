import SwiftUI
import ARKit
import SceneKit
import Combine

struct PlacedDetection {
    let detection: ObjectDetection
    let corners: [SIMD3<Float>]
    let estimated: Bool
}

@MainActor final class ObjectOverlayController: ObservableObject {
    @Published var detections: [ObjectDetection] = []
    @Published var analyzedImage: UIImage?
    @Published var message = "Point the camera at the room."
    @Published var analyzedFrameID = ""
    @Published var localStatus = "Loading on-phone recognition…"
    @Published var serverStatus = "Server recognition starts with your sweep."
    @Published var depthStatus = "Camera view · dimensions require calibrated depth"
    @Published var furniture: [MeasuredFurniture] = []
    let surface = CameraSurfaceView()
    private var cache: [String: (metadata: FrameMetadata, jpeg: Data, capturedAt: Double)] = [:]
    private var order: [String] = []
    private var cacheBytes = 0
    private var gate = OverlayFrameGate(sessionID:"")

    func attach(view: ARSCNView) { surface.attach(view) }
    func showFurniture(_ values: [MeasuredFurniture]) {
        furniture = values
        surface.showFurniture(values)
    }

    func showLocal(_ result: LocalRecognition, frame: ARFrame) {
        localStatus = "On phone · \(result.detections.count) objects · dotted outlines · \(Int(result.inference_ms)) ms"
        surface.showLocal(result.detections,frame:frame)
    }

    func reset(sessionID: String) {
        gate = OverlayFrameGate(sessionID:sessionID)
        cache.removeAll(); order.removeAll(); cacheBytes = 0
        detections = []; analyzedImage = nil; analyzedFrameID = ""
        furniture = []; surface.clear()
        surface.resumeTracking()
        message = sessionID.isEmpty ? "Camera preview · capture starts when you are ready." : "Looking for objects…"
    }

    func remember(_ frame: FrameUpload, capturedAt: Double) {
        guard frame.metadata.ar_session_id == gate.sessionID,
              let jpeg = Data(base64Encoded:frame.jpeg_base64), cache[frame.metadata.id] == nil else { return }
        cache[frame.metadata.id] = (frame.metadata,jpeg,capturedAt); order.append(frame.metadata.id); cacheBytes += jpeg.count
        while order.count > 64 || cacheBytes > 32_000_000 {
            let first = order.removeFirst()
            cacheBytes -= cache.removeValue(forKey:first)?.1.count ?? 0
        }
    }

    func accept(_ event: JSONValue, session: ARSession) {
        guard let id = event["frame_id"].string,
              let timestamp = event["timestamp_s"].number,
              let sessionID = event["ar_session_id"].string,
              gate.accept(sessionID:sessionID,timestamp:timestamp) else { return }
        let objects = event["detections"].array.prefix(250).compactMap(ObjectDetection.init)
        detections = objects; analyzedFrameID = id
        let saved = cache[id]
        let live = OverlayFreshness.isLive(capturedAt:saved?.capturedAt,now:ProcessInfo.processInfo.systemUptime)
        let deadline = saved.map { OverlayFreshness.deadline(capturedAt:$0.capturedAt) } ?? 0
        analyzedImage = saved.flatMap { UIImage(data:$0.1)?.cgImage }.map { UIImage(cgImage:$0,scale:1,orientation:.right) }
        let placed = objects.map { object -> PlacedDetection in
            guard live else { return PlacedDetection(detection:object,corners:[],estimated:false) }
            var corners = object.worldCorners
            let estimated = corners.isEmpty
            if corners.isEmpty, let meta = saved?.0, meta.tracking == "normal" {
                let center = [object.polygon.map{$0[0]}.reduce(0,+)/4,object.polygon.map{$0[1]}.reduce(0,+)/4]
                if let ray = OverlayGeometry.ray(point:center,metadata:meta) {
                    let query = ARRaycastQuery(origin:ray.origin,direction:ray.direction,allowing:.existingPlaneGeometry,alignment:.any)
                    if let hit = session.raycast(query).first {
                        let t = hit.worldTransform
                        corners = OverlayGeometry.intersections(polygon:object.polygon,metadata:meta,
                            planePoint:SIMD3(t.columns.3.x,t.columns.3.y,t.columns.3.z),
                            normal:SIMD3(t.columns.1.x,t.columns.1.y,t.columns.1.z))
                    }
                }
            }
            return PlacedDetection(detection:object,corners:corners,estimated:estimated)
        }
        // Age is measured from AR capture, never from network arrival.
        surface.show(placed,expiresAt:deadline)
        let anchored = placed.filter { $0.corners.count == 4 }.count
        message = objects.isEmpty ? "No objects found in this frame. Keep scanning." : "\(objects.count) objects · \(anchored) placed in camera · dotted outlines"
    }

    func stop() {
        furniture = []; surface.clear()
        message = "Capture ended · last analyzed frame below"
    }
}

@MainActor final class CameraSurfaceView: UIView {
    private(set) weak var camera: ARSCNView?
    private let drawing = ProjectedObjectsView()
    private var displayLink: CADisplayLink?
    private var observations: [PlacedDetection] = []
    private var expiresAt = 0.0
    private var localObjects: [LocalObject] = []
    private var localFrame: ARFrame?
    private var tracker = LocalPreviewTracker()
    private var frameBuffer = PreviewFrameBuffer()
    private var pendingSeed: PreviewTrackingSeed?
    private var trackingGeneration = UUID()
    private var isTracking = false
    private var isCollecting = false
    private var lastTrackingRequest = -Double.infinity
    private var trackingEnabled = false
    private var furniture: [MeasuredFurniture] = []
    private var furnitureExpiresAt = 0.0

    func attach(_ view: ARSCNView) {
        if camera === view { return }
        camera?.removeFromSuperview()
        camera = view; addSubview(view); addSubview(drawing)
        view.scene = SCNScene()
        drawing.isUserInteractionEnabled = false
        setNeedsLayout()
    }
    override func layoutSubviews() {
        super.layoutSubviews(); camera?.frame = bounds; drawing.frame = bounds
    }
    override func didMoveToWindow() {
        super.didMoveToWindow()
        displayLink?.invalidate(); displayLink = nil
        if window != nil {
            let link = CADisplayLink(target:self,selector:#selector(tick))
            link.preferredFrameRateRange = CAFrameRateRange(minimum:10,maximum:15,preferred:15)
            link.add(to:.main,forMode:.common); displayLink = link
        }
    }
    func clear(all: Bool = true) {
        if all {
            furniture = []; localObjects = []; localFrame = nil; pendingSeed = nil
            trackingGeneration = UUID(); tracker = LocalPreviewTracker(); frameBuffer = PreviewFrameBuffer()
            isTracking = false; isCollecting = false; lastTrackingRequest = -Double.infinity
            trackingEnabled = false
        }
        observations = []
        drawing.entries = []; drawing.setNeedsDisplay()
    }
    func showLocal(_ objects: [LocalObject], frame: ARFrame) {
        trackingEnabled = true
        pendingSeed = PreviewTrackingSeed(objects:objects,buffer:frame.capturedImage,timestamp:frame.timestamp)
        tick()
    }
    func resumeTracking() { trackingEnabled = true }
    func showFurniture(_ values: [MeasuredFurniture]) {
        furniture = values; furnitureExpiresAt = ProcessInfo.processInfo.systemUptime+8
    }
    func show(_ observations: [PlacedDetection], expiresAt: Double) {
        clear(all:false); self.observations = observations; self.expiresAt = expiresAt
        tick()
    }
    @objc private func tick() {
        guard let camera, let frame = camera.session.currentFrame else {
            drawing.entries = []; drawing.setNeedsDisplay(); return
        }
        updateTracking(frame)
        let now = ProcessInfo.processInfo.systemUptime
        let normal: Bool
        if case .normal = frame.camera.trackingState { normal = true } else { normal = false }
        drawing.entries = !normal || now >= expiresAt ? [] : observations.compactMap { object in
            guard object.corners.count == 4 else { return nil }
            let p = object.corners.map { camera.projectPoint(SCNVector3($0.x,$0.y,$0.z)) }
            guard p.allSatisfy({ $0.z > 0 && $0.z < 1 && $0.x.isFinite && $0.y.isFinite }) else { return nil }
            let points = p.map { CGPoint(x:CGFloat($0.x),y:CGFloat($0.y)) }
            let rect = points.reduce(CGRect.null) { $0.union(CGRect(origin:$1,size:CGSize(width:1,height:1))) }
            guard bounds.intersects(rect) else { return nil }
            return ProjectedObjectsView.Entry(points:points,label:object.detection.label,
                dimensions:object.detection.dimensionsText,estimated:object.estimated)
        }
        if normal, now < furnitureExpiresAt {
            drawing.entries += furniture.compactMap { object in
                let p = object.top.map { camera.projectPoint(SCNVector3($0.x,$0.y,$0.z)) }
                guard p.allSatisfy({$0.z > 0 && $0.z < 1 && $0.x.isFinite && $0.y.isFinite}) else { return nil }
                return ProjectedObjectsView.Entry(points:p.map { CGPoint(x:CGFloat($0.x),y:CGFloat($0.y)) },
                    label:object.category+" · LiDAR",dimensions:object.dimensionsText,estimated:false)
            }
        }
        // These positions were tracked in recent video pixels, independently of
        // AR plane/depth tracking. An old detector frame is never drawn directly.
        if let source = localFrame, now >= source.timestamp, now-source.timestamp < 0.35 {
            let orientation = window?.windowScene?.effectiveGeometry.interfaceOrientation ?? .portrait
            let transform = source.displayTransform(for:orientation,viewportSize:bounds.size)
            drawing.entries += localObjects.map { object in
                let points = object.polygon.map { point -> CGPoint in
                    let p = CGPoint(x:point[0],y:point[1]).applying(transform)
                    return CGPoint(x:p.x*bounds.width,y:p.y*bounds.height)
                }
                return ProjectedObjectsView.Entry(points:points,label:displayCategory(object.label),
                    dimensions:"",estimated:true)
            }
        }
        drawing.setNeedsDisplay()
    }

    private func updateTracking(_ frame: ARFrame) {
        guard trackingEnabled, !isCollecting, frame.timestamp > lastTrackingRequest else { return }
        isCollecting = true; lastTrackingRequest = frame.timestamp
        let generation = trackingGeneration, collector = frameBuffer
        Task { [weak self] in
            do {
                let history = try await collector.capture(frame.capturedImage,at:frame.timestamp)
                guard let self, self.trackingGeneration == generation else { return }
                self.isCollecting = false
                guard !self.isTracking else { return }
                self.isTracking = true
                let seed = self.pendingSeed; self.pendingSeed = nil
                let worker = self.tracker
                do {
                    let objects = try await worker.process(history,seed:seed)
                    guard self.trackingGeneration == generation else { return }
                    self.localObjects = objects; self.localFrame = frame; self.isTracking = false
                } catch {
                    guard self.trackingGeneration == generation else { return }
                    self.isTracking = false
                }
            } catch {
                guard let self, self.trackingGeneration == generation else { return }
                self.isCollecting = false
            }
        }
    }
}

@MainActor final class ProjectedObjectsView: UIView {
    struct Entry {
        let points: [CGPoint]
        let label: String
        let dimensions: String
        let estimated: Bool
    }
    var entries: [Entry] = []
    override init(frame: CGRect) { super.init(frame:frame); isOpaque = false; backgroundColor = .clear }
    required init?(coder: NSCoder) { super.init(coder:coder); isOpaque = false }
    override func draw(_ rect: CGRect) {
        guard let context = UIGraphicsGetCurrentContext() else { return }
        for entry in entries where entry.points.count == 4 {
            let path = UIBezierPath(); path.move(to:entry.points[0]); entry.points.dropFirst().forEach { path.addLine(to:$0) }; path.close()
            (entry.estimated ? UIColor.systemYellow : UIColor.systemMint).setStroke(); path.lineWidth = 2
            path.lineCapStyle = .round
            path.setLineDash([0,6],count:2,phase:0)
            path.stroke()
            let text = [entry.label,entry.dimensions].filter { !$0.isEmpty }.joined(separator:"\n")
            let attributes: [NSAttributedString.Key:Any] = [.font:UIFont.systemFont(ofSize:11,weight:.semibold),.foregroundColor:UIColor.white]
            let size = (text as NSString).boundingRect(with:CGSize(width:155,height:42),options:[.usesLineFragmentOrigin,.truncatesLastVisibleLine],attributes:attributes,context:nil).size
            let x = max(2,min(bounds.width-size.width-10,entry.points.map(\.x).min() ?? 0))
            let y = max(2,min(bounds.height-size.height-10,(entry.points.map(\.y).min() ?? 0)-size.height-8))
            context.setFillColor(UIColor.black.withAlphaComponent(0.72).cgColor)
            context.fill(CGRect(x:x,y:y,width:size.width+8,height:size.height+6))
            (text as NSString).draw(in:CGRect(x:x+4,y:y+3,width:size.width+1,height:size.height),withAttributes:attributes)
        }
    }
}

struct ObjectCameraPreview: UIViewRepresentable {
    let surface: CameraSurfaceView
    func makeUIView(context: Context) -> CameraSurfaceView { surface }
    func updateUIView(_ uiView: CameraSurfaceView, context: Context) {}
}

struct AnalyzedFrameView: View {
    @ObservedObject var overlays: ObjectOverlayController
    var body: some View {
        if let image = overlays.analyzedImage {
            GeometryReader { proxy in
                let height = proxy.size.width*image.size.height/image.size.width
                ZStack(alignment:.topLeading) {
                    Image(uiImage:image).resizable().aspectRatio(contentMode:.fit)
                    ForEach(overlays.detections) { detection in
                        let points = detection.polygon.map { CGPoint(x:(1-$0[1])*proxy.size.width,y:$0[0]*height) }
                        let path = Path { p in p.move(to:points[0]); points.dropFirst().forEach { p.addLine(to:$0) }; p.closeSubpath() }
                        path.stroke(.mint,style:StrokeStyle(lineWidth:2,lineCap:.round,dash:[0,6]))
                        Text([detection.label,detection.dimensionsText].filter { !$0.isEmpty }.joined(separator:"\n"))
                            .font(.system(size:10,weight:.semibold)).foregroundStyle(.white)
                            .padding(3).background(.black.opacity(0.75))
                            .position(x:max(70,min(proxy.size.width-70,points.map(\.x).min() ?? 70)),
                                      y:max(18,min(height-18,points.map(\.y).min() ?? 18)))
                    }
                }
            }.aspectRatio(image.size.width/image.size.height,contentMode:.fit)
        }
    }
}
