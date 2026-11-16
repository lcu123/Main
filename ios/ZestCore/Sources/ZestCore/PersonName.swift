import Foundation

/// Names the way a route list shows them: "Smith, Jane".
///
/// Techs already read customers last-name-first every day, and the last name
/// is also what they say at the start of each video.
public enum PersonName {
    /// "Jane Smith" with last name "Smith" → "Smith, Jane".
    ///
    /// The name is left exactly as it is whenever it cannot be turned round
    /// safely: a business (no last name), a name that does not end with the
    /// last name on file, a name that is only the last name, or one that is
    /// already written with a comma.
    public static func listed(name: String, lastName: String) -> String {
        let full = collapse(name)
        let last = collapse(lastName)
        guard !last.isEmpty, !full.isEmpty, !full.contains(",") else { return full }

        let fullWords = full.split(separator: " ")
        let lastWords = last.split(separator: " ")
        guard fullWords.count > lastWords.count else { return full }

        // The name must end with the last name, word for word ("Van Dyke"
        // counts as one last name of two words). Case, accents and the kind
        // of apostrophe or dash do not matter: "Peña" is "PENA", and
        // "O’Brien" is "O'Brien".
        let tail = fullWords.suffix(lastWords.count)
        let same = zip(tail, lastWords).allSatisfy { key(String($0)) == key(String($1)) }
        guard same else { return full }

        let first = fullWords.dropLast(lastWords.count).joined(separator: " ")
        // Keep the spelling from the full name ("McDonald", not "MCDONALD").
        return tail.joined(separator: " ") + ", " + first
    }

    /// What the tech calls the customer in "Hi …": the first name.
    ///
    /// "Jane Smith" with last name "Smith" → "Jane". "Mary Ann Smith" →
    /// "Mary Ann". A business (no last name) is called by its whole name. A
    /// name already written "Smith, Jane" gives "Jane". When the last name
    /// on file is not at the end of the name, the first word is used. An
    /// empty name gives "".
    public static func first(name: String, lastName: String) -> String {
        let full = collapse(name)
        let last = collapse(lastName)
        guard !full.isEmpty else { return "" }
        if let comma = full.firstIndex(of: ",") {
            let after = collapse(String(full[full.index(after: comma)...]))
            return after.isEmpty ? collapse(String(full[..<comma])) : after
        }
        guard !last.isEmpty else { return full }

        let fullWords = full.split(separator: " ")
        let lastWords = last.split(separator: " ")
        // Only the last name: there is nothing else to call them.
        guard fullWords.count > lastWords.count else { return full }
        let tail = fullWords.suffix(lastWords.count)
        if zip(tail, lastWords).allSatisfy({ key(String($0)) == key(String($1)) }) {
            return fullWords.dropLast(lastWords.count).joined(separator: " ")
        }
        return String(fullWords[0])
    }

    /// The letter for a round badge: the first letter of a name, in capitals.
    public static func initial(_ name: String) -> String {
        guard let first = collapse(name).first(where: { $0.isLetter || $0.isNumber }) else { return "" }
        return String(first).uppercased()
    }

    /// A word reduced to what has to match: no case, no accents, one kind
    /// of apostrophe and one kind of dash.
    static func key(_ word: String) -> String {
        let folded = word.folding(options: [.diacriticInsensitive, .caseInsensitive], locale: Locale(identifier: "en_US")).lowercased()
        return String(folded.map { character -> Character in
            switch character {
            case "\u{2019}", "\u{2018}", "\u{02BC}", "`", "\u{00B4}": return "'"
            case "\u{2010}", "\u{2011}", "\u{2012}", "\u{2013}", "\u{2014}": return "-"
            default: return character
            }
        })
    }

    /// Trims the ends and squeezes runs of spaces and tabs to one space.
    static func collapse(_ text: String) -> String {
        text.split(whereSeparator: { $0 == " " || $0 == "\t" || $0 == "\n" }).joined(separator: " ")
    }
}

public extension Stop {
    /// The customer's name for lists and headings: "Smith, Jane".
    var listName: String {
        PersonName.listed(name: name, lastName: lastName)
    }

    /// What the tech calls the customer: "Jane". A business's whole name.
    var greetingName: String {
        PersonName.first(name: name, lastName: lastName)
    }
}

/// The lines shown while recording: what to say, one at a time.
///
/// The server sends them; `{name}` in a line stands for the customer's first
/// name, so the first line reads "Hi Jane".
public enum PromptText {
    public static let namePlaceholder = "{name}"

    /// Puts the name in. With no name the placeholder goes cleanly:
    /// "Hi {name}" → "Hi", "Hi {name}, here is what I did" → "Hi, here is
    /// what I did".
    public static func fill(_ prompt: String, name: String?) -> String {
        let name = PersonName.collapse(name ?? "")
        var text = prompt.replacingOccurrences(of: namePlaceholder, with: name)
        text = PersonName.collapse(text)
        if name.isEmpty {
            // No space left hanging before a comma or a full stop, and no
            // comma left hanging at the end.
            for mark in [",", ".", "!", "?", ":", ";"] {
                text = text.replacingOccurrences(of: " " + mark, with: mark)
            }
            // "Thanks, {name}!" → "Thanks!", not "Thanks,!".
            for mark in [".", "!", "?"] {
                text = text.replacingOccurrences(of: "," + mark, with: mark)
            }
            while let last = text.last, last == "," || last == ":" || last == ";" || last == " " {
                text.removeLast()
            }
            while let first = text.first, first == "," || first == " " {
                text.removeFirst()
            }
        }
        return text
    }

    /// Every line, with the name in.
    public static func fill(_ prompts: [String], name: String?) -> [String] {
        prompts.map { fill($0, name: name) }
    }
}
