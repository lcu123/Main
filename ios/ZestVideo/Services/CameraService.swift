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
}

/// The camera. One capture session, made when the tech confirms a stop and
/// shut down as soon as the clip is recorded, so it costs no battery or
/// memory the rest of the day.
///
/// Capture is 720p, 30 frames a second, H.264 + AAC at about 2.5 Mbps: a
/// 15-second clip is roughly 5 MB. Recording stops by itself at the limit.
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
        case ready
        case recording
        /// No camera at all (the simulator, or App Review's test machine).
        case noCamera
        /// The tech said no to the camera or the microphone.
        case denied
        case failed(String)
    }

    @Published private(set) var status: Status = .idle
    /// When the file output really began writing. The ring counts from this.
    @Published private(set) var recordingStartedAt: Date?
    @Published private(set) var usingFrontCamera = false
    /// Why the phone is keeping the camera from the app right now (a call,
    /// another app). `nil` when it is not. The record screen says so.
    @Published private(set) var away: RecordingCut?

    let session = AVCaptureSession()

    private let queue = DispatchQueue(label: "com.zestlawnpest.video.camera")
    private let output = AVCaptureMovieFileOutput()
    private var videoInput: AVCaptureDeviceInput?
    private var configured = false
    private var onFinish: ((RecordingEnd) -> Void)?

    // All of these are touched on the main thread only.
    /// A screen wants the camera (asked for by `prepare`, given up by `shutDown`).
    private var wanted = false
    /// The tech tapped the ring: this recording's end is his own doing.
    private var stopRequested = false
    /// The last thing the phone said took the camera away, and when.
    private var lastCut: (cut: RecordingCut, at: Date)?
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
            center.addObserver(forName: AVCaptureSession.runtimeErrorNotification, object: session, queue: .main) { [weak self] _ in
                self?.sessionHadAnError()
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
    private var standInURL: URL?
    private var standInTimer: DispatchWorkItem?
    /// The interruption a test asked for has been played (once per launch).
    private var standInInterrupted = false
    private var standInObservers: [NSObjectProtocol] = []
    #endif

    static let targetBitRate = 2_500_000

    // MARK: Lifecycle (call on the main thread)

    /// Asks for permission if needed, builds the session once, and starts it.
    /// Safe to call again and again.
    func prepare() {
        wanted = true
        switch status {
        case .starting, .ready, .recording: return
        default: break
        }
        #if targetEnvironment(simulator)
        if standIn {
            // (While the stand-in is "interrupted" the camera stays away.)
            if away == nil { status = .ready }
            return
        }
        #endif
        guard AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back) != nil else {
            status = .noCamera
            return
        }
        status = .starting
        Task {
            let camera = await AVCaptureDevice.requestAccess(for: .video)
            let microphone = await AVCaptureDevice.requestAccess(for: .audio)
            guard camera && microphone else {
                self.publish(.denied)
                return
            }
            self.queue.async { self.configureAndStart() }
        }
    }

    /// Stops the camera and frees the microphone. A recording in progress is
    /// finished and delivered first.
    func shutDown() {
        wanted = false
        #if targetEnvironment(simulator)
        if standIn {
            if status != .recording { status = .idle }
            return
        }
        #endif
        queue.async {
            if self.session.isRunning {
                self.session.stopRunning()
            }
            self.publish(.idle)
        }
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
        let taken = whatTookTheCamera(raw.flatMap { AVCaptureSession.InterruptionReason(rawValue: $0) })
        lastCut = (taken, Date())
        away = taken
        // A recording in progress is ended by iOS itself; its file arrives
        // through `didFinishRecordingTo` like any other.
        if status == .ready || status == .starting {
            status = .idle
        }
    }

    private func sessionInterruptionEnded() {
        away = nil
        bringBackIfWanted()
    }

    /// The capture session stopped with an error (the phone's media
    /// services were restarted, say). A recording in progress has been
    /// handed over; the session is started again if a screen wants it.
    private func sessionHadAnError() {
        if status == .ready || status == .starting {
            status = .idle
        }
        bringBackIfWanted()
    }

    /// Starts the session again after the phone has given the camera back.
    private func bringBackIfWanted() {
        guard wanted, status != .recording else { return }
        #if targetEnvironment(simulator)
        if standIn {
            status = .ready
            return
        }
        #endif
        guard configured else {
            // Never got as far as a session: start from the top.
            if status == .idle || isFailed { prepare() }
            return
        }
        queue.async {
            if !self.session.isRunning, !self.session.isInterrupted {
                self.session.startRunning()
            }
            let running = self.session.isRunning
            DispatchQueue.main.async {
                guard self.wanted, self.status != .recording else { return }
                if running {
                    self.status = .ready
                } else if !self.session.isInterrupted {
                    self.status = .failed("The camera could not start.")
                }
            }
        }
    }

    private var isFailed: Bool {
        if case .failed = status { return true }
        return false
    }

    func flip() {
        guard status == .ready else { return }
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
    /// was saved) and what cut it short (if anything did).
    func startRecording(to url: URL, maxSeconds: Int, onFinish: @escaping (RecordingEnd) -> Void) {
        guard status == .ready else {
            // The phone took the camera in the moment before the tap landed.
            onFinish(RecordingEnd(clip: nil, cut: away ?? .unknown, seconds: 0))
            return
        }
        self.onFinish = onFinish
        stopRequested = false
        #if targetEnvironment(simulator)
        if standIn {
            startStandInRecording(to: url, maxSeconds: maxSeconds)
            return
        }
        #endif
        queue.async {
            guard self.session.isRunning, !self.output.isRecording else {
                self.deliver(RecordingEnd(clip: nil, cut: .unknown, seconds: 0))
                return
            }
            // The hard stop: the file output ends the clip itself at the limit.
            self.output.maxRecordedDuration = CMTime(seconds: Double(maxSeconds), preferredTimescale: 600)
            self.applyOutputSettings()
            Files.remove(url)
            Sounds.allowDuringRecording()
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
        #if targetEnvironment(simulator)
        if standIn {
            standInTimer?.cancel()
            standInTimer = nil
            standInURL = nil
        }
        #endif
        let startedAt = recordingStartedAt ?? Date()
        recordingStartedAt = nil
        status = .idle
        queue.async {
            if self.output.isRecording {
                self.output.stopRecording()
            }
        }
        let clip = Files.size(of: url) > 0 ? RecordedClip(url: url, startedAt: startedAt) : nil
        handler(RecordingEnd(clip: clip, cut: .unknown, seconds: Date().timeIntervalSince(startedAt)))
        bringBackIfWanted()
    }

    // MARK: AVCaptureFileOutputRecordingDelegate

    func fileOutput(_ output: AVCaptureFileOutput, didStartRecordingTo fileURL: URL, from connections: [AVCaptureConnection]) {
        let now = Date()
        DispatchQueue.main.async {
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
            let startedAt = self.recordingStartedAt ?? stopped
            // The red frame comes down now, whatever follows.
            self.recordingStartedAt = nil
            self.status = self.session.isRunning ? .ready : .idle
            guard let handler = self.onFinish else { return }   // already given up on
            self.onFinish = nil
            let clip = usable ? RecordedClip(url: outputFileURL, startedAt: startedAt) : nil
            let seconds = max(0, stopped.timeIntervalSince(startedAt))

            // The limit, or the tech's own tap: nothing cut it short.
            if reachedLimit || (problem == nil && self.stopRequested) {
                handler(RecordingEnd(clip: clip, cut: nil, seconds: seconds))
                return
            }
            if outOfRoom {
                handler(RecordingEnd(clip: clip, cut: .phoneFull, seconds: seconds))
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
                handler(RecordingEnd(clip: clip, cut: taken, seconds: seconds))
            }
        }
    }

    // MARK: Session setup (camera queue)

    private func configureAndStart() {
        if !configured {
            session.beginConfiguration()
            session.sessionPreset = session.canSetSessionPreset(.hd1280x720) ? .hd1280x720 : .high

            guard let camera = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back),
                  let cameraInput = try? AVCaptureDeviceInput(device: camera),
                  session.canAddInput(cameraInput) else {
                session.commitConfiguration()
                publish(.noCamera)
                return
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
                publish(.failed("The camera could not start."))
                return
            }
            session.addOutput(output)
            // Write the file in one-second pieces so a crash or a phone call
            // mid-clip still leaves a playable recording.
            output.movieFragmentInterval = CMTime(seconds: 1, preferredTimescale: 600)
            session.commitConfiguration()

            applyOutputSettings()
            setFrameRate(on: camera)
            configured = true
        }

        Sounds.allowDuringRecording()
        if !session.isRunning {
            session.startRunning()
        }
        Sounds.allowDuringRecording()
        if session.isRunning {
            publish(.ready)
        } else if session.isInterrupted {
            // A call is on, or another app has the camera: it is not a
            // fault. The screen says the camera is off for now, and
            // `sessionInterruptionEnded` brings it back.
            DispatchQueue.main.async {
                self.status = .idle
                if self.away == nil {
                    self.away = self.lastCut?.cut ?? .unknown
                }
            }
        } else {
            publish(.failed("The camera could not start."))
        }
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

    private func deliver(_ end: RecordingEnd) {
        DispatchQueue.main.async {
            let handler = self.onFinish
            self.onFinish = nil
            handler?(end)
        }
    }
}

#if targetEnvironment(simulator)
extension CameraService {
    /// UI tests only. From now on this object records without a camera: it is
    /// "ready" at once; on start it writes a real, playable video file to the
    /// very path the camera would have used, straight away; and it ends by
    /// itself at the time limit, or when stopped, through the same
    /// `onFinish` hand-over as the real recorder. Everything downstream (the
    /// re-wrap to .mp4, the draft, the queue, the upload) is the real code.
    func useStandInRecorder() {
        standIn = true
    }

    /// Main thread. The clip is on disk before this returns, so a kill at
    /// any moment after "recording started" finds a file to recover.
    fileprivate func startStandInRecording(to url: URL, maxSeconds: Int) {
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
        status = away == nil ? .ready : .idle
        let handler = onFinish
        onFinish = nil
        let clip = Files.size(of: url) > 0 ? RecordedClip(url: url, startedAt: startedAt) : nil
        handler?(RecordingEnd(clip: clip, cut: cut, seconds: Date().timeIntervalSince(startedAt)))
    }

    /// Main thread. What the real camera does when the phone interrupts it:
    /// the clip so far is handed over, the camera is away, and it comes
    /// back by itself afterwards if a screen still wants it.
    private func interruptStandIn(cut: RecordingCut, for seconds: TimeInterval?) {
        lastCut = (cut, Date())
        away = cut
        if status == .recording {
            finishStandInRecording(cut: cut)
        } else if status == .ready {
            status = .idle
        }
        guard let seconds else { return }   // until the app is back on the screen
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { [weak self] in
            self?.sessionInterruptionEnded()
        }
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
                guard let self, self.away == .appClosed else { return }
                self.sessionInterruptionEnded()
            },
        ]
    }

    /// Screenshot mode only: the simulator has no camera, so the record
    /// screens are shown by setting the state they would have on a phone.
    func setScreenshotState(status: Status, recordingStartedAt: Date?) {
        self.status = status
        self.recordingStartedAt = recordingStartedAt
    }
}
#endif
