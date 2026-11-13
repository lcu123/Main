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
        // counts as one last name of two words). Case does not matter.
        let tail = fullWords.suffix(lastWords.count)
        let same = zip(tail, lastWords).allSatisfy { $0.lowercased() == $1.lowercased() }
        guard same else { return full }

        let first = fullWords.dropLast(lastWords.count).joined(separator: " ")
        // Keep the spelling from the full name ("McDonald", not "MCDONALD").
        return tail.joined(separator: " ") + ", " + first
    }

    /// The letter for a round badge: the first letter of a name, in capitals.
    public static func initial(_ name: String) -> String {
        guard let first = collapse(name).first(where: { $0.isLetter || $0.isNumber }) else { return "" }
        return String(first).uppercased()
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
}
