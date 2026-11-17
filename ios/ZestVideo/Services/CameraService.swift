import AVFoundation
import Foundation
import UIKit
import ZestCore

/// A finished recording, still in the camera's own .mov wrapper.
struct RecordedClip {
    let url: URL
    let startedAt: Date
}

/// How a recording ended.
struct RecordingEnd {
    /// The file, when anything usable was written.
    let clip: RecordedClip?
    /// What cut it short: a call, the app leaving the screen, and so on.
    /// `nil` when it ran to the limit or the tech ended it himself.
    let cut: RecordingCut?
    /// How long the camera ran, by the clock. (What the file really holds
    /// is read from the file; this is for when that cannot be.)
    let seconds: Double
    /// Set when no recording ever began: the tap started nothing, because
    /// the camera was not running and could not be started in time, or the
    /// phone would not record. It holds a few words for the audit trail
    /// about why ("stopped", "refused -11803"). `nil` for a recording that
    /// did begin, however short it was.
    var neverBegan: String? = nil
    /// What the phone's camera said about a recording that did begin, when
    /// it said anything (its error number; that it had to be started again
    /// at the tap). For the audit trail.
    var camera: String? = nil
}

/// Which recording the camera queue has been asked to start: its number,
/// or none once that recording has been given up on. Written on the main
/// thread and read on the camera queue, so it is locked.
private final class RecordingTicket: @unchecked Sendable {
    private let lock = NSLock()
    private var issued = 0
    private var live = 0

    func issue() -> Int {
        lock.lock()
        defer { lock.unlock() }
        issued += 1
        live = issued
        return issued
    }

    func giveUp() {
        lock.lock()
        defer { lock.unlock() }
        live = 0
    }

    func isLive(_ number: Int) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return live == number
    }
}

/// The camera. One capture session, made when the tech confirms a stop and
/// shut down as soon as the clip is recorded, so it costs no battery or
/// memory the rest of the day.
///
/// Capture is 720p, 30 frames a second, H.264 + AAC at about 2.5 Mbps: a
/// 15-second clip is roughly 5 MB. Recording stops by itself at the limit.
///
/// **"Ready" is never remembered.** The first build on a real phone showed
/// the record screen black with TAP TO RECORD under the button: `status`
/// had been set to `.ready` when the session started, the session had
/// stopped since, and nothing asked it again, so every tap failed. Now the
/// status is worked out from what the session itself says, by one rule
/// (`CameraRule.settle` in ZestCore), each time it is asked:
///
/// - every start, stop and look ends by asking the session whether it is
///   running and whether the phone has interrupted it (`report`), and the
///   answer is turned into the status in one place (`settle`);
/// - the session is asked when a screen asks for the camera (`prepare`,
///   also when the record screen appears and when the app comes back to
///   the front), when the phone says anything about it (interrupted, over,
///   an error, started, stopped), after every recording, and once a second
///   for as long as a screen wants the camera;
/// - a session found stopped while a screen wants it is started again with
///   no touch, three times at most, then the screen says so and offers TRY
///   AGAIN; a session found running when no screen wants it is stopped;
/// - a tap on the record button looks at the session once more on the
///   camera's own queue, starts it there if it has stopped, and records.
///   If it will not start, the tap ends as "never began", which is not a
///   clip that was cut short;
/// - a session that says it is running and records nothing all the same
///   (the file output refuses, or never answers, or still holds a recording
///   nobody is waiting for) is stopped and started afresh: what going BACK
///   and opening the stop again did on that phone, without the tech
///   having to.
///
/// **When the phone takes the camera away** (a call rings, Siri, an alarm,
/// the app leaves the screen, another app wants the camera) iOS interrupts
/// the capture session. If a clip is being recorded, iOS ends the file and
/// hands it over like any other: what was recorded is kept, and
/// `RecordingEnd.cut` says what stopped it. While the interruption lasts
/// `away` says why the camera is off, and when it ends the camera comes back
/// by itself if a screen still wants it. Nothing here waits on the tech.
final class CameraService: NSObject, ObservableObject, AVCaptureFileOutputRecordingDelegate, @unchecked Sendable {
    enum Status: Equatable {
        case idle
        case starting
        /// The session says it is running. Only `settle` sets this.
        case ready
        case recording
        /// No camera at all (the simulator, or App Review's test machine).
        case noCamera
        /// The tech said no to the camera or the microphone.
        case denied
        case failed(String)
    }

    @Published private(set) var status: Status = .idle {
        didSet {
            #if targetEnvironment(simulator)
            // UI tests only: what the camera said, for a failure report.
            if status != oldValue {
                UITestMode.note("camera \(oldValue) to \(status)")
            }
            #endif
        }
    }
    /// When the file output really began writing. The ring counts from this.
    @Published private(set) var recordingStartedAt: Date?
    @Published private(set) var usingFrontCamera = false
    /// Why the phone is keeping the camera from the app right now (a call,
    /// another app). `nil` when it is not. The record screen says so.
    @Published private(set) var away: RecordingCut?

    let session = AVCaptureSession()

    private let queue = DispatchQueue(label: "com.zestlawnpest.video.camera")
    private let output = AVCaptureMovieFileOutput()
    private let ticket = RecordingTicket()
    // These three are touched on the camera queue only.
    private var videoInput: AVCaptureDeviceInput?
    private var configured = false
    /// The latest tap got as far as asking the file output to record.
    private var askedOutput = false

