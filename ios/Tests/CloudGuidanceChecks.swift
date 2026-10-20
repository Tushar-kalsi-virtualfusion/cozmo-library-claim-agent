import Foundation

@main struct CloudGuidanceChecks {
    static func main() {
        var gate = CloudGuidanceGate()
        let activity = UUID(), speaking = UUID()
        precondition(gate.begin(now:0,idle:false,activity:activity) == nil, "Never request while owner speech/playback is active")
        guard let first = gate.begin(now:0,idle:true,activity:activity) else { fatalError("Idle guidance was not admitted") }
        precondition(gate.begin(now:10,idle:true,activity:activity) == nil, "Coalesce while one request is in flight")
        precondition(!gate.complete(request:first,activity:speaking,idle:true,eventID:"saved-1"), "Speech that began during the request invalidates its reply even if now idle")
        precondition(gate.begin(now:7,idle:true,activity:speaking) == nil, "Do not exceed the poll rate")
        let second = gate.begin(now:8,idle:true,activity:speaking)!
        precondition(gate.complete(request:second,activity:speaking,idle:true,eventID:"saved-2"))
        let duplicate = gate.begin(now:16,idle:true,activity:speaking)!
        precondition(!gate.complete(request:duplicate,activity:speaking,idle:true,eventID:"saved-2"), "Do not repeat already delivered cues")
        let interrupted = gate.begin(now:24,idle:true,activity:speaking)!
        precondition(!gate.complete(request:interrupted,activity:speaking,idle:false,eventID:"saved-3"), "A reply cannot interrupt a current utterance")
        let failure = gate.begin(now:32,idle:true,activity:speaking)!
        gate.pause()
        precondition(gate.begin(now:80,idle:true,activity:speaking) == nil, "An ambiguous paid result pauses proactive requests")
        gate = CloudGuidanceGate()
        precondition(!gate.complete(request:failure,activity:speaking,idle:true,eventID:"stale"), "Stopped/restarted sessions cannot deliver old cues")
        let cancelled = gate.begin(now:0,idle:true,activity:activity)!
        gate.cancelPending()
        precondition(!gate.complete(request:cancelled,activity:activity,idle:true,eventID:"cancelled"))
        precondition(gate.begin(now:7,idle:true,activity:activity) == nil)
        precondition(gate.begin(now:8,idle:true,activity:activity) != nil, "After owner speech, polling can resume without retaining a cancelled slot")
        gate.pause()
        precondition(gate.begin(now:40,idle:true,activity:activity) == nil, "A synthesis failure after a cue was received also pauses guidance")
        print("Cloud guidance idle admission, throttle, coalescing, owner cancellation and ambiguous failure checks passed")
    }
}
