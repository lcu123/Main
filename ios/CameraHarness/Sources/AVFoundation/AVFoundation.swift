import Foundation

// A stand-in for the parts of AVFoundation that CameraService.swift uses, so
// that the file compiles on Linux unchanged, with a capture session and a
// movie file output that a script can steer. See ../../run.sh.
//
// It is not AVFoundation and proves nothing about it. It lets the service's
// own logic be run against a session that behaves in a given way: stops
// with no word, is interrupted, fails, refuses to record.
//
// If CameraService.swift starts using a name that is not here, this stops
// compiling and the CI job "camera" says so: add the name here.

// MARK: What Foundation gives on a phone and not on Linux

@propertyWrapper
public struct Published<Value> {
    public var wrappedValue: Value

    public init(wrappedValue: Value) {
        self.wrappedValue = wrappedValue
    }
}

public protocol ObservableObject: AnyObject {}

// MARK: Small types

public struct CMTime {
    public var seconds: Double

    public init(seconds: Double, preferredTimescale: Int32) {
        self.seconds = seconds
    }

    public init(value: Int64, timescale: Int32) {
        self.seconds = Double(value) / Double(timescale)
    }
}

public struct AVMediaType: Equatable {
    let name: String
    public static let video = AVMediaType(name: "video")
    public static let audio = AVMediaType(name: "audio")
}

public struct AVVideoCodecType: Equatable {
    let name: String
    public static let h264 = AVVideoCodecType(name: "h264")
}

public let AVCaptureSessionInterruptionReasonKey = "AVCaptureSessionInterruptionReasonKey"
public let AVCaptureSessionErrorKey = "AVCaptureSessionErrorKey"
public let AVVideoCodecKey = "AVVideoCodecKey"
public let AVVideoCompressionPropertiesKey = "AVVideoCompressionPropertiesKey"
public let AVVideoAverageBitRateKey = "AVVideoAverageBitRateKey"
public let AVErrorRecordingSuccessfullyFinishedKey = "AVErrorRecordingSuccessfullyFinishedKey"
public let AVFoundationErrorDomain = "AVFoundationErrorDomain"

public struct AVError {
    public enum Code: Int {
        case unknown = -11800
        case recordingFailed = -11803
        case diskFull = -11807
        case maximumDurationReached = -11810
        case sessionWasInterrupted = -11818
        case mediaServicesWereReset = -11819
    }
}

public class AVCaptureInput {}
public class AVCaptureOutput {}

// MARK: The camera and the microphone

public final class AVCaptureDevice {
    public enum Position {
        case back
        case front
    }

    public struct DeviceType {
        public static let builtInWideAngleCamera = DeviceType()
    }

    public struct FrameRateRange {
        public var minFrameRate: Double = 1
        public var maxFrameRate: Double = 60
    }

    public struct Format {
        public var videoSupportedFrameRateRanges = [FrameRateRange()]
    }

    /// Steered by a script: the phone has a camera; the tech said yes.
    public static var hasCamera = true
    public static var allowed = true

    public let position: Position
    public var activeFormat = Format()
    public var activeVideoMinFrameDuration = CMTime(value: 1, timescale: 30)
    public var activeVideoMaxFrameDuration = CMTime(value: 1, timescale: 30)

    init(position: Position) {
        self.position = position
    }

    public class func `default`(_ type: DeviceType, for media: AVMediaType?, position: Position) -> AVCaptureDevice? {
        hasCamera ? AVCaptureDevice(position: position) : nil
    }

    public class func `default`(for media: AVMediaType) -> AVCaptureDevice? {
        AVCaptureDevice(position: .back)
    }

    public class func requestAccess(for media: AVMediaType) async -> Bool {
        allowed
    }

    public func lockForConfiguration() throws {}
    public func unlockForConfiguration() {}
}

public final class AVCaptureDeviceInput: AVCaptureInput {
    public let device: AVCaptureDevice

    public init(device: AVCaptureDevice) throws {
        self.device = device
    }
}

public final class AVCaptureConnection {
    public enum StabilizationMode {
        case standard
    }

    public var videoRotationAngle: Double = 0
    public var isVideoStabilizationSupported = true
    public var preferredVideoStabilizationMode = StabilizationMode.standard

