import Foundation

/// The spoken key: did the tech say this customer's name, street or house
/// number at the start of the clip?
///
/// It is the only one of the three keys that can tell next-door customers
/// apart, so it also looks at every other stop on today's list and says so
/// when a different stop fits the words better.
public enum SpokenMatcher {
    public struct Score: Equatable, Sendable {
        public var lastName = false
        /// What the tech is asked to say: "Hi Jane".
        public var firstName = false
        public var street = false
        public var houseNumber = false

        /// Last name is the strongest signal; the first name, a street or a
        /// full house number each count too. Zero means nothing of this stop
        /// was heard.
        public var value: Int {
            (lastName ? 3 : 0) + (firstName ? 2 : 0) + (street ? 2 : 0) + (houseNumber ? 2 : 0)
        }
    }

    public struct Outcome: Equatable, Sendable {
        public let result: SignalResult
        /// A different stop on today's list that the words fit better.
        public let better: Stop?
        public let target: Score
    }

    // MARK: - Public API

    /// Scores the transcript against the chosen stop and against the rest of the list.
    ///
    /// The tech is asked for one thing: the customer's first name ("Hi
    /// Jane"). A last name counts for more; a street or a house number, if
    /// he happens to say one, still counts. A tie with another stop is a
    /// match (two Janes on one route must not warn); only a stop that fits
    /// the words strictly better makes a mismatch.
    ///
    /// - Parameter techName: the tech's own first name. Said in "this is
    ///   Sam", it must not point at a customer called Sam somewhere else.
    public static func evaluate(transcript: String?, target: Stop, all: [Stop], techName: String? = nil) -> Outcome {
        guard let transcript else {
            return Outcome(result: .unknown, better: nil, target: Score())
        }
        let heard = Heard(transcript)
        // Too little speech to judge: unknown, not a mismatch.
        guard heard.tokens.count >= 2 else {
            return Outcome(result: .unknown, better: nil, target: Score())
        }

        let own = Set(tokens(techName ?? ""))
        let targetScore = score(heard, stop: target)
        let others = all
            .filter { $0.customerId != target.customerId }
            .map { (stop: $0, score: score(heard, stop: $0, ignoringFirstNames: own)) }
        let bestValue = others.map { $0.score.value }.max() ?? 0
        let best = others.filter { $0.score.value == bestValue }
        let uniqueBest: Stop? = (best.count == 1 && bestValue >= 2) ? best[0].stop : nil

        if targetScore.value > 0 && bestValue <= targetScore.value {
            return Outcome(result: .match, better: nil, target: targetScore)
        }
        return Outcome(result: .mismatch, better: uniqueBest, target: targetScore)
    }

    public static func score(transcript: String, stop: Stop) -> Score {
        score(Heard(transcript), stop: stop)
    }

    /// Words to hand the speech recognizer so it spells names the way they are
    /// written on the route: first names (what the tech is asked to say),
    /// last names and street names. Names first: if the list has to be cut
    /// short, the streets are what goes.
    public static func hints(for stops: [Stop]) -> [String] {
        var seen = Set<String>()
        var out: [String] = []
        func add(_ text: String) {
            let trimmed = text.trimmingCharacters(in: .whitespaces)
            guard trimmed.count >= 2, seen.insert(trimmed.lowercased()).inserted else { return }
            out.append(trimmed)
        }
        for stop in stops where !stop.lastName.trimmingCharacters(in: .whitespaces).isEmpty {
            // "Mary Ann" as said, and each word of it.
            let first = stop.greetingName
            add(first)
            for word in first.split(separator: " ") where word.contains(where: { $0.isLetter }) {
                add(String(word))
            }
        }
        for stop in stops {
            add(stop.lastName)
        }
        for stop in stops {
            let parts = addressParts(stop.address)
            if !parts.streetDisplay.isEmpty { add(parts.streetDisplay) }
        }
        return Array(out.prefix(100))
    }

    // MARK: - Address parsing

    public struct AddressParts: Equatable, Sendable {
        /// Digits of the house number, e.g. "123" from "123-B Main St".
        public var houseNumber: String?
        /// The street name without number, direction, suffix or unit: ["oak", "hollow"].
        public var street: [String]
        /// The street as written, for speech hints: "Oak Hollow".
        public var streetDisplay: String
    }

    static let suffixes: Set<String> = [
        "st", "street", "ave", "av", "avenue", "blvd", "boulevard", "dr", "drive", "ct", "court", "cir", "circle",
        "ln", "lane", "rd", "road", "way", "pl", "place", "ter", "terrace", "trl", "trail", "pkwy", "parkway",
        "hwy", "highway", "loop", "sq", "square", "aly", "alley", "cv", "cove", "pt", "point",
    ]
    static let directions: Set<String> = [
        "n", "s", "e", "w", "ne", "nw", "se", "sw", "north", "south", "east", "west",
    ]
    static let unitWords: Set<String> = ["apt", "apartment", "unit", "ste", "suite", "bldg", "building", "lot", "spc", "space", "rm", "room"]

