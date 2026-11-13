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
}
