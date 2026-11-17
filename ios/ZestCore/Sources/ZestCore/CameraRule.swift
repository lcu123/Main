import Foundation

/// What is known about the camera at one moment. Every value is asked for
/// at that moment; none is remembered from an earlier look.
public struct CameraFacts: Equatable, Sendable {
    /// A screen wants the camera.
    public var wanted: Bool
    /// The phone's capture session says it is running.
    public var running: Bool
    /// The capture session says the phone has taken the camera for now (a
    /// call, another app, the app off the screen).
    public var interrupted: Bool
    /// A clip is being recorded, or its start is on its way to the camera.
    public var recording: Bool
    /// A start of the session is already on its way.
    public var starting: Bool
    /// The camera has been given up on until someone asks for it again.
    public var failed: Bool
    /// How many times the app has started or stopped the session by itself
    /// lately (see `CameraRule.triesWindow`), with nobody asking.
    public var triesLately: Int
    /// The app is in front. Nothing is started while it is not.
    public var inFront: Bool

    public init(
        wanted: Bool, running: Bool, interrupted: Bool, recording: Bool = false, starting: Bool = false,
        failed: Bool = false, triesLately: Int = 0, inFront: Bool = true
    ) {
        self.wanted = wanted
        self.running = running
        self.interrupted = interrupted
        self.recording = recording
        self.starting = starting
        self.failed = failed
        self.triesLately = triesLately
        self.inFront = inFront
    }
}

/// What the record screen may say about the camera, and what the app does
/// to bring the camera back, decided from the facts and from nothing else.
///
/// The first build on a real phone showed the record screen black with TAP
/// TO RECORD under the button: the app remembered "ready" from the moment
/// the session started, the session had since stopped, and nothing ever
/// asked it again. Every tap then failed with "The video stopped too soon."
/// So "ready" is no longer remembered. It is worked out here, each time,
/// from what the session itself says:
///
/// - TAP TO RECORD is offered only while the session says it is running.
/// - A session that has stopped while a screen wants it is started again
///   with no touch from the tech, a few times at most; after that the
///   screen says the camera could not start and offers TRY AGAIN.
/// - A session the phone has interrupted is waited for: it comes back by
///   itself, and the screen says CAMERA OFF meanwhile.
/// - A session found running when no screen wants it is stopped.
public enum CameraRule {
    /// What the screen shows.
    public enum Standing: Equatable, Sendable {
        /// A clip is being recorded: leave everything as it is.
        case asIs
        /// No screen wants the camera.
        case off
        /// STARTING CAMERA. The button waits.
        case starting
        /// TAP TO RECORD.
        case ready
        /// CAMERA OFF: the phone has it for now. The button waits.
        case away
        /// "The camera could not start.", with TRY AGAIN.
        case failed
    }

    /// What the app does next, by itself.
    public enum Step: Equatable, Sendable {
        case nothing
        /// Start the session.
        case start
        /// Stop the session: nobody wants it.
        case stop
    }

    public struct Verdict: Equatable, Sendable {
        public var standing: Standing
        public var step: Step

        public init(_ standing: Standing, _ step: Step = .nothing) {
            self.standing = standing
            self.step = step
        }
    }

    /// How many times in a row the app starts (or stops) the session by
    /// itself before it stops trying and says so.
    public static let mostTries = 3
    /// "In a row" means within this many seconds.
    public static let triesWindow: TimeInterval = 10
    /// The pause before the second and third try.
    public static let secondsBetweenTries: TimeInterval = 0.7
    /// While a screen wants the camera, the session is asked how it is
    /// this often, so the screen cannot be wrong for longer.
    public static let watchSeconds: TimeInterval = 1

    public static func settle(_ facts: CameraFacts) -> Verdict {
        // A clip in progress is the file's business until it is handed
        // over; the camera is looked at again the moment it is.
        if facts.recording {
            return Verdict(.asIs)
        }
        guard facts.wanted else {
            // Nobody wants it. A session still running (the phone starts an
            // interrupted one again by itself) is stopped.
            let stop = facts.running && facts.triesLately < mostTries
            return Verdict(.off, stop ? .stop : .nothing)
        }
        if facts.running {
            return Verdict(.ready)
        }
        if facts.interrupted {
            return Verdict(.away)
        }
        if facts.starting {
            return Verdict(.starting)
        }
        if facts.failed || facts.triesLately >= mostTries {
            return Verdict(.failed)
        }
        // Stopped, and nothing is on its way: start it. (Not behind the
        // tech's back: with the app off the screen it waits.)
        return Verdict(.starting, facts.inFront ? .start : .nothing)
    }

    // MARK: The words

    /// Under the big button when it can be tapped.
    public static let readyLabel = "TAP TO RECORD"
    /// Under the big button between the tap and the first frame.
    public static let armedLabel = "GET READY"
    /// Under the big button while the camera is starting, or starting again.
    public static let startingLabel = "STARTING CAMERA"

    /// The words under the big record button.
    /// - Parameters:
    ///   - ready: the camera is running (`Standing.ready`).
    ///   - armed: the button was tapped and the clip has not begun.
    ///   - away: the phone has the camera for now.
    public static func label(ready: Bool, armed: Bool, away: Bool) -> String {
        if armed { return armedLabel }
        if ready { return readyLabel }
        return away ? RecordingRule.cameraAwayLabel : startingLabel
    }

    /// The record button can be tapped only while the camera is running and
    /// no clip is on its way.
    public static func offersRecord(ready: Bool, armed: Bool) -> Bool {
        ready && !armed
    }

    /// The one line on the camera screen when a tap on the record button
    /// started nothing: the camera was not running and could not be started
    /// in time. Nothing was recorded, so nothing "stopped too soon".
    public static let didNotStart = "The camera did not start. Tap again."

    /// The page shown when the camera will not start after several tries.
    public static let couldNotStart = "The camera could not start."

    /// What to say after a tap that started nothing. While the phone has
    /// the camera (a call) the screen already says so, and that line stays.
    public static func didNotStartLine(away: RecordingCut?) -> String? {
        away == nil ? didNotStart : nil
    }

    // MARK: The audit trail

    /// What `record_stop` says for a tap that started nothing: no clip, not
    /// kept, and why the camera did not record ("stopped", "refused -11803").
    public static let notStarted = "not_started"

    public static func notStartedDetail(why: String) -> [String: String] {
        ["seconds": "0.0", "endedBy": notStarted, "kept": "no", "camera": why]
    }
}