    // All of these are touched on the main thread only.
    private var onFinish: ((RecordingEnd) -> Void)?
    /// A screen wants the camera (asked for by `prepare`, given up by `shutDown`).
    private var wanted = false
    /// The tech tapped the ring: this recording's end is his own doing.
    private var stopRequested = false
    /// The last thing the phone said took the camera away, and when.
    private var lastCut: (cut: RecordingCut, at: Date)?
    /// The last thing the phone said went wrong with the camera, and when.
    private var lastTrouble: (words: String, at: Date)?
    /// A start is on its way to the camera queue, or is running there.
    private var startOnItsWay = false
    /// When the session was started or stopped with nobody asking (see
    /// `CameraRule.mostTries`). Emptied each time a screen asks.
    private var triesByItself: [Date] = []
    /// The once-a-second look is running.
    private var watching = false
    /// The app is in front.
    private var inFront = true
    /// The session had to be started again at the tap, for this recording.
    private var startedAtTheTap = false
    private var observers: [NSObjectProtocol] = []

    override init() {
        super.init()
        let center = NotificationCenter.default
        observers = [
            center.addObserver(forName: AVCaptureSession.wasInterruptedNotification, object: session, queue: .main) { [weak self] note in
                self?.sessionWasInterrupted(note)
            },
            center.addObserver(forName: AVCaptureSession.interruptionEndedNotification, object: session, queue: .main) { [weak self] _ in
                self?.sessionInterruptionEnded()
            },
            center.addObserver(forName: AVCaptureSession.runtimeErrorNotification, object: session, queue: .main) { [weak self] note in
                self?.sessionHadAnError(note)
            },
            // The session's own word that it has started or stopped,
            // whoever started or stopped it.
            center.addObserver(forName: AVCaptureSession.didStartRunningNotification, object: session, queue: .main) { [weak self] _ in
                self?.look()
            },
            center.addObserver(forName: AVCaptureSession.didStopRunningNotification, object: session, queue: .main) { [weak self] _ in
                self?.look()
            },
            center.addObserver(forName: UIApplication.didBecomeActiveNotification, object: nil, queue: .main) { [weak self] _ in
                self?.appCameToTheFront()
            },
            center.addObserver(forName: UIApplication.willResignActiveNotification, object: nil, queue: .main) { [weak self] _ in
                self?.inFront = false
            },
        ]
    }

    deinit {
        observers.forEach { NotificationCenter.default.removeObserver($0) }
    }

    #if targetEnvironment(simulator)
    /// UI tests only (the simulator has no camera): a stand-in recorder.
    /// See the extension at the end of this file.
    private(set) var standIn = false
    /// The stand-in's "capture session": what it says when asked.
    private let fake = StandInSession()
    private var standInURL: URL?
    private var standInTimer: DispatchWorkItem?
    /// The interruption a test asked for has been played (once per launch).
    private var standInInterrupted = false
    /// The stop behind the screen's back a test asked for has been played
    /// (once per launch).
    private var standInStopPlayed = false
    private var standInObservers: [NSObjectProtocol] = []
    /// Screenshot mode: the status is whatever the picture needs, and
    /// nothing here changes it.
    private var pictureOnly = false
    #endif

    static let targetBitRate = 2_500_000

    // MARK: Lifecycle (call on the main thread)

    /// A screen wants the camera. Asks for permission if needed, builds the
    /// session once, and starts it. Safe to call again and again, and worth
    /// it: each call asks the session whether it really is running, and
    /// starts it again if it is not, whatever the status says.
    func prepare() {
        let alreadyWanted = wanted
        wanted = true
        // A screen is asking, so the app is in front, whatever was heard last.
        inFront = true
        #if targetEnvironment(simulator)
        if pictureOnly { return }
        #endif
        keepWatch()
        guard !busy else { return }
        // A screen asked: tries that failed earlier are forgotten.
        triesByItself.removeAll()
        #if targetEnvironment(simulator)
        if alreadyWanted { standInOpened() }
        #endif
        // The record screen must not come up offering TAP TO RECORD on a
        // remembered "ready": the session is asked here, before it is drawn.
        if status == .ready, !sessionRuns {
            status = .starting
        }
        // A new use of the camera begins with a start of its own, never
        // with a session left running from an earlier one.
        start(fresh: !alreadyWanted)
    }

    /// Stops the camera and frees the microphone. A recording in progress is
    /// finished and delivered first.
    func shutDown() {
        wanted = false
        #if targetEnvironment(simulator)
        if pictureOnly {
            if status != .recording { status = .idle }
            return
        }
        #endif
        // The screens are told at once; the session follows on its queue.
        // (`prepare` no longer trusts the status, so a stop that lands
        // after the next `prepare` cannot leave the camera off.)
        if !busy {
            show(.idle)
        }
        queue.async {
            // An interrupted session too. Left alone, the phone starts it
            // again by itself when the interruption ends, with no screen
            // wanting it: the camera and the microphone on under My Route.
            if self.sessionRuns || self.sessionIsInterrupted {
                self.stopSession()
            }
            self.report()
        }
    }

    /// Asks the session how it is, and acts on the answer (see `settle`).
    /// Costs nothing when all is well.
    func look() {
        #if targetEnvironment(simulator)
        if pictureOnly { return }
        #endif
        queue.async { self.report() }
    }