    public func isVideoRotationAngleSupported(_ angle: Double) -> Bool {
        true
    }
}

// MARK: The movie file output

public protocol AVCaptureFileOutputRecordingDelegate: AnyObject {
    func fileOutput(_ output: AVCaptureFileOutput, didStartRecordingTo fileURL: URL, from connections: [AVCaptureConnection])
    func fileOutput(
        _ output: AVCaptureFileOutput, didFinishRecordingTo outputFileURL: URL,
        from connections: [AVCaptureConnection], error: Error?
    )
}

public class AVCaptureFileOutput: AVCaptureOutput {
    public var maxRecordedDuration = CMTime(seconds: 15, preferredTimescale: 600)
}

public final class AVCaptureMovieFileOutput: AVCaptureFileOutput {
    /// What the next `startRecording` does. Steered by a script.
    public enum Behaviour {
        /// Says it started, writes a file, and ends at the limit.
        case records
        /// Ends at once with this error and no file, having never started.
        case refuses(Int)
        /// Says nothing at all, ever, and writes nothing.
        case silent
        /// Says it started, writes a file, and then never says another
        /// word: not at the limit, and not when it is asked to stop.
        case neverEnds
    }

    public static var behaviour = Behaviour.records
    /// The output most recently made (the service makes one).
    public static var last: AVCaptureMovieFileOutput?

    private let lock = NSLock()
    private var recordingNow = false
    /// The recording in progress is one this output will never report on
    /// (`silent`, `neverEnds`). Only its session stopping ends it.
    private var unanswered = false
    private var number = 0
    private var url: URL?
    private weak var delegate: AVCaptureFileOutputRecordingDelegate?
    private let callbacks = DispatchQueue(label: "stand-in.movie-file-output")

    public var movieFragmentInterval = CMTime(seconds: 10, preferredTimescale: 600)
    public var availableVideoCodecTypes: [AVVideoCodecType] = [.h264]

    public override init() {
        super.init()
        AVCaptureMovieFileOutput.last = self
    }

    public var isRecording: Bool {
        lock.lock()
        defer { lock.unlock() }
        return recordingNow
    }

    public func connection(with media: AVMediaType) -> AVCaptureConnection? {
        AVCaptureConnection()
    }

    public func supportedOutputSettingsKeys(for connection: AVCaptureConnection) -> [String] {
        [AVVideoCompressionPropertiesKey]
    }

    public func setOutputSettings(_ settings: [String: Any]?, for connection: AVCaptureConnection) {}

    public func startRecording(to url: URL, recordingDelegate: AVCaptureFileOutputRecordingDelegate) {
        switch AVCaptureMovieFileOutput.behaviour {
        case .refuses(let code):
            callbacks.asyncAfter(deadline: .now() + 0.05) {
                recordingDelegate.fileOutput(
                    self, didFinishRecordingTo: url, from: [],
                    error: NSError(domain: AVFoundationErrorDomain, code: code)
                )
            }
        case .silent:
            lock.lock()
            recordingNow = true
            unanswered = true
            lock.unlock()
        case .neverEnds:
            lock.lock()
            recordingNow = true
            unanswered = true
            lock.unlock()
            try? Data(repeating: 7, count: 2000).write(to: url)
            callbacks.asyncAfter(deadline: .now() + 0.05) {
                recordingDelegate.fileOutput(self, didStartRecordingTo: url, from: [])
            }
        case .records:
            lock.lock()
            recordingNow = true
            unanswered = false
            number += 1
            let mine = number
            self.url = url
            delegate = recordingDelegate
            lock.unlock()
            try? Data(repeating: 7, count: 2000).write(to: url)
            callbacks.asyncAfter(deadline: .now() + 0.05) {
                recordingDelegate.fileOutput(self, didStartRecordingTo: url, from: [])
            }
            // The limit arrives as an "error" that says the file is fine.
            let limit = NSError(
                domain: AVFoundationErrorDomain, code: AVError.Code.maximumDurationReached.rawValue,
                userInfo: [AVErrorRecordingSuccessfullyFinishedKey: true]
            )
            callbacks.asyncAfter(deadline: .now() + 0.05 + maxRecordedDuration.seconds) {
                self.finish(mine, error: limit)
            }
        }
    }

