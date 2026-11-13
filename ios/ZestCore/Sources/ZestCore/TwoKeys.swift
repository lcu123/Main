import Foundation

/// The two-keys rule: three signals are checked on the phone right after
/// recording, while the tech is still at the stop. If at least two agree on
/// the customer the video goes with nobody else involved. If not, the tech
/// sees one warning and fixes it himself (or sends anyway, which is logged).
public enum TwoKeys {
    public enum Key: String, CaseIterable, Equatable, Sendable {
        case spoken
        case gps
        case checkIn
    }

    public struct Verdict: Equatable, Sendable {
        /// Two or more signals matched: send with no warning.
        public let clean: Bool
        public let matches: Int
        /// Signals that actively disagreed (not merely unknown).
        public let mismatched: [Key]
        /// Signals that could not be read.
        public let unknown: [Key]

        /// One short line per problem, in plain words for the warning screen.
        public var reasons: [String] {
            var lines: [String] = []
            if mismatched.contains(.spoken) { lines.append("I did not hear this name or street.") }
            if mismatched.contains(.gps) { lines.append("You were far from this address.") }
            if mismatched.contains(.checkIn) { lines.append("The time does not match your check-in.") }
            if lines.isEmpty { lines.append("I could not check this is the right stop.") }
            return lines
        }
    }

    public static let requiredMatches = 2

    public static func verdict(_ signals: Signals) -> Verdict {
        let all: [(Key, SignalResult)] = [(.spoken, signals.spoken), (.gps, signals.gps), (.checkIn, signals.checkIn)]
        let matches = all.filter { $0.1 == .match }.count
        return Verdict(
            clean: matches >= requiredMatches,
            matches: matches,
            mismatched: all.filter { $0.1 == .mismatch }.map { $0.0 },
            unknown: all.filter { $0.1 == .unknown }.map { $0.0 }
        )
    }

    /// GPS key: was the phone near the stop's map pin when recording began?
    ///
    /// A fix with a very wide error circle says nothing either way. The error
    /// circle (capped) is added to the allowed distance so a fuzzy fix on the
    /// right lawn is not called a mismatch.
    public static func gpsSignal(fix: VideoLocation?, stop: Stop, farMeters: Double) -> SignalResult {
        guard let fix, let pin = stop.coordinate else { return .unknown }
        if fix.accuracyM < 0 || fix.accuracyM > 500 { return .unknown }
        let distance = Geo.distanceMeters(fix.coordinate, pin)
        return distance <= farMeters + min(fix.accuracyM, 100) ? .match : .mismatch
    }

    /// For the confirm screen, before recording: is the phone clearly far away?
    /// `nil` when there is nothing to compare.
    public static func isFar(fix: VideoLocation?, stop: Stop, farMeters: Double) -> Bool? {
        switch gpsSignal(fix: fix, stop: stop, farMeters: farMeters) {
        case .match: return false
        case .mismatch: return true
        case .unknown: return nil
        }
    }

    public static let checkInSlackSeconds: TimeInterval = 20 * 60

    /// Check-in key: does the record time fall inside the stop's FieldRoutes
    /// check-in window? Check-in is a hint, not proof (some stops are checked
    /// in and out a second apart), so the window gets generous slack.
    public static func checkInSignal(recordedAt: Date, stop: Stop, slack: TimeInterval = checkInSlackSeconds) -> SignalResult {
        guard let checkIn = stop.checkIn else { return .unknown }
        let opens = checkIn.addingTimeInterval(-slack)
        if recordedAt < opens { return .mismatch }
        guard let checkOut = stop.checkOut else { return .match }
        return recordedAt <= checkOut.addingTimeInterval(slack) ? .match : .mismatch
    }

    /// All three signals for one stop. `transcript` is `nil` when
    /// speech-to-text did not run.
    public static func signals(
        transcript: String?, fix: VideoLocation?, recordedAt: Date, stop: Stop, all: [Stop], farMeters: Double
    ) -> (signals: Signals, better: Stop?) {
        let spoken = SpokenMatcher.evaluate(transcript: transcript, target: stop, all: all)
        let signals = Signals(
            spoken: spoken.result,
            gps: gpsSignal(fix: fix, stop: stop, farMeters: farMeters),
            checkIn: checkInSignal(recordedAt: recordedAt, stop: stop)
        )
        return (signals, spoken.better)
    }
}

/// The undo window. For `holdMinutes` after recording, the tech can record
/// again or delete the video. After that it has gone to the customer and only
/// the office can change it.
public enum HoldWindow {
    public static func closesAt(recordedAt: Date, holdMinutes: Int) -> Date {
        recordedAt.addingTimeInterval(TimeInterval(max(holdMinutes, 0) * 60))
    }

    public static func secondsLeft(recordedAt: Date, now: Date, holdMinutes: Int) -> Int {
        max(0, Int(closesAt(recordedAt: recordedAt, holdMinutes: holdMinutes).timeIntervalSince(now).rounded(.up)))
    }

    /// True while Record again and Delete are still offered.
    public static func isOpen(recordedAt: Date, now: Date, holdMinutes: Int) -> Bool {
        // A clock set forward on the phone must not open the window for good.
        if now < recordedAt.addingTimeInterval(-60) { return false }
        return secondsLeft(recordedAt: recordedAt, now: now, holdMinutes: holdMinutes) > 0
    }

    /// Same question when only the server's closing time is known
    /// (a video the phone no longer has a record of).
    public static func isOpen(closesAt: Date?, now: Date) -> Bool {
        guard let closesAt else { return false }
        return now < closesAt
    }

    /// Whole minutes left, rounded up, so "1" is shown until it closes.
    public static func minutesLeft(secondsLeft: Int) -> Int {
        secondsLeft <= 0 ? 0 : (secondsLeft + 59) / 60
    }

    /// "You can change it for 7 more minutes."
    public static func text(secondsLeft: Int) -> String {
        let minutes = minutesLeft(secondsLeft: secondsLeft)
        switch minutes {
        case 0: return "It has gone to the customer."
        case 1: return "You can change it for 1 more minute."
        default: return "You can change it for \(minutes) more minutes."
        }
    }
}