    /// A clip is being recorded, or its start is on its way to the camera.
    private var busy: Bool {
        status == .recording || onFinish != nil
    }

    private var isFailed: Bool {
        if case .failed = status { return true }
        return false
    }

    private func show(_ new: Status) {
        if status != new {
            status = new
        }
    }

    /// While a screen wants the camera the session is asked how it is once
    /// a second, so that no screen can go on saying "ready" over a camera
    /// that has stopped, whether or not the phone said anything about it.
    private func keepWatch() {
        guard wanted, !watching else { return }
        watching = true
        DispatchQueue.main.asyncAfter(deadline: .now() + CameraRule.watchSeconds) { [weak self] in
            guard let self else { return }
            self.watching = false
            guard self.wanted else { return }
            if !self.busy {
                self.look()
            }
            self.keepWatch()
        }
    }

    // MARK: Starting (main thread, then the camera queue)

    /// Puts one start on the camera queue, unless one is on its way. A
    /// session that is found running is left as it is, so this is also how
    /// the session is checked.
    /// - Parameter fresh: a new use of the camera: a session left over from
    ///   an earlier one is stopped first.
    private func start(fresh: Bool = false) {
        #if targetEnvironment(simulator)
        if pictureOnly { return }
        #endif
        guard !busy else { return }
        if status != .ready {
            show(.starting)
        }
        guard !startOnItsWay else { return }
        #if targetEnvironment(simulator)
        if standIn {
            startOnItsWay = true
            queue.async { self.startOnQueue(fresh: fresh) }
            return
        }
        #endif
        guard AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back) != nil else {
            status = .noCamera
            return
        }
        startOnItsWay = true
        Task {
            let camera = await AVCaptureDevice.requestAccess(for: .video)
            let microphone = await AVCaptureDevice.requestAccess(for: .audio)
            DispatchQueue.main.async {
                guard camera && microphone else {
                    self.startOnItsWay = false
                    self.status = .denied
                    return
                }
                guard self.wanted else {
                    // The screen left while the phone was asking.
                    self.startOnItsWay = false
                    return
                }
                self.queue.async { self.startOnQueue(fresh: fresh) }
            }
        }
    }

    /// Camera queue. Starts the session unless it is running, then says
    /// how it is.
    private func startOnQueue(fresh: Bool) {
        if fresh, sessionRuns || sessionIsInterrupted, !output.isRecording {
            stopSession()
        }
        if !sessionRuns {
            // No screen may offer the record button meanwhile.
            DispatchQueue.main.async {
                if self.status == .ready, !self.busy {
                    self.status = .starting
                }
            }
            startSession()
        }
        report(startDone: true)
    }

    /// The session has stopped while a screen wants it: it is started again
    /// without waiting to be asked. The first try goes at once, the next
    /// ones after a short pause.
    private func startByItself() {
        let again = !triesByItself.isEmpty
        triesByItself.append(Date())
        guard again else {
            start()
            return
        }
        startOnItsWay = true
        DispatchQueue.main.asyncAfter(deadline: .now() + CameraRule.secondsBetweenTries) { [weak self] in
            guard let self else { return }
            self.startOnItsWay = false
            if self.wanted {
                self.start()
            }
        }
    }

    /// Stops the session and starts it afresh: what going BACK and opening
    /// the stop again does, without the tech having to. For a session that
    /// says it is running and still would not record.
    private func startAfresh() {
        guard wanted, !busy, !startOnItsWay else {
            look()
            return
        }
        startOnItsWay = true
        show(.starting)
        queue.async { self.startOnQueue(fresh: true) }
    }

    // MARK: The session's own word

    /// Asked, never remembered. (Reading these is safe on any thread;
    /// starting and stopping happen on the camera queue only.)
    private var sessionRuns: Bool {
        #if targetEnvironment(simulator)
        if standIn { return fake.running }
        #endif
        return session.isRunning
    }

    private var sessionIsInterrupted: Bool {
        #if targetEnvironment(simulator)
        if standIn { return fake.interrupted }
        #endif
        return session.isInterrupted
    }

    /// Camera queue.
    private func startSession() {
        #if targetEnvironment(simulator)
        if standIn {
            fake.start()
            return
        }
        #endif
        guard configure() else { return }
        Sounds.allowDuringRecording()
        session.startRunning()
        Sounds.allowDuringRecording()
    }

    /// Camera queue.
    private func stopSession() {
        #if targetEnvironment(simulator)
        if standIn {
            fake.stop()
            return
        }
        #endif
        session.stopRunning()
    }

    /// Camera queue. Tells the main thread what the session says now.
    /// Every start, stop and look ends here.
    private func report(startDone: Bool = false) {
        let running = sessionRuns
        let interrupted = sessionIsInterrupted
        DispatchQueue.main.async {
            if startDone {
                self.startOnItsWay = false
            }
            self.settle(running: running, interrupted: interrupted)
        }
    }

    /// Main thread. The one place the status is worked out, from what the
    /// session has just said and what the screens want (`CameraRule`).
    private func settle(running: Bool, interrupted: Bool) {
        #if targetEnvironment(simulator)
        if pictureOnly { return }
        #endif
        // No camera, or no permission: only a screen asking again helps.
        if wanted, status == .noCamera || status == .denied { return }

        // Why the camera is away, for as long as it is, and not after. A
        // session that says it is running is running, whatever else it
        // says: the rule below looks at "running" first, and so must this.
        // (With "interrupted" still up over a running session, the button
        // was lit, the line said the camera was off, and every tap was
        // refused without a word.)
        if interrupted, !running {
            if away == nil {
                away = lastCut?.cut ?? .unknown
            }
        } else if away != nil, running || !wanted {
            away = nil
        }

        let now = Date()
        triesByItself.removeAll { now.timeIntervalSince($0) > CameraRule.triesWindow }
        let verdict = CameraRule.settle(CameraFacts(
            wanted: wanted, running: running, interrupted: interrupted, recording: busy,
            starting: startOnItsWay, failed: isFailed, triesLately: triesByItself.count, inFront: inFront
        ))
        switch verdict.standing {
        case .asIs:
            break
        case .off, .away:
            show(.idle)
        case .starting:
            show(.starting)
        case .ready:
            show(.ready)
        case .failed:
            if !isFailed {
                status = .failed(CameraRule.couldNotStart)
            }
        }
        switch verdict.step {
        case .nothing:
            break
        case .start:
            startByItself()
        case .stop:
            triesByItself.append(now)
            queue.async {
                if self.sessionRuns {
                    self.stopSession()
                }
                self.report()
            }
        }
    }

    /// Main thread. A recording has been handed over (or given up on): the
    /// status is the session's again. Looked at here and now, so the record
    /// button is right the moment the red frame comes down, and once more
    /// through the camera queue in case the session was still on its way
    /// down.
    private func afterRecording() {
        if status == .recording {
            status = .idle
        }
        settle(running: sessionRuns, interrupted: sessionIsInterrupted)
        look()
    }

    // MARK: The phone takes the camera away, and gives it back (main thread)

    /// What the phone's reason for an interruption means in the app's terms.
    private func whatTookTheCamera(_ reason: AVCaptureSession.InterruptionReason?) -> RecordingCut {
        guard let reason else { return .unknown }
        switch reason {
        case .audioDeviceInUseByAnotherClient:
            // The microphone was taken: by a call, FaceTime, Siri, an alarm
            // or a timer. The phone does not say which, and the app does
            // not ask (that would take Apple's call framework, which this
            // app must not link).
            return .callOrAlarm
        case .videoDeviceNotAvailableInBackground:
            return .appClosed
        case .videoDeviceInUseByAnotherClient, .videoDeviceNotAvailableWithMultipleForegroundApps:
            return .cameraTaken
        case .videoDeviceNotAvailableDueToSystemPressure:
            return .phoneBusy
        @unknown default:
            return .unknown
        }
    }

    private func sessionWasInterrupted(_ note: Notification) {
        let raw = (note.userInfo?[AVCaptureSessionInterruptionReasonKey] as? NSNumber)?.intValue
        if let raw {
            lastTrouble = ("interrupted \(raw)", Date())
        }
        cameraWasTaken(by: whatTookTheCamera(raw.flatMap { AVCaptureSession.InterruptionReason(rawValue: $0) }))
    }

    private func cameraWasTaken(by taken: RecordingCut) {
        lastCut = (taken, Date())
        away = taken
        // A recording in progress is ended by iOS itself; its file arrives
        // through `didFinishRecordingTo` like any other.
        if status == .ready || status == .starting {
            status = .idle
        }
        look()
    }

    private func sessionInterruptionEnded() {
        away = nil
        if wanted {
            // (While a clip is being handed over this does nothing: the
            // camera is looked at again when the hand-over is done.)
            start()
        } else {
            // The phone starts an interrupted session again by itself. If
            // no screen wants it, `settle` stops it.
            look()
        }
    }

    /// The capture session stopped with an error (the phone's media
    /// services were restarted, say). A recording in progress is handed
    /// over by the file output; the session is looked at now, and again in
    /// a moment in case it had not finished stopping, and started again if
    /// a screen wants it.
    private func sessionHadAnError(_ note: Notification) {
        if let error = note.userInfo?[AVCaptureSessionErrorKey] as? NSError {
            lastTrouble = ("error \(error.code)", Date())
        } else {
            lastTrouble = ("error", Date())
        }
        look()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            self?.look()
        }
    }

    /// Back from a call, the lock screen or another app, on whichever
    /// screen wants the camera: it is looked at, and started if it is not
    /// running.
    private func appCameToTheFront() {
        inFront = true
        if wanted {
            start()
        }
    }

    /// What the phone last said went wrong with the camera, for the audit
    /// trail: nothing else can be read off a tech's phone afterwards.
    private func lastSaid() -> String? {
        guard let said = lastTrouble else { return nil }
        let ago = Int(Date().timeIntervalSince(said.at))
        guard ago >= 0, ago < 3600 else { return nil }
        return "\(said.words) \(ago)s ago"
    }

    func flip() {
        guard status == .ready, onFinish == nil else { return }
        #if targetEnvironment(simulator)
        if standIn {
            usingFrontCamera.toggle()
            return
        }
        #endif
        queue.async {
            guard let current = self.videoInput else { return }
            let wanted: AVCaptureDevice.Position = current.device.position == .back ? .front : .back
            guard let device = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: wanted),
                  let input = try? AVCaptureDeviceInput(device: device) else { return }
            self.session.beginConfiguration()
            self.session.removeInput(current)
            if self.session.canAddInput(input) {
                self.session.addInput(input)
                self.videoInput = input
            } else {
                self.session.addInput(current)
            }
            self.session.commitConfiguration()
            self.applyOutputSettings()
            self.setFrameRate(on: self.videoInput?.device)
            let front = self.videoInput?.device.position == .front
            DispatchQueue.main.async { self.usingFrontCamera = front }
        }
    }

    // MARK: Recording (call on the main thread)

    /// Starts writing to `url`. `onFinish` is called once, on the main thread,
    /// with how the recording ended: the clip (or none, if nothing usable
    /// was saved) and what cut it short (if anything did); or that no
    /// recording ever began (`RecordingEnd.neverBegan`).
    func startRecording(to url: URL, maxSeconds: Int, onFinish: @escaping (RecordingEnd) -> Void) {
        #if targetEnvironment(simulator)
        if pictureOnly {
            // Screenshot mode has no camera, stand-in or other.
            onFinish(RecordingEnd(clip: nil, cut: nil, seconds: 0, neverBegan: "picture"))
            return
        }
        #endif
        // The button was tapped on "ready" a moment ago. If the session has
        // been found stopped since, it is being started again, and the tap
        // still stands (the camera queue below waits for that start). Only
        // when the phone has taken the camera, or there is none to use, is
        // there nothing to do: nothing was recorded, so nothing was cut
        // short, and the camera is asked for again.
        let canTry = wanted && (status == .ready || status == .starting || status == .idle)
        guard canTry, away == nil, self.onFinish == nil else {
            onFinish(RecordingEnd(clip: nil, cut: nil, seconds: 0, neverBegan: why(away == nil ? "not_ready" : "away")))
            if wanted {
                start()
            }
            return
        }
        self.onFinish = onFinish
        stopRequested = false
        startedAtTheTap = false
        let mine = ticket.issue()
        #if targetEnvironment(simulator)
        standInTapped()
        #endif
        queue.async {
            self.askedOutput = false
            // The session may have stopped behind the screen's back, with
            // the screen still offering the button (the black record screen
            // on the first real phone). It is asked here, and started here
            // if it has stopped, so that this one tap records.
            if !self.sessionRuns {
                self.startSession()
                DispatchQueue.main.async { self.startedAtTheTap = true }
            }
            // (Given up on while the session was starting: nothing to record.)
            guard self.ticket.isLive(mine) else { return }
            guard self.sessionRuns else {
                self.neverBegan(self.sessionIsInterrupted ? "interrupted" : "stopped")
                return
            }
            #if targetEnvironment(simulator)
            if self.standIn {
                DispatchQueue.main.async { self.startStandInRecording(to: url, maxSeconds: maxSeconds) }
                return
            }
            #endif
            guard !self.output.isRecording else {
                // A recording nobody is waiting for is still in the file
                // output: one given up on earlier, that the output never
                // ended. Nothing can be recorded on top of it, and left
                // there it would answer every tap from now on this way.
                // The tap is answered first, so that the old recording's
                // end, when it comes, finds nobody to hand it to. Then
                // that recording and the session are stopped, as BACK
                // would stop them, and the start that follows is a fresh
                // one.
                self.neverBegan("busy", afresh: true)
                self.output.stopRecording()
                self.stopSession()
                self.report()
                return
            }
            // The hard stop: the file output ends the clip itself at the limit.
            self.output.maxRecordedDuration = CMTime(seconds: Double(maxSeconds), preferredTimescale: 600)
            self.applyOutputSettings()
            Files.remove(url)
            Sounds.allowDuringRecording()
            self.askedOutput = true
            self.output.startRecording(to: url, recordingDelegate: self)
        }
    }

    func stopRecording() {
        stopRequested = true
        #if targetEnvironment(simulator)
        if standIn {
            if !UITestMode.silentCamera {
                finishStandInRecording(cut: nil)
            }
            return
        }
        #endif
        queue.async {
            if self.output.isRecording {
                self.output.stopRecording()
            }
        }
    }

    /// The camera never said the recording was over (see
    /// `RecordingRule.isOverdue`). End it here: hand over whatever is on
    /// disk, take the red frame down, and let the tech carry on.
    func giveUpRecording(of url: URL) {
        guard let handler = onFinish else { return }
        onFinish = nil
        ticket.giveUp()
        #if targetEnvironment(simulator)
        if standIn {
            standInTimer?.cancel()
            standInTimer = nil
            standInURL = nil
        }
        #endif
        let began = recordingStartedAt != nil
        let startedAt = recordingStartedAt ?? Date()
        recordingStartedAt = nil
        status = .idle
        queue.async {
            if self.output.isRecording {
                self.output.stopRecording()
            }
            // The file output was asked to record, on a session that says
            // it is running, and never said the clip had begun nor why
            // not: that session gave it nothing to write. It is stopped,
            // as BACK would stop it, so that the start below is a fresh
            // one. (Left running, it answered the next tap the same way.
            // A tap given up on before it reached the file output is not
            // this: its session has only just been started.)
            if !began, self.askedOutput, self.sessionRuns {
                self.askedOutput = false
                self.stopSession()
                self.report()
            }
        }
        let clip = Files.size(of: url) > 0 ? RecordedClip(url: url, startedAt: startedAt) : nil
        if !began && clip == nil {
            // It never even began: there is no clip to call cut short.
            handler(RecordingEnd(clip: nil, cut: nil, seconds: 0, neverBegan: why("silent")))
        } else {
            handler(RecordingEnd(clip: clip, cut: .unknown, seconds: Date().timeIntervalSince(startedAt), camera: "silent"))
        }
        if wanted {
            start()
        } else {
            look()
        }
    }

    /// Any thread. The tap started nothing: say so, once. The camera is
    /// looked at first (which starts it again if a screen still wants it),
    /// so that what the screen says next is already true.
    /// - Parameter afresh: the caller is stopping the session on the camera
    ///   queue: the button waits from now, and a start is put behind that
    ///   stop.
    private func neverBegan(_ words: String, afresh: Bool = false) {
        DispatchQueue.main.async {
            guard let handler = self.onFinish else { return }
            self.onFinish = nil
            let said = self.why(words)
            if afresh {
                self.startAfresh()
            } else {
                self.afterRecording()
            }
            handler(RecordingEnd(clip: nil, cut: nil, seconds: 0, neverBegan: said))
        }
    }

    /// Main thread. Why nothing was recorded, with what the phone last said
    /// about the camera if it said anything.
    private func why(_ words: String) -> String {
        guard let said = lastSaid() else { return words }
        return "\(words); \(said)"
    }

    // MARK: AVCaptureFileOutputRecordingDelegate

    func fileOutput(_ output: AVCaptureFileOutput, didStartRecordingTo fileURL: URL, from connections: [AVCaptureConnection]) {
        let now = Date()
        DispatchQueue.main.async {
            // (A recording already given up on is not brought back to life
            // by a late notice.)
            guard self.onFinish != nil else { return }
            self.recordingStartedAt = now
            self.status = .recording
        }
    }

    func fileOutput(
        _ output: AVCaptureFileOutput, didFinishRecordingTo outputFileURL: URL,
        from connections: [AVCaptureConnection], error: Error?
    ) {
        // Reaching the time limit arrives as an "error" that says the file is fine.
        var finishedCleanly = true
        let problem = error as NSError?
        if let problem {
            finishedCleanly = (problem.userInfo[AVErrorRecordingSuccessfullyFinishedKey] as? Bool) ?? false
        }
        let reachedLimit = problem?.domain == AVFoundationErrorDomain && problem?.code == AVError.Code.maximumDurationReached.rawValue
        let outOfRoom = problem?.domain == AVFoundationErrorDomain && problem?.code == AVError.Code.diskFull.rawValue
        // A call or a trip to the home screen cuts the clip short. The file is
        // written in one-second pieces, so what was recorded is still there.
        let usable = finishedCleanly || Files.size(of: outputFileURL) > 0
        let stopped = Date()
        DispatchQueue.main.async {
            // The file output says when a recording really begins
            // (`didStartRecordingTo`). If it never did, nothing was recorded.
            let began = self.recordingStartedAt != nil
            let startedAt = self.recordingStartedAt ?? stopped
            // The red frame comes down now, whatever follows.
            self.recordingStartedAt = nil
            guard let handler = self.onFinish else { return }   // already given up on
            self.onFinish = nil

            if !began && !usable && !outOfRoom {
                // The phone would not record at all. Not a clip cut short:
                // nothing began. (A full phone is not this: it has a line
                // of its own, below.) If a call took the camera at that
                // very moment the phone says so in a notice of its own,
                // which may arrive a moment after this does: it is given
                // that moment, as below.
                let code = problem.map { "refused \($0.code)" } ?? "refused"
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                    let said = self.why(code)
                    if self.sessionRuns && !self.sessionIsInterrupted {
                        // A session that says it is running and would not
                        // record. It is stopped and started afresh, which
                        // is what going BACK and opening the stop again did
                        // on the first real phone.
                        self.startAfresh()
                    } else {
                        // Interrupted, or stopped: the usual look says
                        // CAMERA OFF and waits, or starts it again.
                        self.afterRecording()
                    }
                    handler(RecordingEnd(clip: nil, cut: nil, seconds: 0, neverBegan: said))
                }
                return
            }

            // The status is the session's again: ready if it is still
            // running, started again if it has stopped and a screen wants it.
            self.afterRecording()

            let clip = usable ? RecordedClip(url: outputFileURL, startedAt: startedAt) : nil
            let seconds = max(0, stopped.timeIntervalSince(startedAt))
            var words: [String] = []
            if self.startedAtTheTap {
                words.append("started at the tap")
            }

            // The limit, or the tech's own tap: nothing cut it short.
            if reachedLimit || (problem == nil && self.stopRequested) {
                handler(RecordingEnd(clip: clip, cut: nil, seconds: seconds, camera: words.first))
                return
            }
            if let problem {
                words.append("error \(problem.code)")
            }
            let said = words.joined(separator: "; ")
            if outOfRoom {
                handler(RecordingEnd(clip: clip, cut: .phoneFull, seconds: seconds, camera: said))
                return
            }
            // Something else ended it. The phone says what in a notice of
            // its own, which may arrive a moment after this does: give it
            // that moment, then go by what is known.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                var taken = RecordingCut.unknown
                if let last = self.lastCut, abs(last.at.timeIntervalSince(stopped)) < 5 {
                    taken = last.cut
                }
                handler(RecordingEnd(clip: clip, cut: taken, seconds: seconds, camera: said))
            }
        }
    }

    // MARK: Session setup (camera queue)

    /// Builds the session, the first time. False when there is nothing to
    /// start; the screens have been told why.
    private func configure() -> Bool {
        if configured { return true }
        session.beginConfiguration()
        session.sessionPreset = session.canSetSessionPreset(.hd1280x720) ? .hd1280x720 : .high

        guard let camera = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back),
              let cameraInput = try? AVCaptureDeviceInput(device: camera),
              session.canAddInput(cameraInput) else {
            session.commitConfiguration()
            publish(.noCamera)
            return false
        }
        session.addInput(cameraInput)
        videoInput = cameraInput

        if let microphone = AVCaptureDevice.default(for: .audio),
           let microphoneInput = try? AVCaptureDeviceInput(device: microphone),
           session.canAddInput(microphoneInput) {
            session.addInput(microphoneInput)
        }

        guard session.canAddOutput(output) else {
            session.commitConfiguration()
            publish(.failed(CameraRule.couldNotStart))
            return false
        }
        session.addOutput(output)
        // Write the file in one-second pieces so a crash or a phone call
        // mid-clip still leaves a playable recording.
        output.movieFragmentInterval = CMTime(seconds: 1, preferredTimescale: 600)
        session.commitConfiguration()

        applyOutputSettings()
        setFrameRate(on: camera)
        configured = true
        return true
    }

    /// Upright video, H.264, and the small-file bitrate where the phone allows it.
    private func applyOutputSettings() {
        guard let connection = output.connection(with: .video) else { return }
        if connection.isVideoRotationAngleSupported(90) {
            connection.videoRotationAngle = 90
        }
        if connection.isVideoStabilizationSupported {
            connection.preferredVideoStabilizationMode = .standard
        }
        guard output.availableVideoCodecTypes.contains(.h264) else { return }
        var settings: [String: Any] = [AVVideoCodecKey: AVVideoCodecType.h264]
        if output.supportedOutputSettingsKeys(for: connection).contains(AVVideoCompressionPropertiesKey) {
            settings[AVVideoCompressionPropertiesKey] = [AVVideoAverageBitRateKey: CameraService.targetBitRate]
        }
        output.setOutputSettings(settings, for: connection)
    }

    /// Thirty frames a second, when the camera's format offers it.
    private func setFrameRate(on device: AVCaptureDevice?) {
        guard let device else { return }
        let supportsThirty = device.activeFormat.videoSupportedFrameRateRanges.contains {
            $0.minFrameRate <= 30 && $0.maxFrameRate >= 30
        }
        guard supportsThirty, (try? device.lockForConfiguration()) != nil else { return }
        device.activeVideoMinFrameDuration = CMTime(value: 1, timescale: 30)
        device.activeVideoMaxFrameDuration = CMTime(value: 1, timescale: 30)
        device.unlockForConfiguration()
    }

    private func publish(_ status: Status) {
        DispatchQueue.main.async { self.status = status }
    }
}

