import Foundation

/// Time helpers. The server speaks ISO 8601 UTC (`2026-11-13T18:04:05Z`).
public enum ZestDates {
    /// Parses an ISO 8601 UTC time, with or without fractional seconds.
    public static func parseISO(_ text: String) -> Date? {
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        if let date = plain.date(from: text) { return date }
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return fractional.date(from: text)
    }

    /// Formats a time as ISO 8601 UTC with whole seconds.
    public static func formatISO(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.string(from: date)
    }

    /// `yyyy-MM-dd` for a moment in a time zone. The route cache is keyed by this.
    public static func dayString(_ date: Date, timeZone: TimeZone = .current) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        let parts = calendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", parts.year ?? 0, parts.month ?? 0, parts.day ?? 0)
    }

    /// "Thursday, Nov 12" for a route's `yyyy-MM-dd` date. The date is a
    /// calendar day, not a moment, so no time zone can shift it.
    public static func longDay(_ day: String) -> String? {
        let parts = day.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3, (1...12).contains(parts[1]), (1...31).contains(parts[2]) else { return nil }
        var calendar = Calendar(identifier: .gregorian)
        guard let utc = TimeZone(identifier: "UTC") else { return nil }
        calendar.timeZone = utc
        var components = DateComponents()
        components.year = parts[0]
        components.month = parts[1]
        components.day = parts[2]
        guard let date = calendar.date(from: components),
              calendar.component(.day, from: date) == parts[2] else { return nil }
        let weekdays = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
        let months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        let weekday = calendar.component(.weekday, from: date)
        return "\(weekdays[weekday - 1]), \(months[parts[1] - 1]) \(parts[2])"
    }

    /// True when a cached `/v1/today` body is still for the phone's current day.
    public static func isToday(_ day: String, now: Date = Date(), timeZone: TimeZone = .current) -> Bool {
        day == dayString(now, timeZone: timeZone)
    }
}

/// JSON coders shared by the network layer and the on-disk queue.
public enum ZestJSON {
    public static func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let text = try container.decode(String.self)
            guard let date = ZestDates.parseISO(text) else {
                throw DecodingError.dataCorruptedError(in: container, debugDescription: "Not an ISO 8601 time: \(text)")
            }
            return date
        }
        return decoder
    }

    public static func encoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .custom { date, encoder in
            var container = encoder.singleValueContainer()
            try container.encode(ZestDates.formatISO(date))
        }
        return encoder
    }
}

extension KeyedDecodingContainer {
    /// Decodes a value, falling back to a default when the key is missing,
    /// null or the wrong shape. One odd field must never cost the tech his
    /// whole route for the day.
    func value<T: Decodable>(_ key: Key, default fallback: T) -> T {
        ((try? decodeIfPresent(T.self, forKey: key)) ?? nil) ?? fallback
    }
}