    /// Words in a street name that are written short on the route and said
    /// in full, or the other way round: "St Andrews Dr" is "Saint Andrews".
    /// Both are matched as the short form. (Not used for last names.)
    static let streetAliases: [String: String] = ["saint": "st", "mount": "mt", "fort": "ft", "highway": "hwy"]

    /// How a PO box address starts, as normalized words.
    static let poBoxLeads: [[String]] = [["po", "box"], ["p", "o", "box"], ["pobox"], ["post", "office", "box"]]

    public static func addressParts(_ address: String) -> AddressParts {
        // Keep the original words for display, normalized ones for matching.
        let rawWords = address
            .replacingOccurrences(of: "#", with: " unit ")
            .split(whereSeparator: { $0 == " " || $0 == "," })
            .map(String.init)
        var pairs: [(raw: String, norm: String)] = rawWords.compactMap { raw in
            let norm = normalizeToken(raw)
            return norm.isEmpty ? nil : (raw, norm)
        }

        // "PO Box 1432" has no street. The box number is what tells it from
        // every other PO box, so it stands in for the house number.
        let norms = pairs.map { $0.norm }
        for lead in poBoxLeads where norms.count > lead.count && Array(norms.prefix(lead.count)) == lead {
            let number = String(norms[lead.count].prefix { $0.isNumber })
            if !number.isEmpty {
                return AddressParts(houseNumber: number, street: [], streetDisplay: "")
            }
        }

        var house: String?
        if let first = pairs.first, let digit = first.norm.first, digit.isNumber, !isOrdinal(first.norm) {
            let digits = String(first.norm.prefix { $0.isNumber })
            house = digits.isEmpty ? nil : digits
            pairs.removeFirst()
        }

        if let cut = pairs.firstIndex(where: { unitWords.contains($0.norm) }) {
            pairs = Array(pairs[..<cut])
        }
        // A unit written with no word in front of it ("... Blvd 14B", "... Rd 3").
        // A numbered street ("5th") is a name, not a unit, and stays.
        while pairs.count > 1, let last = pairs.last, let lead = last.norm.first, lead.isNumber, !isOrdinal(last.norm) {
            pairs.removeLast()
        }
        if pairs.count > 1, let last = pairs.last, directions.contains(last.norm) {
            pairs.removeLast()
        }
        if pairs.count > 1, let last = pairs.last, suffixes.contains(last.norm) {
            pairs.removeLast()
        }
        if pairs.count > 1, let first = pairs.first, directions.contains(first.norm) {
            pairs.removeFirst()
        }

        return AddressParts(
            houseNumber: house,
            street: pairs.map { streetAliases[$0.norm] ?? $0.norm },
            streetDisplay: pairs.map { $0.raw }.joined(separator: " ")
        )
    }

    // MARK: - Scoring

    static func score(_ heard: Heard, stop: Stop, ignoringFirstNames ignored: Set<String> = []) -> Score {
        var score = Score()

        // Last name, or for a business the telling words of its name.
        let nameWords: [String]
        let isPerson = !stop.lastName.trimmingCharacters(in: .whitespaces).isEmpty
        if isPerson {
            nameWords = tokens(stop.lastName)
        } else {
            nameWords = tokens(stop.name).filter { !businessNoise.contains($0) }
        }
        score.lastName = heard.containsName(nameWords)

        // First name: what the tech is asked to say. Any one of a person's
        // first names will do ("Hi Mary" for Mary Ann, "Hi John" for John
        // & Mary).
        if isPerson {
            score.firstName = firstNameWords(of: stop).contains { word in
                !ignored.contains(word) && heard.containsFirstName(word)
            }
        }

        let parts = addressParts(stop.address)
        if !parts.street.isEmpty {
            score.street = heard.containsAll(parts.street)
        }
        if let house = parts.houseNumber, heard.numbers.contains(house) {
            // A short number ("5") turns up in ordinary talk ("5 sprinklers"),
            // so on its own it only counts once it is three digits or more.
            score.houseNumber = house.count >= 3 || score.street
        }
        return score
    }

    /// A person's first names as words to listen for. Titles, "and", and
    /// names of one or two letters are left out: "Al" or "Jo" would turn up
    /// inside ordinary talk by accident.
    static func firstNameWords(of stop: Stop) -> [String] {
        tokens(stop.greetingName).filter { $0.count >= 3 && !firstNameNoise.contains($0) && !$0.allSatisfy { $0.isNumber } }
    }

    static let firstNameNoise: Set<String> = ["and", "mr", "mrs", "miss", "mister", "dr", "the", "family", "jr", "sr"]