#if targetEnvironment(simulator)
/// The stand-in's "capture session": the two things the real one says about
/// itself, and what a test can do to it. It is asked on the camera queue
/// and told on the main thread, as the real one is, so it is locked.
final class StandInSession: @unchecked Sendable {
    private let lock = NSLock()
    private var isRunning = false
    /// What has the camera for now (a "call"), as the phone would say.
    private var taken: RecordingCut?
    /// A test asked for starts to fail until this moment.
    private var deadUntil: Date?
    /// A test asked for a start to take this long, as a real one does.
    private var startSeconds: TimeInterval = 0

    var running: Bool {
        lock.lock()
        defer { lock.unlock() }
        return isRunning
    }

    var interrupted: Bool {
        takenBy != nil
    }

    var takenBy: RecordingCut? {
        lock.lock()
        defer { lock.unlock() }
        return taken
    }

    /// Camera queue. Running afterwards, unless the phone has the camera
    /// or a test said this start is to fail.
    func start() {
        lock.lock()
        let wait = startSeconds
        lock.unlock()
        if wait > 0 {
            Thread.sleep(forTimeInterval: wait)
        }
        lock.lock()
        defer { lock.unlock() }
        if let deadUntil, Date() < deadUntil { return }
        if taken == nil {
            isRunning = true
        }
    }

