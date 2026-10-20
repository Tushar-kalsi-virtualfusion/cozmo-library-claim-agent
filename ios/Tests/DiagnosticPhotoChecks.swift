import Foundation

@main struct DiagnosticChecks {
    static func main() {
        var disabled = DiagnosticPhotoSchedule(enabled:false)
        precondition(disabled.reserve(time:1,foreground:true,hasConversation:false) == nil)
        var active = DiagnosticPhotoSchedule(enabled:true)
        precondition(active.reserve(time:1,foreground:false,hasConversation:false) == nil)
        precondition(active.reserve(time:1,foreground:true,hasConversation:true) == nil)
        precondition(active.reserve(time:1,foreground:true,hasConversation:false) == 1)
        precondition(active.reserve(time:2,foreground:true,hasConversation:false) == nil)
        for n in 2...6 { precondition(active.reserve(time:Double(n*4),foreground:true,hasConversation:false) == n) }
        precondition(active.reserve(time:100,foreground:true,hasConversation:false) == nil)
        print("Diagnostic capture checks passed: explicit opt-in, foreground/setup gates, six-photo bound and interval")
    }
}