    public func stopRecording() {
        lock.lock()
        let mine = number
        lock.unlock()
        callbacks.async { self.finish(mine, error: nil) }
    }

    /// The phone ends the clip that is being recorded, with this error.
    public func cut(code: Int) {
        lock.lock()
        let mine = number
        lock.unlock()
        callbacks.async { self.finish(mine, error: NSError(domain: AVFoundationErrorDomain, code: code)) }
    }

    private func finish(_ mine: Int, error: Error?) {
        lock.lock()
        guard recordingNow, !unanswered, mine == number, let url, let delegate else {
            lock.unlock()
            return
        }
        recordingNow = false
        lock.unlock()
        delegate.fileOutput(self, didFinishRecordingTo: url, from: [], error: error)
    }

    /// The session this output belongs to has stopped. A recording it
    /// would never have reported on is over, without a word. (One that
    /// `records` is ended by the script, with `cut`, as before.)
    func sessionStopped() {
        lock.lock()
        if unanswered {
            recordingNow = false
            unanswered = false
        }
        lock.unlock()
    }
}

// MARK: The capture session

public final class AVCaptureSession: NSObject {
    public static let wasInterruptedNotification = Notification.Name("AVCaptureSessionWasInterruptedNotification")
    public static let interruptionEndedNotification = Notification.Name("AVCaptureSessionInterruptionEndedNotification")
    public static let runtimeErrorNotification = Notification.Name("AVCaptureSessionRuntimeErrorNotification")
    public static let didStartRunningNotification = Notification.Name("AVCaptureSessionDidStartRunningNotification")
    public static let didStopRunningNotification = Notification.Name("AVCaptureSessionDidStopRunningNotification")

    public enum InterruptionReason: Int {
        case videoDeviceNotAvailableInBackground = 1
        case audioDeviceInUseByAnotherClient = 2
        case videoDeviceInUseByAnotherClient = 3
        case videoDeviceNotAvailableWithMultipleForegroundApps = 4
        case videoDeviceNotAvailableDueToSystemPressure = 5
    }

    public struct Preset: Equatable {
        let name: String
        public static let hd1280x720 = Preset(name: "hd1280x720")
        public static let high = Preset(name: "high")
    }

    private let lock = NSLock()
    private var running = false
    private var interrupted = false
    private var startCount = 0
    private var stopCount = 0
    private var movieOutputs: [AVCaptureMovieFileOutput] = []

    public var sessionPreset = Preset.high

    // What a script steers.

    /// A start works unless this says no.
    public var startWorks: () -> Bool = { true }
    /// A start leaves the session interrupted instead (a call is still on).
    public var startIsInterrupted: () -> Bool = { false }
    /// How long a start and a stop take. (Real ones block for a while too.)
    public var startSeconds: TimeInterval = 0.05
    public var stopSeconds: TimeInterval = 0.05
    /// Called on the starting thread right after a start has worked.
    public var afterStart: (() -> Void)?
    /// The session does not say that it started or stopped.
    public var saysNothingOfStartAndStop = false
    /// Once "interrupted" is up it stays up: a stop and a start do not
    /// take it down. (Whether a phone's does is not known.)
    public var interruptedStaysUp = false

    /// How many times the service asked it to start, and to stop.
    public var starts: Int {
        lock.lock()
        defer { lock.unlock() }
        return startCount
    }

    public var stops: Int {
        lock.lock()
        defer { lock.unlock() }
        return stopCount
    }

    public override init() {
        super.init()
    }

    public var isRunning: Bool {
        lock.lock()
        defer { lock.unlock() }
        return running
    }

    public var isInterrupted: Bool {
        lock.lock()
        defer { lock.unlock() }
        return interrupted
    }

    public func beginConfiguration() {}
    public func commitConfiguration() {}
    public func canSetSessionPreset(_ preset: Preset) -> Bool { true }
    public func canAddInput(_ input: AVCaptureInput) -> Bool { true }
    public func addInput(_ input: AVCaptureInput) {}
    public func removeInput(_ input: AVCaptureInput) {}
    public func canAddOutput(_ output: AVCaptureOutput) -> Bool { true }