    func stop() {
        lock.lock()
        defer { lock.unlock() }
        isRunning = false
    }

    /// The phone takes the camera (a call, the app off the screen).
    func take(_ cut: RecordingCut) {
        lock.lock()
        defer { lock.unlock() }
        taken = cut
        isRunning = false
    }

    func giveBack() {
        lock.lock()
        defer { lock.unlock() }
        taken = nil
    }

    /// Stops with no word to anyone: no notice, no change of status. What
    /// a real session did on the first real phone.
    /// - Parameters:
    ///   - deadFor: starts fail for this long afterwards.
    ///   - startSeconds: each later start takes this long.
    func stopUnseen(deadFor: TimeInterval, startSeconds: TimeInterval) {
        lock.lock()
        defer { lock.unlock() }
        isRunning = false
        deadUntil = deadFor > 0 ? Date().addingTimeInterval(deadFor) : nil
        self.startSeconds = startSeconds
    }
}

extension CameraService {
    /// UI tests only. From now on this object records without a camera. Its
    /// "session" is a stand-in too (`StandInSession`): it starts at once,
    /// and everything that decides what the screens show (`prepare`,
    /// `settle`, the once-a-second look, the look at the tap) is the same
    /// code a phone runs. On start it writes a real, playable video file to
    /// the very path the camera would have used, straight away; and it ends
    /// by itself at the time limit, or when stopped, through the same
    /// `onFinish` hand-over as the real recorder. Everything downstream (the
    /// re-wrap to .mp4, the draft, the queue, the upload) is the real code.
    func useStandInRecorder() {
        standIn = true
    }

