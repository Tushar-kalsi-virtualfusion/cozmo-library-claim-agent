import Foundation

@main struct Checks {
    static func main() {
        let point = LocalGeometry.toRaw(x:0.2,y:0.3,rotation:90)
        precondition(abs(point[0]-0.3)<0.0001 && abs(point[1]-0.8)<0.0001)
        let box = LocalGeometry.decode([160,80,480,560,0.8,0],width:640,height:480,rotation:0,labels:["chair"])
        precondition(box?.label == "chair")
        precondition(abs(box!.polygon[0][1])<0.0001 && abs(box!.polygon[2][1]-1)<0.0001)
        precondition(LocalGeometry.decode([1,1,1,1,0.8,0],width:640,height:480,rotation:0,labels:["chair"]) == nil)
        precondition(LocalGeometry.decode([0,0,640,640,0.1,0],width:640,height:480,rotation:0,labels:["chair"]) == nil)
        var policy = ViewSelection()
        let pose: [Float] = [1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1]
        var r = LocalRecognition(detections:[box!],sharpness:100,dark_fraction:0,bright_fraction:0,fingerprint:"0000000000000000",inference_ms:5)
        precondition(policy.shouldCapture(r,pose:pose,time:1,tracking:true,pending:0))
        policy.record(r,pose:pose,time:1)
        precondition(!policy.shouldCapture(r,pose:pose,time:10,tracking:true,pending:0))
        precondition(DepthReadiness.level(valid:0,total:0) == 0)
        precondition(DepthReadiness.level(valid:0,total:100) == 1)
        precondition(DepthReadiness.level(valid:40,total:100) == 2)
        precondition(DepthReadiness.level(valid:90,total:100) == 3)
        precondition(policy.shouldCapture(r,pose:pose,time:10,tracking:true,pending:0,depthLevel:1))
        policy.record(r,pose:pose,time:10,depthLevel:1)
        precondition(policy.shouldCapture(r,pose:pose,time:20,tracking:true,pending:0,depthLevel:3))
        policy.record(r,pose:pose,time:20,depthLevel:3)
        precondition(!policy.shouldCapture(r,pose:pose,time:30,tracking:true,pending:0,depthLevel:3))
        r.sharpness = 150
        precondition(policy.shouldCapture(r,pose:pose,time:30,tracking:true,pending:0))
        precondition(!policy.shouldCapture(r,pose:pose,time:10,tracking:true,pending:4))
        r.sharpness = 2
        precondition(!policy.shouldCapture(r,pose:pose,time:10,tracking:true,pending:0))
        precondition(!CaptureFrameGate.accepts(frameTime:90,captureStart:100,generation:"new",currentGeneration:"new"))
        precondition(!CaptureFrameGate.accepts(frameTime:105,captureStart:100,generation:"old",currentGeneration:"new"))
        precondition(CaptureFrameGate.accepts(frameTime:105,captureStart:100,generation:"new",currentGeneration:"new"))
        print("Local recognition geometry/selection checks passed")
    }
}