    public func addOutput(_ output: AVCaptureOutput) {
        guard let movie = output as? AVCaptureMovieFileOutput else { return }
        lock.lock()
        movieOutputs.append(movie)
        lock.unlock()
    }

    public func startRunning() {
        // As on a phone: it blocks, so never on the main thread.
        precondition(!Thread.isMainThread, "startRunning was called on the main thread")
        Thread.sleep(forTimeInterval: startSeconds)
        lock.lock()
        startCount += 1
        if running {
            lock.unlock()
            return
        }
        if startIsInterrupted() {
            interrupted = true
            lock.unlock()
            post(AVCaptureSession.wasInterruptedNotification, [
                AVCaptureSessionInterruptionReasonKey: NSNumber(value: InterruptionReason.audioDeviceInUseByAnotherClient.rawValue),
            ])
            return
        }
        let works = startWorks()
        if works {
            running = true
            if !interruptedStaysUp {
                interrupted = false
            }
        }
        lock.unlock()
        if works {
            post(AVCaptureSession.didStartRunningNotification)
            afterStart?()
        } else {
            // "It reports a failure by posting a runtime error."
            post(AVCaptureSession.runtimeErrorNotification, [
                AVCaptureSessionErrorKey: NSError(domain: AVFoundationErrorDomain, code: AVError.Code.unknown.rawValue),
            ])
        }
    }

    public func stopRunning() {
        precondition(!Thread.isMainThread, "stopRunning was called on the main thread")
        Thread.sleep(forTimeInterval: stopSeconds)
        lock.lock()
        stopCount += 1
        let was = running
        running = false
        if !interruptedStaysUp {
            interrupted = false
        }
        let outputs = movieOutputs
        lock.unlock()
        // A recording its output would never report on ends with the session.
        outputs.forEach { $0.sessionStopped() }
        if was {
            post(AVCaptureSession.didStopRunningNotification)
        }
    }

    // What the phone does to it.

    /// It stops, and says nothing to anyone.
    public func stopUnseen() {
        lock.lock()
        running = false
        lock.unlock()
    }

    /// The phone takes the camera (a call, the app off the screen).
    public func interrupt(_ reason: InterruptionReason, notice: Bool = true) {
        lock.lock()
        running = false
        interrupted = true
        lock.unlock()
        if notice {
            post(AVCaptureSession.wasInterruptedNotification, [
                AVCaptureSessionInterruptionReasonKey: NSNumber(value: reason.rawValue),
            ])
        }
    }

    /// The interruption is over. With `resumes` the phone starts the
    /// session again by itself, as it does for one it interrupted.
    public func endInterruption(resumes: Bool, notice: Bool = true) {
        lock.lock()
        interrupted = false
        if resumes {
            running = true
        }
        lock.unlock()
        if notice {
            post(AVCaptureSession.interruptionEndedNotification)
        }
        if resumes {
            post(AVCaptureSession.didStartRunningNotification)
        }
    }

    /// A runtime error. The session stops: at once, or `late` seconds
    /// after the notice.
    public func fail(code: Int, late: TimeInterval = 0) {
        let error = [AVCaptureSessionErrorKey: NSError(domain: AVFoundationErrorDomain, code: code)]
        if late > 0 {
            post(AVCaptureSession.runtimeErrorNotification, error)
            DispatchQueue.global().asyncAfter(deadline: .now() + late) {
                self.lock.lock()
                self.running = false
                self.lock.unlock()
                self.post(AVCaptureSession.didStopRunningNotification)
            }
        } else {
            lock.lock()
            running = false
            lock.unlock()
            post(AVCaptureSession.runtimeErrorNotification, error)
            post(AVCaptureSession.didStopRunningNotification)
        }
    }

    private func post(_ name: Notification.Name, _ info: [AnyHashable: Any]? = nil) {
        let aboutStartOrStop = name == AVCaptureSession.didStartRunningNotification
            || name == AVCaptureSession.didStopRunningNotification
        if saysNothingOfStartAndStop && aboutStartOrStop { return }
        NotificationCenter.default.post(name: name, object: self, userInfo: info)
    }
}
