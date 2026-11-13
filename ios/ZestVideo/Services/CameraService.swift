import AVFoundation
import Foundation

/// A finished recording, still in the camera's own .mov wrapper.
struct RecordedClip {
    let url: URL
    let startedAt: Date
}

/// The camera. One capture session, made when the tech confirms a stop and
/// shut down as soon as the clip is recorded, so it costs no battery or
/// memory the rest of the day.
///
/// Capture is 720p, 30 frames a second, H.264 + AAC at about 2.5 Mbps: a
/// 15-second clip is roughly 5 MB. Recording stops by itself at the limit.
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

    let session = AVCaptureSession()

    private let queue = DispatchQueue(label: "com.zestlawnpest.video.camera")
    private let output = AVCaptureMovieFileOutput()
    private var videoInput: AVCaptureDeviceInput?
    private var configured = false
    private var onFinish: ((RecordedClip?) -> Void)?

    #if targetEnvironment(simulator)
    /// UI tests only (the simulator has no camera): a stand-in recorder.
    /// See the extension at the end of this file.
    private(set) var standIn = false
    private var standInURL: URL?
    private var standInTimer: DispatchWorkItem?
    #endif

    static let targetBitRate = 2_500_000

    // MARK: Lifecycle (call on the main thread)

    /// Asks for permission if needed, builds the session once, and starts it.
    /// Safe to call again and again.
    func prepare() {
        switch status {
        case .starting, .ready, .recording: return
        default: break
        }
        #if targetEnvironment(simulator)
        if standIn {
            status = .ready
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
        queue.async {
            if self.session.isRunning {
                self.session.stopRunning()
            }
            self.publish(.idle)
        }
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
    /// with the clip, or `nil` if nothing usable was saved.
    func startRecording(to url: URL, maxSeconds: Int, onFinish: @escaping (RecordedClip?) -> Void) {
        guard status == .ready else {
            onFinish(nil)
            return
        }
        self.onFinish = onFinish
        #if targetEnvironment(simulator)
        if standIn {
            startStandInRecording(to: url, maxSeconds: maxSeconds)
            return
        }
        #endif
        queue.async {
            guard self.session.isRunning, !self.output.isRecording else {
                self.deliver(nil)
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
        #if targetEnvironment(simulator)
        if standIn {
            finishStandInRecording()
            return
        }
        #endif
        queue.async {
            if self.output.isRecording {
                self.output.stopRecording()
            }
        }
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
        if let error = error as NSError? {
            finishedCleanly = (error.userInfo[AVErrorRecordingSuccessfullyFinishedKey] as? Bool) ?? false
        }
        // A call or a trip to the home screen cuts the clip short. The file is
        // written in one-second pieces, so what was recorded is still there.
        let usable = finishedCleanly || Files.size(of: outputFileURL) > 0
        let running = session.isRunning
        DispatchQueue.main.async {
            let startedAt = self.recordingStartedAt ?? Date()
            self.recordingStartedAt = nil
            self.status = running ? .ready : .idle
            let handler = self.onFinish
            self.onFinish = nil
            handler?(usable ? RecordedClip(url: outputFileURL, startedAt: startedAt) : nil)
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
        publish(session.isRunning ? .ready : .failed("The camera could not start."))
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

    private func deliver(_ clip: RecordedClip?) {
        DispatchQueue.main.async {
            let handler = self.onFinish
            self.onFinish = nil
            handler?(clip)
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

        let timer = DispatchWorkItem { [weak self] in
            self?.finishStandInRecording()
        }
        standInTimer = timer
        DispatchQueue.main.asyncAfter(deadline: .now() + Double(maxSeconds), execute: timer)
    }

    /// Main thread. The time limit was reached, or the ring was tapped.
    fileprivate func finishStandInRecording() {
        guard status == .recording, let url = standInURL else { return }
        standInTimer?.cancel()
        standInTimer = nil
        standInURL = nil
        let startedAt = recordingStartedAt ?? Date()
        recordingStartedAt = nil
        status = .ready
        let handler = onFinish
        onFinish = nil
        handler?(Files.size(of: url) > 0 ? RecordedClip(url: url, startedAt: startedAt) : nil)
    }

    /// Screenshot mode only: the simulator has no camera, so the record
    /// screens are shown by setting the state they would have on a phone.
    func setScreenshotState(status: Status, recordingStartedAt: Date?) {
        self.status = status
        self.recordingStartedAt = recordingStartedAt
    }
}
#endif