    /// First names that are also everyday words. "I will spray the rose
    /// bushes in May" names nobody: these only count right after a greeting
    /// ("Hi Rose", "Good morning Will").
    static let everydayWordNames: Set<String> = [
        "will", "may", "art", "rose", "mark", "bill", "grace", "hope", "joy", "guy", "pat", "rich", "sue", "don",
        "june", "april", "august", "summer", "dawn", "faith", "chase", "hunter", "frank", "gene", "rob", "max",
        "miles", "cliff", "dale", "glen", "lane", "skip", "chip", "bud", "dean", "penny", "ruby", "ivy", "holly",
        "iris", "lily", "daisy", "olive", "amber", "sunny", "sandy", "rusty", "jack", "jay", "ray", "drew", "wade",
        "reed", "page", "lance", "sage", "basil", "herb", "rod", "nick", "van", "lee", "bob", "tony", "misty",
        "storm", "rocky", "sky", "star", "crystal", "ginger", "pepper", "angel", "major", "junior", "sonny", "dusty",
    ]

    /// Words a first name follows when it is being said to the customer.
    static let greetings: Set<String> = [
        "hi", "hello", "hey", "morning", "afternoon", "evening", "mr", "mister", "mrs", "ms", "miss", "dear", "thanks", "bye",
    ]

    static let businessNoise: Set<String> = [
        "the", "inc", "llc", "corp", "co", "company", "and", "of", "ltd", "group", "hoa", "association",
    ]

    /// What was said, pre-digested once per clip.
    struct Heard {
        let tokens: [String]
        /// Tokens plus neighbours glued together ("mc" + "donald").
        let words: Set<String>
        /// Every way the spoken numbers could be read as a house number.
        let numbers: Set<String>

        init(_ transcript: String) {
            let tokens = SpokenMatcher.tokens(transcript)
            self.tokens = tokens
            var words = Set(tokens)
            if tokens.count > 1 {
                for i in 0..<(tokens.count - 1) { words.insert(tokens[i] + tokens[i + 1]) }
            }
            // "Saint" said aloud also counts as the "St" written on the route.
            for token in tokens {
                if let short = SpokenMatcher.streetAliases[token] { words.insert(short) }
            }
            self.words = words
            self.numbers = SpokenMatcher.numberCandidates(tokens)
        }

        func contains(_ word: String) -> Bool {
            if words.contains(word) { return true }
            return words.contains { SpokenMatcher.fuzzyEqual($0, word) }
        }

        func containsAll(_ wanted: [String]) -> Bool {
            !wanted.isEmpty && wanted.allSatisfy { contains($0) }
        }

        /// A first name, heard anywhere; or, for a name that is also an
        /// everyday word, only straight after a greeting. Spelled exactly
        /// (the recognizer is given the route's first names): a near miss
        /// would turn "grand" into Grant.
        func containsFirstName(_ word: String) -> Bool {
            guard SpokenMatcher.everydayWordNames.contains(word) else { return words.contains(word) }
            for index in tokens.indices.dropFirst() where tokens[index] == word {
                if SpokenMatcher.greetings.contains(tokens[index - 1]) { return true }
            }
            return false
        }

        func containsName(_ nameWords: [String]) -> Bool {
            let telling = nameWords.filter { $0.count >= 3 }
            let joined = nameWords.joined()
            if joined.count >= 3 && contains(joined) { return true }
            return !telling.isEmpty && telling.allSatisfy { contains($0) }
        }
    }

    // MARK: - Text normalizing

    /// Lowercased, accent-free words with punctuation dropped. Ordinals are
    /// unified ("fifth" and "5th" both become "5th").
    public static func tokens(_ text: String) -> [String] {
        text.split(whereSeparator: { $0 == " " || $0 == "\n" || $0 == "\t" || $0 == "-" || $0 == "/" || $0 == "," || $0 == "." })
            .map { normalizeToken(String($0)) }
            .filter { !$0.isEmpty }
    }

    static func normalizeToken(_ raw: String) -> String {
        let folded = raw.folding(options: [.diacriticInsensitive, .caseInsensitive], locale: Locale(identifier: "en_US"))
            .lowercased()
        let cleaned = String(folded.unicodeScalars.filter { scalar in
            (scalar.value >= 97 && scalar.value <= 122) || (scalar.value >= 48 && scalar.value <= 57)
        })
        return ordinalWords[cleaned] ?? cleaned
    }

    static func isOrdinal(_ token: String) -> Bool {
        guard token.count >= 3, let first = token.first, first.isNumber else { return false }
        let suffix = String(token.suffix(2))
        let body = token.dropLast(2)
        return ["st", "nd", "rd", "th"].contains(suffix) && body.allSatisfy { $0.isNumber }
    }