    /// Main thread. The clip is on disk before this returns, so a kill at
    /// any moment after "recording started" finds a file to recover.
    fileprivate func startStandInRecording(to url: URL, maxSeconds: Int) {
        // (Given up on while this was on its way from the camera queue.)
        guard onFinish != nil else { return }
        Files.remove(url)
        StandInClip.write(to: url)
        standInURL = url
        recordingStartedAt = Date()
        status = .recording
        watchForLeavingTheScreen()

        // (A test can ask for a camera that never says it has finished.)
        if !UITestMode.silentCamera {
            let timer = DispatchWorkItem { [weak self] in
                self?.finishStandInRecording(cut: nil)
            }
            standInTimer = timer
            DispatchQueue.main.asyncAfter(deadline: .now() + Double(maxSeconds), execute: timer)
        }

        // A test asked for this recording to be interrupted, as a call
        // would: the clip ends there, the camera is away for a while, and
        // then it comes back by itself. Once per launch, so that RECORD
        // AGAIN records.
        if let plan = UITestMode.interruption, !standInInterrupted {
            standInInterrupted = true
            DispatchQueue.main.asyncAfter(deadline: .now() + plan.at) { [weak self] in
                self?.interruptStandIn(cut: plan.cut, for: plan.lasting)
            }
        }
    }

