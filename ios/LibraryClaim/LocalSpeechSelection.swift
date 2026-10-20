import Foundation

enum LocalSpeechVoiceQuality: Int {
    case standard = 1, enhanced, premium

    var description: String {
        switch self {
        case .standard: return "default"
        case .enhanced: return "enhanced"
        case .premium: return "premium"
        }
    }
}

struct LocalSpeechVoice: Equatable {
    let identifier: String
    let locale: String
    let quality: LocalSpeechVoiceQuality
    var isNovelty = false
}

enum LocalSpeechSelection {
    static func canonicalLocale(_ identifier: String) -> String {
        Locale(identifier:identifier).identifier(.bcp47)
    }

    static func sameLanguage(_ first: String, _ second: String) -> Bool {
        let lhs = Locale(identifier:first).language, rhs = Locale(identifier:second).language
        return lhs.languageCode != nil && lhs.languageCode == rhs.languageCode && lhs.script == rhs.script
    }

    /// Prefer quality within the selected language/script, then preserve the regional accent.
    /// Never use novelty voices or a default voice from a different language.
    static func rankedVoices(locale: String, voices: [LocalSpeechVoice]) -> [LocalSpeechVoice] {
        let target = canonicalLocale(locale)
        return voices.filter { !$0.isNovelty && sameLanguage($0.locale,target) }.sorted {
            if $0.quality != $1.quality { return $0.quality.rawValue > $1.quality.rawValue }
            let lhsExact = canonicalLocale($0.locale) == target
            let rhsExact = canonicalLocale($1.locale) == target
            if lhsExact != rhsExact { return lhsExact }
            return $0.identifier < $1.identifier
        }
    }

    static func preferredVoice(locale: String, voices: [LocalSpeechVoice]) -> LocalSpeechVoice? {
        rankedVoices(locale:locale,voices:voices).first
    }

    static func recognitionLocale(language: String, explicitLocale: String? = nil,
                                  preferredLocales: [String], supportedLocales: [String],
                                  supportsOnDevice: (String) -> Bool = { _ in true }) -> String? {
        let supported = Set(supportedLocales.map(canonicalLocale))
        var matching = supported.filter { matches(language:language,locale:$0) && supportsOnDevice($0) }
        if let explicitLocale, !explicitLocale.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty {
            let explicit = canonicalLocale(explicitLocale)
            if matching.contains(explicit) { return explicit }
            // Claude may return a valid language/script tag without a region (en,
            // hi, zh-Hant). Resolve that hint against actual on-device recognizers.
            // A specific unsupported region or mismatched language still fails.
            guard Locale(identifier:explicit).region == nil else { return nil }
            matching = matching.filter { sameLanguage($0,explicit) }
        }
        for preferred in preferredLocales.map(canonicalLocale) where matching.contains(preferred) {
            return preferred
        }
        return matching.sorted().first
    }

    private static func matches(language: String, locale: String) -> Bool {
        let native = Locale(identifier:locale), english = Locale(identifier:"en")
        guard let code = native.language.languageCode?.identifier else { return false }
        let labels = [code, locale, english.localizedString(forLanguageCode:code),
                      native.localizedString(forLanguageCode:code),
                      english.localizedString(forIdentifier:locale), native.localizedString(forIdentifier:locale)]
        let wanted = normalizedLabel(language)
        return !wanted.isEmpty && labels.compactMap { $0 }.contains { normalizedLabel($0) == wanted }
    }

    private static func normalizedLabel(_ value: String) -> String {
        value.trimmingCharacters(in:.whitespacesAndNewlines)
            .replacingOccurrences(of:"_",with:"-")
            .folding(options:[.caseInsensitive,.diacriticInsensitive],locale:Locale(identifier:"en"))
    }
}
