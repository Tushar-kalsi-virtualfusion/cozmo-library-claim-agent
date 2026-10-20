import Foundation

@main struct CloudSpeechHandoffChecks {
    @MainActor static func main() async throws {
        var events: [String] = []
        try await CloudSpeechHandoff.request(
            prepareAnnouncement: { events.append("translate"); return "Starting capture" },
            play: { text in
                precondition(text == "Starting capture")
                events.append("playing")
                await Task.yield()
                precondition(!events.contains("capture"), "Onboarding must stay open during audio playback")
                events.append("played")
            },
            checkCurrent: {},
            requestCapture: { events.append("capture") })
        precondition(events == ["translate","playing","played","capture"])
        var current = true, captureRequests = 0
        do {
            try await CloudSpeechHandoff.request(
                prepareAnnouncement: { "Starting capture" },
                play: { _ in current = false },
                checkCurrent: { if !current { throw CancellationError() } },
                requestCapture: { captureRequests += 1 })
            fatalError("Stop during playback must cancel handoff")
        } catch is CancellationError {}
        precondition(captureRequests == 0)
        do {
            try await CloudSpeechHandoff.request(
                prepareAnnouncement: { "Starting capture" },
                play: { _ in throw CloudSpeechError.playbackUnavailable },
                checkCurrent: {},
                requestCapture: { captureRequests += 1 })
            fatalError("Failed playback must not consume onboarding")
        } catch CloudSpeechError.playbackUnavailable {}
        precondition(captureRequests == 0)
        print("Cloud speech handoff playback ordering, failure and Stop checks passed")
    }
}