    /// Main thread. The time limit was reached or the ring was tapped
    /// (`cut` nil), or the phone took the camera (`cut` says how).
    fileprivate func finishStandInRecording(cut: RecordingCut?) {
        guard status == .recording, let url = standInURL else { return }
        standInTimer?.cancel()
        standInTimer = nil
        standInURL = nil
        let startedAt = recordingStartedAt ?? Date()
        recordingStartedAt = nil
        let handler = onFinish
        onFinish = nil
        // As after a real recording: the status is the session's again.
        afterRecording()
        let clip = Files.size(of: url) > 0 ? RecordedClip(url: url, startedAt: startedAt) : nil
        handler?(RecordingEnd(
            clip: clip, cut: cut, seconds: Date().timeIntervalSince(startedAt),
            camera: startedAtTheTap ? "started at the tap" : nil
        ))
    }

    /// Main thread. What the real camera does when the phone interrupts it:
    /// the clip so far is handed over, the camera is away, and it comes
    /// back by itself afterwards if a screen still wants it.
    private func interruptStandIn(cut: RecordingCut, for seconds: TimeInterval?) {
        fake.take(cut)
        cameraWasTaken(by: cut)
        if status == .recording {
            finishStandInRecording(cut: cut)
        }
        guard let seconds else { return }   // until the app is back on the screen
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { [weak self] in
            self?.endStandInInterruption()
        }
    }

