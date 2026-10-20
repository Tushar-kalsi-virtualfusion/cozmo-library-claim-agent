import Foundation

@main struct ModelContractChecks {
    static func main() throws {
        let data = #"{"version":1,"id":"household-test","labels":["coffee table","wireless router","phone charger"],"input_size":640,"output_name":"boxes","output_shape":[1,300,38],"confidence_threshold":0.35}"#
        let contract = try LocalModelContract.read(data)
        let broadLabels=(0..<4585).map { "native model label \($0)" }
        let broad=try JSONSerialization.jsonObject(with:Data(data.utf8)) as! [String:Any]
        var expanded=broad; expanded["labels"]=broadLabels
        let full=try LocalModelContract.read(String(data:JSONSerialization.data(withJSONObject:expanded),encoding:.utf8)!)
        precondition(LocalGeometry.decode([160,80,480,560,0.8,4584],width:640,height:480,rotation:0,labels:full.labels)?.label == broadLabels[4584])
        let row = [160.0,80,480,560,0.8,1]
        let decoded = LocalGeometry.decode(row,width:640,height:480,rotation:90,labels:contract.labels,threshold:contract.confidence_threshold)
        precondition(decoded?.label == "wireless router")
        precondition(LocalGeometry.decode([160,80,480,560,0.3,1],width:640,height:480,rotation:0,labels:contract.labels,threshold:contract.confidence_threshold) == nil)
        precondition(LocalGeometry.decode([160,80,480,560,0.8,1.5],width:640,height:480,rotation:0,labels:contract.labels) == nil)
        precondition(LocalGeometry.decode([160,80,480,560,1.1,1],width:640,height:480,rotation:0,labels:contract.labels) == nil)
        precondition((try? LocalModelContract.read(data.replacingOccurrences(of:"[1,300,38]",with:"[1,38,300]"))) == nil)
        precondition((try? LocalModelContract.read(data.replacingOccurrences(of:"wireless router",with:"coffee table"))) == nil)
        let p = [[0.1,0.1],[0.8,0.1],[0.8,0.8],[0.1,0.8]]
        let table = LocalObject(label:"coffee table",confidence:0.8,polygon:p)
        let duplicate = LocalObject(label:"table",confidence:0.6,polygon:p)
        let charger = LocalObject(label:"phone charger",confidence:0.8,polygon:[[0.2,0.2],[0.3,0.2],[0.3,0.3],[0.2,0.3]])
        precondition(LocalGeometry.suppressDuplicates([table,duplicate,charger]).count == 2)
        let crop = LocalObject(label:"wall art",confidence:0.8,polygon:[[0.2,0.2],[0.4,0.2],[0.4,0.6],[0.2,0.6]])
        let mapped = LocalGeometry.fromCrop(crop,region:[0,0.4,1,0.6],rotation:90)!
        precondition(abs(mapped.polygon[0][0]-0.52)<0.0001 && abs(mapped.polygon[0][1]-0.8)<0.0001)
        let clipped = LocalObject(label:"wall art",confidence:0.8,polygon:[[0,0.2],[0.4,0.2],[0.4,0.6],[0,0.6]])
        precondition(LocalGeometry.fromCrop(clipped,region:[0,0.4,1,0.6],rotation:90) == nil)
        precondition(LocalGeometry.fromCrop(crop,region:[0,0.5,1,0.6],rotation:0) == nil)
        let competing = LocalObject(label:"arbitrary different model label",confidence:0.95,polygon:p)
        precondition(LocalGeometry.suppressDuplicates([competing],preserving:[table]).first?.label == table.label)
        let many = (0..<400).map { i in
            let x=Double(i%20)/20, y=Double(i/20)/20
            return LocalObject(label:"model label \(i)",confidence:Double(i+1)/401,
                polygon:[[x,y],[x+0.02,y],[x+0.02,y+0.02],[x,y+0.02]])
        }
        let bounded=LocalGeometry.suppressDuplicates(many)
        precondition(bounded.count == 300 && bounded.first!.confidence > bounded.last!.confidence)
        print("Model contract checks passed: vocabulary, tensor layout, scores, class indices and alias suppression")
    }
}
