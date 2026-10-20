import Foundation
@main struct Checks {
 static func main() throws {
  let data = #"{"id":"t","category":"table","dimensions":[1.4,0.8,0.7],"confidence":"high","corners":[[-0.7,0,-0.35],[-0.7,0,0.35],[-0.7,0.8,-0.35],[-0.7,0.8,0.35],[0.7,0,-0.35],[0.7,0,0.35],[0.7,0.8,-0.35],[0.7,0.8,0.35]]}"#
  let d=JSONDecoder()
  let f=MeasuredFurniture(try d.decode(JSONValue.self,from:Data(data.utf8)))!
  precondition(f.dimensionsText == "L 140 · W 70 · H 80 cm")
  precondition(f.top.allSatisfy {abs($0.y-0.8)<0.001})
  let low=try d.decode(JSONValue.self,from:Data(data.replacingOccurrences(of:"high",with:"low").utf8))
  precondition(MeasuredFurniture(low) == nil)
  let room=try d.decode(JSONValue.self,from:Data(#"{"length_m":4,"width_m":3,"height_m":2.6,"floor_area_m2":12}"#.utf8))
  precondition(RoomMeasurementSummary(value:room).size == "L 4.00 m · W 3.00 m · H 2.60 m")
  precondition(RoomMeasurementSummary(value:.null).area == "Floor area pending")
  print("Furniture dimensions, top-plane coordinates and unknown room checks passed")
 }
}
