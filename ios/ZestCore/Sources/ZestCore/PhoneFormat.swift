import Foundation

/// US mobile numbers on the sign-in screen: shown grouped as the tech types,
/// sent to the server as ten plain digits.
public enum PhoneFormat {
    /// The ten digits of a number, from whatever was typed, pasted or
    /// filled in by the phone ("+1 (916) 555-0100" → "9165550100").
    /// A leading 1 is the country code: no US area code starts with 1.
    public static func digits(from text: String) -> String {
        var digits = text.filter { $0.isASCII && $0.isNumber }
        while digits.hasPrefix("1") {
            digits.removeFirst()
        }
        return String(digits.prefix(10))
    }

    /// "(916) 555-0100", built up as the digits arrive.
    ///
    /// The text always ends in a digit, never in a bracket, space or dash, so
    /// the delete key always takes off a digit and can never get stuck.
    public static func display(_ digits: String) -> String {
        let d = Array(digits.prefix(10))
        switch d.count {
        case 0:
            return ""
        case 1...3:
            return "(" + String(d)
        case 4...6:
            return "(" + String(d[0..<3]) + ") " + String(d[3...])
        default:
            return "(" + String(d[0..<3]) + ") " + String(d[3..<6]) + "-" + String(d[6...])
        }
    }

    /// All ten digits are there.
    public static func isComplete(_ digits: String) -> Bool {
        digits.count == 10
    }

    // MARK: What the tech typed, checked when SEND MY CODE is tapped

    /// The sign-in field holds exactly what was typed, pasted or filled in by
    /// the phone. It is never rewritten while it is being edited (rewriting
    /// it loses keystrokes); it is read once, here, when the tech taps send.
    public enum Entry: Equatable, Sendable {
        /// A whole US mobile number, as ten plain digits.
        case ok(String)
        /// Nothing typed yet.
        case empty
        /// Fewer than ten digits.
        case tooShort
        /// More than ten digits (after a leading +1 or 1 is taken off).
        case tooLong
    }

    /// Every ASCII digit in the text, in order. Spaces, dashes, dots,
    /// brackets and a plus sign are skipped.
    static func allDigits(in text: String) -> String {
        text.filter { $0.isASCII && $0.isNumber }
    }

    /// The digits with the country code taken off: one leading 1, when there
    /// are more than ten digits or the text began with a plus sign.
    static func nationalDigits(in text: String) -> String {
        var digits = allDigits(in: text)
        let hadPlus = text.trimmingCharacters(in: .whitespaces).hasPrefix("+")
        if digits.hasPrefix("1") && (digits.count > 10 || hadPlus) {
            digits.removeFirst()
        }
        return digits
    }

    /// Reads the field. "+1 (916) 555-0100", "1-916-555-0100" and
    /// "916.555.0100" are all the same ten digits.
    public static func check(_ text: String) -> Entry {
        let digits = nationalDigits(in: text)
        if digits.isEmpty { return allDigits(in: text).isEmpty ? .empty : .tooShort }
        if digits.count < 10 { return .tooShort }
        if digits.count > 10 { return .tooLong }
        // No US area code starts with 1: ten digits that do are the country
        // code and nine digits, so one digit is still missing.
        if digits.hasPrefix("1") { return .tooShort }
        return .ok(digits)
    }

    /// The number grouped for reading back, shown under the field as the tech
    /// types: "(916) 555-0100". Digits past the tenth are shown after a
    /// space, so one digit too many can be seen and taken off.
    public static func preview(_ text: String) -> String {
        let digits = nationalDigits(in: text)
        let grouped = display(String(digits.prefix(10)))
        guard digits.count > 10 else { return grouped }
        return grouped + " " + String(digits.dropFirst(10))
    }
}

/// The six-digit sign-in code. One hidden text field takes whatever arrives
/// (typed fast, pasted with words round it, or filled in all at once by the
/// phone from the text message); the six boxes show what this makes of it.
public enum CodeEntry {
    public static let length = 6

    /// The first six ASCII digits in the text, in order.
    public static func digits(from text: String) -> String {
        String(text.filter { $0.isASCII && $0.isNumber }.prefix(length))
    }

    /// All six are there: time to send it.
    public static func isComplete(_ digits: String) -> Bool {
        digits.count == length
    }

    /// Should this text be sent to the server now? Only a whole code, and
    /// only once for the same six digits, however many times the field
    /// reports a change (a paste and a rewrite both report one).
    public static func shouldSubmit(text: String, lastSubmitted: String?) -> String? {
        let digits = digits(from: text)
        guard isComplete(digits), digits != lastSubmitted else { return nil }
        return digits
    }
}
