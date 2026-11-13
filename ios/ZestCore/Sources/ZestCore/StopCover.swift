import Foundation

/// One video per tech, per property, per day.
///
/// When two techs visit the same property on the same day, each sends his
/// own video and the customer gets both. One tech with two or three
/// appointments there still sends one video. A second video from the same
/// tech replaces his first, and nobody else's.
///
/// So the only video a tech can change or delete is his own. The server
/// shows him his own video for a stop, or none. A teammate's video is shown
/// to him only where he has no appointment of his own (he is helping), and
/// there it is something to know about, never something to touch.
public enum StopCover: Equatable, Sendable {
    /// No video for this tech to see: the stop is his to record.
    case open
    /// His own video, on the server. `emailAt` is when it goes to the
    /// customer, if the server said.
    case mine(StopVideo, emailAt: Date?)
    /// A teammate's video, shown to a helper. The stop is covered. He can
    /// still record his own: it is kept beside the first and the customer
    /// gets both.
    case team(StopVideo)

    /// What the tech can do at the stop.
    public enum Standing: Equatable, Sendable {
        /// Nothing here yet: record.
        case record
        /// His own video, inside the undo window: record again (which
        /// replaces his own) or delete it.
        case change(closesAt: Date)
        /// His own video, gone to the customer: only the office can change it.
        case locked
        /// A teammate's video, whatever became of it: nothing to change,
        /// nothing to delete, and his own recording is a new video.
        case teamCovered
    }

    /// Reads one stop from the server's answer for the day.
    ///
    /// - Parameter ownVideos: `videos` from `/v1/today`, which lists this
    ///   tech's own videos only. If the stop shows a teammate's video but
    ///   he has one of his own for that customer, his own is the one that
    ///   counts for him: he must never be offered a second "first" video.
    public static func of(_ stop: Stop, ownVideos: [TodayVideo] = []) -> StopCover {
        guard let shown = stop.liveVideo else { return .open }
        func emailAt(_ id: String) -> Date? {
            ownVideos.first { $0.id == id }?.emailAt
        }
        if shown.byYou {
            return .mine(shown, emailAt: emailAt(shown.id))
        }
        if let own = ownVideos.last(where: { $0.customerId == stop.customerId && !$0.state.isGone }) {
            return .mine(StopVideo(id: own.id, state: own.state, byYou: true), emailAt: own.emailAt)
        }
        return .team(shown)
    }

    public func standing(now: Date) -> Standing {
        switch self {
        case .open:
            return .record
        case .team:
            return .teamCovered
        case .mine(let video, let emailAt):
            // Still on its way out, and the ten minutes are not up.
            let notOutYet = video.state == .held || video.state == .awaitingUpload
            if notOutYet, let emailAt, HoldWindow.isOpen(closesAt: emailAt, now: now) {
                return .change(closesAt: emailAt)
            }
            return .locked
        }
    }

    /// The video the stop shows this tech, if any.
    public var video: StopVideo? {
        switch self {
        case .open: return nil
        case .mine(let video, _), .team(let video): return video
        }
    }

    /// A video counts for the stop: nothing is waiting on this tech.
    public var isCovered: Bool { self != .open }

    /// The video is this tech's own.
    public var isMine: Bool {
        if case .mine = self { return true }
        return false
    }
}

public extension StopCover.Standing {
    /// He can start a recording here that replaces nothing.
    var canRecordNew: Bool { self == .record || self == .teamCovered }

    /// RECORD AGAIN (replacing a video) is offered.
    var canRecordAgain: Bool {
        if case .change = self { return true }
        return false
    }

    /// DELETE VIDEO is offered. Only ever for his own.
    var canDelete: Bool { canRecordAgain }
}