    private func endStandInInterruption() {
        fake.giveBack()
        sessionInterruptionEnded()
    }

    /// The real camera is taken from an app the moment it leaves the
    /// screen, and given back when it returns. The stand-in does the same,
    /// so the tests can press the home button in the middle of a clip.
    private func watchForLeavingTheScreen() {
        guard standInObservers.isEmpty else { return }
        let center = NotificationCenter.default
        standInObservers = [
            center.addObserver(forName: UIApplication.didEnterBackgroundNotification, object: nil, queue: .main) { [weak self] _ in
                self?.interruptStandIn(cut: .appClosed, for: nil)
            },
            center.addObserver(forName: UIApplication.willEnterForegroundNotification, object: nil, queue: .main) { [weak self] _ in
                guard let self, self.fake.takenBy == .appClosed else { return }
                self.endStandInInterruption()
            },
        ]
    }

    // MARK: A camera that stops behind the screen's back

    /// Main thread, from `prepare` when the camera was already wanted: the
    /// way into the record screen. A test asked for the stand-in session
    /// to stop here with no word to anyone (`UITestMode.cameraStops`), or
    /// some seconds from here. Once per launch.
    fileprivate func standInOpened() {
        guard standIn, !standInStopPlayed, let plan = UITestMode.cameraStops else { return }
        switch plan {
        case .onOpen:
            stopStandInUnseen()
        case .after(let seconds):
            standInStopPlayed = true
            DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { [weak self] in
                self?.stopStandInUnseen()
            }
        case .onTap:
            break
        }
    }

    /// Main thread, as a recording is asked for: the same, at the tap.
    fileprivate func standInTapped() {
        guard standIn, !standInStopPlayed, UITestMode.cameraStops == .onTap else { return }
        stopStandInUnseen()
    }

    private func stopStandInUnseen() {
        standInStopPlayed = true
        fake.stopUnseen(deadFor: UITestMode.cameraDeadSeconds, startSeconds: UITestMode.cameraRestartSeconds)
        UITestMode.note("camera: the stand-in session stopped behind the screen's back")
    }

    /// Screenshot mode only: the simulator has no camera, so the record
    /// screens are shown by setting the state they would have on a phone.
    /// Nothing changes it afterwards.
    func setScreenshotState(status: Status, recordingStartedAt: Date?) {
        pictureOnly = true
        self.status = status
        self.recordingStartedAt = recordingStartedAt
    }
}
#endif
