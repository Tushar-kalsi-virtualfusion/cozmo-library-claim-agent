import Foundation

@main struct RealtimeAudioLifecycleChecks {
    static func main() {
        var gate = RealtimeAudioLifecycleGate()
        let first = gate.beginStart()
        precondition(!gate.configurationChanged(generation:first,engineRunning:false,formatMatches:false,routeMatches:false), "Own start notifications must not fail an unstarted session")
        gate.didStart(generation:first)
        precondition(!gate.configurationChanged(generation:first,engineRunning:true,formatMatches:true,routeMatches:true), "A delayed notification from successful startup must not stop a healthy engine")
        precondition(gate.configurationChanged(generation:first,engineRunning:false,formatMatches:true,routeMatches:true), "An engine stopped by the system must make Listening fail visibly")
        precondition(!gate.configurationChanged(generation:first,engineRunning:false,formatMatches:false,routeMatches:false), "One route change must cause only one failure")
        gate.stop()
        precondition(!gate.configurationChanged(generation:first,engineRunning:false,formatMatches:false,routeMatches:false), "Own stop notifications are not session failures")
        let replacement = gate.beginStart()
        gate.didStart(generation:replacement)
        precondition(!gate.configurationChanged(generation:first,engineRunning:false,formatMatches:false,routeMatches:false), "A delayed old engine callback cannot stop a replacement session")
        precondition(gate.configurationChanged(generation:replacement,engineRunning:true,formatMatches:true,routeMatches:false), "A new Bluetooth route must invalidate the converter even if its sample format happens to match")
        let changedFormat = gate.beginStart()
        gate.didStart(generation:changedFormat)
        precondition(gate.configurationChanged(generation:changedFormat,engineRunning:true,formatMatches:false,routeMatches:true), "A hardware format change must not keep using the old converter")
        let beforeReset = gate.beginStart()
        gate.didStart(generation:beforeReset)
        gate.stop()
        let afterReset = gate.beginStart()
        gate.didStart(generation:afterReset)
        precondition(beforeReset != afterReset)
        precondition(!gate.configurationChanged(generation:beforeReset,engineRunning:false,formatMatches:false,routeMatches:false), "Reset invalidates all callbacks from orphaned audio objects")
        precondition(!gate.configurationChanged(generation:afterReset,engineRunning:true,formatMatches:true,routeMatches:true))
        print("Realtime audio start/stop, changed route/format, reset and stale notification checks passed")
    }
}
