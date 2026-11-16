import Foundation

/// The draft stays.
///
/// A clip the tech recorded and has not saved yet is never thrown away
/// because the day changed. The next time he opens the app he gets one
/// question about it: save it or delete it. Only when it is too old for the
/// server to take (14 days) is it dropped by itself, and that is logged.
public enum DraftRule {
    /// The server takes a video up to this many days after its stop.
    public static let keepDays = 14

    public enum Standing: Equatable, Sendable {
        /// Filmed today: SAVE or RECORD AGAIN.
        case today
        /// Filmed on an earlier day: SAVE or DELETE. Recording it again makes
        /// no sense, the tech is not at that stop any more.
        case earlier(daysOld: Int)
        /// Too old to send. Dropped by itself; `draft_discarded` is logged.
        case expired(daysOld: Int)
    }

    /// - Parameters:
    ///   - stopDate: the day the clip was filmed for, `yyyy-MM-dd`.
    ///   - today: the day it is now, `yyyy-MM-dd`.
    public static func standing(stopDate: String, today: String) -> Standing {
        // A date that cannot be read is never a reason to throw a clip away.
        guard let age = ZestDates.daysBetween(stopDate, today), age > 0 else { return .today }
        return age > keepDays ? .expired(daysOld: age) : .earlier(daysOld: age)
    }

    /// RECORD AGAIN is offered: only for a clip filmed today.
    public static func canRecordAgain(_ standing: Standing) -> Bool {
        standing == .today
    }

    /// DELETE is offered in its place for a clip from an earlier day.
    public static func canDelete(_ standing: Standing) -> Bool {
        if case .earlier = standing { return true }
        return false
    }

    /// "Filmed Thursday, Nov 12" for the question about an earlier day's clip.
    public static func filmedText(stopDate: String) -> String? {
        ZestDates.longDay(stopDate).map { "Filmed \($0)" }
    }
}

/// App deleted with videos waiting: warn in the app.
///
/// Deleting the app deletes every video that is still on the phone. While
/// there is one, My Route and My Videos say so in plain words.
public enum KeepApp {
    /// For My Videos. `nil` when nothing is on the phone.
    /// - Parameter videosOnPhone: videos waiting or sending, plus a clip
    ///   that is recorded and not saved yet.
    public static func warning(videosOnPhone: Int) -> String? {
        guard videosOnPhone > 0 else { return nil }
        let videos = videosOnPhone == 1 ? "1 video is" : "\(videosOnPhone) videos are"
        return "Do not delete this app. \(videos) still on this phone."
    }

    /// For the banner on My Route. `nil` when nothing is waiting.
    public static func waitingBanner(waiting: Int) -> String? {
        guard waiting > 0 else { return nil }
        let videos = waiting == 1 ? "1 video" : "\(waiting) videos"
        return "\(videos) waiting to send. Do not delete this app."
    }
}
