import Foundation

/// What cut a recording short, when it was neither the time limit nor the
/// tech's own tap on the ring.
///
/// A phone only says so much about why it stopped the camera. Each case is
/// what the app can really tell, no finer:
///
/// - the phone took the microphone away: a call ringing or under way (the
///   phone app, FaceTime, WhatsApp), Siri, an alarm or a timer. They all
///   look the same to the app, and it does not ask which: the one way to
///   know is Apple's call framework, which the app must never link (it is
///   not allowed in every country's store; `AppSourceRulesTests` checks),
///   so the words say "a call or alarm";
/// - the app left the screen (home, the app switcher, the screen locked, a
///   call that was answered and took the screen over);
/// - another app has the camera (split screen on an iPad);
/// - the phone is too hot or too busy;
/// - the phone ran out of room;
/// - the app itself was stopped mid-clip, and the clip was found at the
///   next start.
public enum RecordingCut: String, Codable, Equatable, Sendable, CaseIterable {
    case callOrAlarm = "call_or_alarm"
    case appClosed = "app_closed"
    case cameraTaken = "camera_taken"
    case phoneBusy = "phone_busy"
    case phoneFull = "phone_full"
    case appStopped = "app_stopped"
    case unknown

    /// A name this build does not know (a record saved by another build)
    /// reads as `unknown`: an unsaved clip is never lost over a word.
    public init(from decoder: Decoder) throws {
        let name = try decoder.singleValueContainer().decode(String.self)
        self = RecordingCut(rawValue: name) ?? .unknown
    }
}

/// What happens to a clip when recording ends, and the words for it.
///
/// The owner's rules: nothing lost, never stuck, one question per screen,
/// plain words, red only while really recording.
///
/// - A clip that ran to the limit, or that the tech ended himself, goes to
///   "Save this video?" as always.
/// - A clip **cut short** by a call, an alarm (or anything else) that is
///   long enough to be worth sending goes to the same "Save this video?"
///   screen, with one plain line saying what stopped it. The tech decides: SAVE or
///   RECORD AGAIN.
/// - A clip cut short too soon to be worth sending is not kept. The camera
///   screen says so in one line, and its one big button records again.
public enum RecordingRule {
    /// A cut clip is worth keeping from this length: three seconds. It is
    /// the same three seconds before which the tech cannot end a clip
    /// himself (`Countdown.earliestStopSeconds`), so the shortest clip a
    /// call can leave is the shortest he could have made on purpose.
    public static let shortestWorthKeeping: TimeInterval = Countdown.earliestStopSeconds

    public enum Next: Equatable, Sendable {
        /// "Save this video?", with a line about what stopped it (if anything did).
        case review(note: String?)
        /// Stay on the camera with this line; the clip is not kept.
        case recordAgain(message: String)
    }

    /// - Parameters:
    ///   - seconds: how long the camera really recorded.
    ///   - cut: what stopped it early; `nil` when nothing did.
    public static func next(seconds: Double, cut: RecordingCut?) -> Next {
        guard let cut else { return .review(note: nil) }
        // A clip found after the app was stopped is kept whatever its
        // length: the tech is not standing at the camera to be told
        // anything, and he can still say RECORD AGAIN.
        if cut != .appStopped, seconds < shortestWorthKeeping {
            return .recordAgain(message: tooShort(cut))
        }
        return .review(note: reviewNote(cut))
    }

    /// The one line on "Save this video?" for a clip that was cut short.
    public static func reviewNote(_ cut: RecordingCut) -> String {
        switch cut {
        case .callOrAlarm: return "A call or alarm stopped this video early."
        case .appClosed, .appStopped: return "This video stopped early when the app closed."
        case .cameraTaken: return "Another app took the camera. This video stopped early."
        case .phoneFull: return "The phone ran out of room. This video stopped early."
        case .phoneBusy: return "The phone stopped this video early."
        case .unknown: return "This video stopped early."
        }
    }

    /// The one line on the camera screen when the clip was too short to keep.
    public static func tooShort(_ cut: RecordingCut) -> String {
        switch cut {
        case .callOrAlarm: return "A call or alarm stopped the video too soon. Please record it again."
        case .appClosed, .appStopped: return "The video stopped when the app closed. Please record it again."
        case .cameraTaken: return "Another app took the camera. Please record it again."
        case .phoneFull: return "The phone ran out of room. The video did not save."
        case .phoneBusy, .unknown: return "The video stopped too soon. Please record it again."
        }
    }

    /// The line on the camera screen while the phone is keeping the camera
    /// from the app. Nothing to do but wait: it comes back by itself.
    /// (An alarm is over in a moment; what keeps the camera away long
    /// enough to be read about is a call.)
    public static func cameraAway(_ cut: RecordingCut) -> String {
        switch cut {
        case .callOrAlarm: return "The camera is off during a call. It comes back by itself."
        case .cameraTaken: return "Another app has the camera. It comes back by itself."
        default: return "The camera is off for a moment. It comes back by itself."
        }
    }

    /// The words on the big button's label while the camera is away.
    public static let cameraAwayLabel = "CAMERA OFF"

    /// A clip that could not be saved at all.
    public static let notSaved = "That video did not save. Please record it again."

    // MARK: Never stuck

    /// How long after the time limit the camera may stay silent before the
    /// app ends the recording itself. The camera reports the end of every
    /// recording; this is for the day it does not, so that the tech is
    /// never left looking at a ring that has run out.
    public static let overdueSeconds: TimeInterval = 4

    /// True when a recording that began `elapsed` seconds ago should have
    /// been reported finished by now.
    public static func isOverdue(elapsed: TimeInterval, maxSeconds: Double) -> Bool {
        elapsed >= maxSeconds + overdueSeconds
    }

    // MARK: Nothing lost

    /// How many times the app tries to turn the camera's file into the
    /// file it sends before giving the clip up. A try can fail because the
    /// app was sent to the background in the middle of it; the next try is
    /// made when the app is opened again.
    public static let saveTries = 3

    /// What to do when the camera's file could not be turned into the file
    /// that is sent.
    /// - Parameter triesSoFar: tries made, this one included.
    /// - Returns: true to keep the camera's file and try again later.
    public static func keepForAnotherTry(triesSoFar: Int) -> Bool {
        triesSoFar < saveTries
    }

    // MARK: The audit trail

    /// What `record_stop` says in its `detail`: how long the clip ran, what
    /// ended it and whether it was kept.
    public static func auditDetail(seconds: Double, cut: RecordingCut?, kept: Bool) -> [String: String] {
        var detail = ["seconds": String(format: "%.1f", max(0, seconds))]
        if let cut {
            detail["endedBy"] = cut.rawValue
            detail["kept"] = kept ? "yes" : "no"
        }
        return detail
    }
}