    static let ordinalWords: [String: String] = [
        "first": "1st", "second": "2nd", "third": "3rd", "fourth": "4th", "fifth": "5th", "sixth": "6th",
        "seventh": "7th", "eighth": "8th", "ninth": "9th", "tenth": "10th", "eleventh": "11th", "twelfth": "12th",
        "thirteenth": "13th", "fourteenth": "14th", "fifteenth": "15th", "sixteenth": "16th",
        "seventeenth": "17th", "eighteenth": "18th", "nineteenth": "19th", "twentieth": "20th",
    ]

    /// Loose equality for names the recognizer may misspell: exact for short
    /// words, one slip for medium ones, two for long ones.
    public static func fuzzyEqual(_ a: String, _ b: String) -> Bool {
        if a == b { return true }
        let shorter = min(a.count, b.count)
        if shorter <= 4 { return false }
        let allowed = shorter >= 8 ? 2 : 1
        if abs(a.count - b.count) > allowed { return false }
        return editDistance(a, b, limit: allowed) <= allowed
    }

    /// Levenshtein distance, giving up early once it must exceed `limit`.
    public static func editDistance(_ a: String, _ b: String, limit: Int = Int.max) -> Int {
        let x = Array(a.unicodeScalars)
        let y = Array(b.unicodeScalars)
        if x.isEmpty { return y.count }
        if y.isEmpty { return x.count }
        var previous = Array(0...y.count)
        var current = [Int](repeating: 0, count: y.count + 1)
        for i in 1...x.count {
            current[0] = i
            var rowBest = current[0]
            for j in 1...y.count {
                let cost = x[i - 1] == y[j - 1] ? 0 : 1
                current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
                rowBest = min(rowBest, current[j])
            }
            if rowBest > limit { return rowBest }
            swap(&previous, &current)
        }
        return previous[y.count]
    }

    // MARK: - Spoken numbers

    static let unitWordsToDigit: [String: Int] = [
        "zero": 0, "oh": 0, "o": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9,
    ]
    static let teenWords: [String: Int] = [
        "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
        "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    ]
    static let tensWords: [String: Int] = [
        "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60,
        "seventy": 70, "eighty": 80, "ninety": 90,
    ]

    /// Every house number the spoken words could mean.
    ///
    /// People read addresses in chunks: "one twenty three" is 123, "twelve
    /// thirty four" is 1234, "four oh one" is 401, "one hundred five" is 105.
    /// The recognizer may also write the digits itself, sometimes split
    /// ("1 23"). Each run of number words becomes a list of digit pieces, and
    /// every unbroken stretch of pieces is offered as a candidate.
    public static func numberCandidates(_ tokens: [String]) -> Set<String> {
        var candidates = Set<String>()
        var pieces: [String] = []

        func flush() {
            guard !pieces.isEmpty else { return }
            for start in 0..<pieces.count {
                var joined = ""
                for end in start..<pieces.count {
                    joined += pieces[end]
                    if joined.count <= 6 { candidates.insert(joined) }
                }
            }
            pieces = []
        }

        var i = 0
        while i < tokens.count {
            let token = tokens[i]
            if token.allSatisfy({ $0.isNumber }) {
                pieces.append(token)
                i += 1
            } else if let tens = tensWords[token] {
                if i + 1 < tokens.count, let unit = unitWordsToDigit[tokens[i + 1]], unit > 0, tokens[i + 1] != "o", tokens[i + 1] != "oh" {
                    pieces.append(String(tens + unit))
                    i += 2
                } else {
                    pieces.append(String(tens))
                    i += 1
                }
            } else if let teen = teenWords[token] {
                pieces.append(String(teen))
                i += 1
            } else if let unit = unitWordsToDigit[token], !(token == "o" && pieces.isEmpty) {
                pieces.append(String(unit))
                i += 1
            } else if token == "hundred", let last = pieces.last, last.count <= 2, let base = Int(last) {
                // "one hundred twenty three" → 123, "twelve hundred" → 1200.
                var value = base * 100
                var consumed = 1
                var j = i + 1
                if j < tokens.count, tokens[j] == "and" { j += 1; consumed += 1 }
                if j < tokens.count, let tens = tensWords[tokens[j]] {
                    value += tens
                    consumed = j - i + 1
                    if j + 1 < tokens.count, let unit = unitWordsToDigit[tokens[j + 1]], unit > 0 {
                        value += unit
                        consumed += 1
                    }
                } else if j < tokens.count, let teen = teenWords[tokens[j]] {
                    value += teen
                    consumed = j - i + 1
                } else if j < tokens.count, let unit = unitWordsToDigit[tokens[j]] {
                    value += unit
                    consumed = j - i + 1
                } else {
                    consumed = 1
                }
                pieces[pieces.count - 1] = String(value)
                i += consumed
            } else {
                flush()
                i += 1
            }
        }
        flush()
        return candidates
    }
}
