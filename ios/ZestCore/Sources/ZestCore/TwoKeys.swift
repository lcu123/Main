import Foundation

/// The two-keys rule: three signals are checked on the phone right after
/// recording, while the tech is still at the stop. If at least two agree on
/// the customer the video goes with nobody else involved. If not, the tech
/// sees one warning and fixes it himself (or sends anyway, which is logged).
///
/// The three keys are not equally good. Three real days of this office's
/// check-ins (106 stops, 4 techs) showed what the check-in is worth:
///
/// - A check-in that MATCHES says little. The median drive between stops is
///   16.6 minutes, so a video filmed at the right stop also falls inside a
///   neighbouring stop's check-in window for 46 to 59% of stops when 20
///   minutes of slack are allowed, 25 to 32% with 10, and 15 to 17% with 5.
/// - A check-in that does NOT match means something.
/// - Two of the four techs tap check-in and check-out within a minute at
///   about a fifth of their stops (22 of 106), and 6 of 106 stops were left
///   checked in for over 90 minutes (104 to 347).
///
/// So the check-in is a supporting key only. It can be the second key
/// beside the spoken name or the GPS; it can never send a video on its own.
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
            if mismatched.contains(.spoken) { lines.append("I did not hear this customer's name.") }
            if mismatched.contains(.gps) { lines.append("You were far from this address.") }
            if mismatched.contains(.checkIn) { lines.append("The time does not match your check-in.") }
            if lines.isEmpty { lines.append("I could not check this is the right stop.") }
            return lines
        }
    }

    public static let requiredMatches = 2

    /// - Parameter spokenMiss: what kind of spoken mismatch it was, if it
    ///   was one. When the words named another stop, the verdict is never
    ///   clean.
    public static func verdict(_ signals: Signals, spokenMiss: SpokenMiss? = nil) -> Verdict {
        verdict(signals, required: requiredMatches, spokenMiss: spokenMiss)
    }

    /// The verdict for one stop.
    ///
    /// Two keys must agree, but only keys that *can* be read for this stop
    /// count towards the two. A stop with no map pin has no GPS key; a stop
    /// with no check-in has no check-in key. When the spoken key is the only
    /// one there is, a spoken match is enough: asking for a second key that
    /// cannot exist would warn on every video for that stop for ever.
    ///
    /// One thing overrides all of it. When the words fit ANOTHER stop on
    /// today's list better (`spokenMiss == .otherStop`: he said a different
    /// customer's name), the video is never sent without asking, whatever
    /// the GPS and the check-in say and whatever kind of stop it is. The GPS
    /// cannot tell next-door houses apart and a neighbour's check-in window
    /// is often still open, so both can agree with the wrong house; the
    /// words are the one key that can tell. A mismatch because no name was
    /// heard at all (`.noName`) is only a missing key: with the GPS and the
    /// check-in both agreeing, the video still goes.
    public static func verdict(_ signals: Signals, stop: Stop, spokenMiss: SpokenMiss? = nil) -> Verdict {
        var readable = 1                                  // the spoken key
        if stop.coordinate != nil { readable += 1 }       // the GPS key
        if stop.checkIn != nil { readable += 1 }          // the check-in key
        return verdict(signals, required: min(requiredMatches, readable), spokenMiss: spokenMiss)
    }

    private static func verdict(_ signals: Signals, required: Int, spokenMiss: SpokenMiss?) -> Verdict {
        let all: [(Key, SignalResult)] = [(.spoken, signals.spoken), (.gps, signals.gps), (.checkIn, signals.checkIn)]
        let matches = all.filter { $0.1 == .match }.count
        // The check-in only ever supports: one of the keys that match has
        // to be the spoken name or the GPS, however few keys are asked for.
        let strongMatch = signals.spoken == .match || signals.gps == .match
        // He said another customer's name: never without asking.
        let contradicted = spokenMiss == .otherStop
        return Verdict(
            clean: matches >= required && strongMatch && !contradicted,
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
        guard let fix = usable(fix), let pin = stop.coordinate else { return .unknown }
        let distance = Geo.distanceMeters(fix.coordinate, pin)
        return distance <= farMeters + min(fix.accuracyM, 100) ? .match : .mismatch
    }

    /// A fix worth going by: one whose error circle is known and under 500
    /// metres. A wider one says nothing about which house, or even which
    /// street, the phone is at.
    public static func usable(_ fix: VideoLocation?) -> VideoLocation? {
        guard let fix, fix.accuracyM >= 0, fix.accuracyM <= 500 else { return nil }
        return fix
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

    /// How far outside the check-in window a video may be filmed and still
    /// count as matching it: 10 minutes. It was 20; with the median drive
    /// between stops at 16.6 minutes, 20 let a video at the right stop match
    /// a neighbour's window too for about half of all stops (see the figures
    /// at the top of this file). Some slack is still needed: a fifth of
    /// stops are checked in and out within the same minute.
    public static let checkInSlackSeconds: TimeInterval = 10 * 60

    /// How long a check-in that was never closed is taken at its word: 90
    /// minutes. In the real days, 6 of 106 stops were left checked in for
    /// longer (104 to 347 minutes), the tech long gone to other houses.
    /// After this an open check-in says nothing either way.
    public static let openCheckInTrustSeconds: TimeInterval = 90 * 60

    /// Check-in key: does the record time fall inside the stop's FieldRoutes
    /// check-in window? Check-in is a hint, not proof (some stops are checked
    /// in and out a second apart), so the window gets some slack.
    public static func checkInSignal(recordedAt: Date, stop: Stop, slack: TimeInterval = checkInSlackSeconds) -> SignalResult {
        guard let checkIn = stop.checkIn else { return .unknown }
        let opens = checkIn.addingTimeInterval(-slack)
        if recordedAt < opens { return .mismatch }
        guard let checkOut = stop.checkOut else {
            // Still open. Left open for hours, it tells nothing about where
            // the tech is now: neither for nor against.
            return recordedAt <= checkIn.addingTimeInterval(openCheckInTrustSeconds) ? .match : .unknown
        }
        return recordedAt <= checkOut.addingTimeInterval(slack) ? .match : .mismatch
    }

    /// All three signals for one stop. `transcript` is `nil` when
    /// speech-to-text did not run.
    /// - Parameter techName: the tech's own first name. If he says it ("this
    ///   is Sam") it is not taken for a customer called Sam at another stop.
    public static func signals(
        transcript: String?, fix: VideoLocation?, recordedAt: Date, stop: Stop, all: [Stop], farMeters: Double,
        techName: String? = nil
    ) -> (signals: Signals, better: Stop?, spokenMiss: SpokenMiss?) {
        let spoken = SpokenMatcher.evaluate(transcript: transcript, target: stop, all: all, techName: techName)
        let signals = Signals(
            spoken: spoken.result,
            gps: gpsSignal(fix: fix, stop: stop, farMeters: farMeters),
            checkIn: checkInSignal(recordedAt: recordedAt, stop: stop)
        )
        return (signals, spoken.better, spoken.miss)
    }

    /// For the audit trail: the three keys, and for a spoken mismatch which
    /// kind it was (`spokenMiss`: "other_stop" or "no_name") and, when the
    /// words pointed at one other stop, that customer (`soundedLike`). The
    /// office can then tell "he said the neighbour's name" from "he forgot
    /// to say a name".
    public static func auditDetail(_ signals: Signals, spokenMiss: SpokenMiss?, better: Stop?) -> [String: String] {
        var detail = ["spoken": signals.spoken.rawValue, "gps": signals.gps.rawValue, "checkIn": signals.checkIn.rawValue]
        if signals.spoken == .mismatch, let spokenMiss {
            detail["spokenMiss"] = spokenMiss.rawValue
            if spokenMiss == .otherStop, let better {
                detail["soundedLike"] = String(better.customerId)
            }
        }
        return detail
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
    /// - Parameter toOffice: the video was filmed as "not on my list": it
    ///   goes to the office to be matched, not to a customer.
    public static func text(secondsLeft: Int, toOffice: Bool = false) -> String {
        let minutes = minutesLeft(secondsLeft: secondsLeft)
        switch minutes {
        case 0: return toOffice ? "It has gone to the office." : "It has gone to the customer."
        case 1: return "You can change it for 1 more minute."
        default: return "You can change it for \(minutes) more minutes."
        }
    }
}
