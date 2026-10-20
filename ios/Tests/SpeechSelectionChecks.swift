import Foundation

@main struct SpeechSelectionChecks {
    static func main() {
        var failures: [String] = []
        func check(_ condition: @autoclosure () -> Bool, _ message: String) {
            if !condition() { failures.append(message) }
        }
        let voices: [LocalSpeechVoice] = [
            .init(identifier:"english-basic",locale:"en-IN",quality:.standard),
            .init(identifier:"english-enhanced",locale:"en-IN",quality:.enhanced),
            .init(identifier:"english-premium",locale:"en-GB",quality:.premium),
            .init(identifier:"hindi-basic",locale:"hi-IN",quality:.standard),
            .init(identifier:"hindi-premium",locale:"hi-IN",quality:.premium),
            .init(identifier:"novelty",locale:"en-IN",quality:.premium,isNovelty:true)
        ]
        // Regressions caught: selecting the default voice, picking a different language,
        // choosing a novelty voice, and relying on OS enumeration order.
        check(LocalSpeechSelection.preferredVoice(locale:"en_IN",voices:voices)?.identifier == "english-premium",
              "Prefer an installed premium English voice over the default and novelty voices")
        check(LocalSpeechSelection.preferredVoice(locale:"hi-IN",voices:voices)?.identifier == "hindi-premium",
              "Hindi must never fall back to an English voice")
        check(LocalSpeechSelection.preferredVoice(locale:"fr-FR",voices:voices) == nil,
              "A missing language must stay unavailable rather than silently switch languages")
        let tied = voices + [.init(identifier:"english-premium-india",locale:"en-IN",quality:.premium)]
        check(LocalSpeechSelection.preferredVoice(locale:"en-IN",voices:tied.reversed())?.identifier == "english-premium-india",
              "Exact locale wins when two voices have the same quality")
        check(LocalSpeechSelection.preferredVoice(locale:"en-IN",voices:voices.filter { $0.quality != .premium })?.identifier == "english-enhanced",
              "Enhanced voice is the fallback when premium is not installed")
        check(LocalSpeechSelection.preferredVoice(locale:"hi-IN",voices:Array(voices.prefix(4)))?.identifier == "hindi-basic",
              "The correct basic voice remains usable when no higher quality voice is installed")
        check(LocalSpeechSelection.preferredVoice(locale:"zh-Hant-TW",voices:[
            .init(identifier:"simplified",locale:"zh-Hans-CN",quality:.premium),
            .init(identifier:"traditional",locale:"zh-TW",quality:.standard)])?.identifier == "traditional",
              "Explicit script must not change to a different script for higher quality")

        let supported = ["en_US","en_IN","hi_IN","fr_FR"]
        // Regressions caught: using the phone's language before saved preferences,
        // accepting a model's mismatched locale, and losing a selected region on reconnect.
        check(LocalSpeechSelection.recognitionLocale(language:"Hindi",preferredLocales:["en_IN"],supportedLocales:supported) == "hi-IN",
              "Restore Hindi preferences on an English-language phone")
        check(LocalSpeechSelection.recognitionLocale(language:"English",preferredLocales:["hi_IN","en_IN"],supportedLocales:supported) == "en-IN",
              "A previous Hindi session must not control a new English conversation")
        check(LocalSpeechSelection.recognitionLocale(language:"English",explicitLocale:"en_IN",preferredLocales:["en_US"],supportedLocales:supported) == "en-IN",
              "Normalize the explicit selected region and preserve it")
        check(LocalSpeechSelection.recognitionLocale(language:"Hindi",explicitLocale:"en-IN",preferredLocales:[],supportedLocales:supported) == nil,
              "Reject a mismatched model language and locale together")
        check(LocalSpeechSelection.recognitionLocale(language:"Hindi",explicitLocale:"hi-XX",preferredLocales:[],supportedLocales:supported) == nil,
              "Reject unsupported explicit speech locales")
        check(LocalSpeechSelection.recognitionLocale(language:"Klingon",preferredLocales:["en_IN"],supportedLocales:supported) == nil,
              "Unsupported preferences must not silently fall back to the phone language")
        check(LocalSpeechSelection.recognitionLocale(language:"हिन्दी",preferredLocales:["en_US"],supportedLocales:supported) == "hi-IN",
              "Native language names restore the same language")
        check(LocalSpeechSelection.recognitionLocale(language:"hi-IN",preferredLocales:[],supportedLocales:supported) == "hi-IN",
              "Persisted BCP 47 language tags remain usable")
        // Actual Claude setup reply uses speech_locale="en", a valid language-only tag.
        check(LocalSpeechSelection.recognitionLocale(language:"English",explicitLocale:"en",preferredLocales:["en-IN"],supportedLocales:supported) == "en-IN",
              "A language-only Claude locale resolves to the preferred supported region")
        check(LocalSpeechSelection.recognitionLocale(language:"Hindi",explicitLocale:"hi",preferredLocales:["en-IN"],supportedLocales:supported) == "hi-IN",
              "A language-only Hindi locale resolves without changing language")
        check(LocalSpeechSelection.recognitionLocale(language:"Hindi",explicitLocale:"en",preferredLocales:[],supportedLocales:supported) == nil,
              "A mismatched language-only model locale remains rejected")
        check(LocalSpeechSelection.recognitionLocale(language:"Chinese",explicitLocale:"zh-Hant",preferredLocales:["zh-CN","zh-TW"],supportedLocales:["zh-CN","zh-TW"]) == "zh-TW",
              "A language-script tag without a region preserves the explicit script")
        let offlineSupport = ["en-IN":false,"en-US":true,"hi-IN":true]
        check(LocalSpeechSelection.recognitionLocale(language:"English",explicitLocale:"en",preferredLocales:["en-IN"],supportedLocales:supported,
              supportsOnDevice:{ offlineSupport[$0] == true }) == "en-US",
              "Language-only model locales still use only on-device-capable regions")
        check(LocalSpeechSelection.recognitionLocale(language:"English",preferredLocales:["en-IN"],supportedLocales:supported,
              supportsOnDevice:{ offlineSupport[$0] == true }) == "en-US",
              "A preferred cloud-only region must not block an available offline region of the same language")
        check(LocalSpeechSelection.recognitionLocale(language:"English",explicitLocale:"en-IN",preferredLocales:[],supportedLocales:supported,
              supportsOnDevice:{ offlineSupport[$0] == true }) == nil,
              "An explicit cloud-only locale must be rejected rather than silently changed")
        check(LocalSpeechSelection.recognitionLocale(language:"English",explicitLocale:"en-US",preferredLocales:["en-IN"],supportedLocales:supported,
              supportsOnDevice:{ offlineSupport[$0] == true }) == "en-US",
              "An explicit offline-capable locale remains valid")
        check(LocalSpeechSelection.recognitionLocale(language:"French",preferredLocales:[],supportedLocales:supported,
              supportsOnDevice:{ offlineSupport[$0] == true }) == nil,
              "A language with no offline-capable recognizer must remain unavailable")
        if !failures.isEmpty {
            for failure in failures { print("FAIL: \(failure)") }
            exit(1)
        }
        print("Speech quality ranking, correct-language fallback, script matching and preference restoration checks passed")
    }
}
