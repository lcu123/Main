import AVFoundation
import Foundation
import UIKit
import ZestCore

// CameraService.swift with its simulator-only code switched on: the stand-in
// recorder the UI tests use, driven here the way the app's screens drive it
// in those tests. A UI test takes a minute on a Mac; this takes seconds on
// Linux, and says in advance whether the camera's side of a UI test holds.
// (The times are shorter than the tests' own; the order of things is theirs.)

func scenario(_ name: String, standIn: Bool = true, _ body: (CameraService) -> Void) {
    Script.begin(name)
    UITestMode.reset()
    AVCaptureDevice.hasCamera = standIn     // a simulator has no camera
    let before = Script.failures
    let camera = CameraService()
    if standIn {
        camera.useStandInRecorder()         // -ZestUITest 1
    }
    body(camera)
    camera.shutDown()
    Script.soon("idle after BACK") { camera.status == .idle }
    if Script.failures > before {
        print("   what the camera said: \(UITestMode.notes)")
    }
}

/// Tap the stop, THIS IS IT, the record screen appears and is looked at.
func openRecorder(_ camera: CameraService) {
    camera.prepare()        // AppModel.beginConfirm
    Script.soon("ready on \"Is this the stop?\"") { camera.status == .ready }
    Script.wait(0.5)        // the tech reads the name
    camera.prepare()        // AppModel.confirmTarget
    camera.prepare()        // RecordView.onAppear
    Script.wait(0.5)        // nobody taps a screen in the instant it appears
}

func failed(_ camera: CameraService) -> Bool {
    if case .failed = camera.status { return true }
    return false
}

// MARK: What the UI tests already did

scenario("the usual visit (RecordUITests)") { c in
    c.prepare()
    Script.soon("ready at once", within: 0.5) { c.status == .ready }
    Script.wait(0.5)
    c.prepare()
    c.prepare()
    Script.expect(c.status == .ready, "still ready as the record screen opens: \(c.status)")

    var end: RecordingEnd?
    c.startRecording(to: Script.clip(), maxSeconds: 1) { end = $0 }
    Script.soon("the red RECORDING sign", within: 0.5) { c.status == .recording && c.recordingStartedAt != nil }
    Script.soon("handed over at the limit") { end != nil }
    Script.expect(
        end?.clip != nil && end?.cut == nil && end?.neverBegan == nil && end?.camera == nil,
        "a clean clip: \(String(describing: end))"
    )
    Script.expect(c.status == .ready, "ready again at once: \(c.status)")

    c.shutDown()            // "Save this video?"
    Script.expect(c.status == .idle, "idle")
    Script.stays("idle", for: 1.3) { c.status == .idle }
    c.prepare()             // RECORD AGAIN
    c.prepare()
    Script.soon("ready again", within: 0.5) { c.status == .ready }
}

scenario("a call early in a clip (testACallTwoSecondsIn...)") { c in
    UITestMode.interruption = (at: 0.5, lasting: 2.5, cut: .callOrAlarm)
    openRecorder(c)
    var end: RecordingEnd?
    c.startRecording(to: Script.clip(), maxSeconds: 6) { end = $0 }
    Script.soon("cut by the call") { end != nil }
    Script.expect(end?.cut == .callOrAlarm && end?.clip != nil && (end?.seconds ?? 9) < 2, "what was filmed, and why it stopped: \(String(describing: end))")
    Script.expect(
        c.status == .idle && c.away == .callOrAlarm && c.recordingStartedAt == nil,
        "nothing red, and the button waits: \(c.status) \(String(describing: c.away))"
    )
    Script.stays("it waits while the call lasts", for: 1.2) { c.status == .idle && c.away == .callOrAlarm }
    Script.soon("back by itself when the call is over") { c.status == .ready && c.away == nil }
    end = nil
    c.startRecording(to: Script.clip(), maxSeconds: 1) { end = $0 }
    Script.soon("the next clip runs its course") { end != nil }
    Script.expect(end?.cut == nil && end?.clip != nil, "clean: \(String(describing: end))")
}

scenario("RECORD AGAIN while the call is still on (testRecordAgainDuringTheCall...)") { c in
    UITestMode.interruption = (at: 0.5, lasting: 3, cut: .callOrAlarm)
    openRecorder(c)
    var end: RecordingEnd?
    c.startRecording(to: Script.clip(), maxSeconds: 6) { end = $0 }
    Script.soon("cut by the call") { end?.cut == .callOrAlarm }
    c.shutDown()            // "Save this video?"
    Script.wait(0.4)
    Script.expect(c.away == .callOrAlarm, "the call is still on: \(String(describing: c.away))")
    c.prepare()             // RECORD AGAIN
    c.prepare()             // the record screen appears
    Script.expect(c.status != .ready && c.away == .callOrAlarm, "the camera is off during a call: \(c.status) \(String(describing: c.away))")
    Script.stays("and the button waits", for: 1.0) { c.status != .ready && c.away == .callOrAlarm }
    Script.soon("back by itself, and the line goes") { c.status == .ready && c.away == nil }
}

scenario("the home button in the middle of a clip (testLeavingTheAppMidClip...)") { c in
    openRecorder(c)
    var end: RecordingEnd?
    c.startRecording(to: Script.clip(), maxSeconds: 5) { end = $0 }
    Script.soon("recording") { c.status == .recording }
    Script.wait(0.3)
    NotificationCenter.default.post(name: UIApplication.willResignActiveNotification, object: nil)
    NotificationCenter.default.post(name: UIApplication.didEnterBackgroundNotification, object: nil)
    Script.soon("cut as the app left") { end != nil }
    Script.expect(end?.cut == .appClosed && end?.clip != nil, "kept, and why: \(String(describing: end))")
    c.shutDown()            // saved in the background: on to "Save this video?"
    Script.wait(0.3)
    NotificationCenter.default.post(name: UIApplication.willEnterForegroundNotification, object: nil)
    NotificationCenter.default.post(name: UIApplication.didBecomeActiveNotification, object: nil)
    Script.soon("off, with nothing stale to say") { c.status == .idle && c.away == nil }
    c.prepare()             // RECORD AGAIN
    c.prepare()
    Script.soon("there is a camera", within: 0.5) { c.status == .ready }
}

scenario("a camera that never reports (testACameraThatNeverReports...)") { c in
    UITestMode.silentCamera = true
    openRecorder(c)
    var end: RecordingEnd?
    let url = Script.clip()
    c.startRecording(to: url, maxSeconds: 1) { end = $0 }
    Script.soon("recording") { c.status == .recording }
    Script.stays("silent past the limit", for: 1.4) { end == nil && c.status == .recording }
    c.stopRecording()
    Script.wait(0.2)
    Script.expect(end == nil, "the ring is not answered either")
    c.giveUpRecording(of: url)
    Script.expect(
        end?.cut == .unknown && end?.clip != nil && end?.neverBegan == nil,
        "what is on disk is handed over as a clip: \(String(describing: end))"
    )
    Script.expect(c.recordingStartedAt == nil, "the red frame is down")
    Script.soon("ready", within: 0.5) { c.status == .ready }
}

scenario("shut down and asked for again at once; and the other way round") { c in
    openRecorder(c)
    c.shutDown()
    c.prepare()
    Script.soon("ready", within: 0.5) { c.status == .ready }
    c.shutDown()
    Script.wait(0.1)
    c.prepare()
    c.shutDown()
    Script.stays("idle", for: 0.4) { c.status == .idle }
}

// (Screenshot mode is not UI-test mode: there is no stand-in recorder, and a
// simulator has no camera. The record screen is shown by setting its state.)
scenario("screenshot mode: the picture's state is left alone", standIn: false) { c in
    c.setScreenshotState(status: .ready, recordingStartedAt: nil)
    c.prepare()
    c.look()
    // (The app says it is active after the screen is up, as often as not.)
    NotificationCenter.default.post(name: UIApplication.didBecomeActiveNotification, object: nil)
    Script.stays("as it was set", for: 1.3) { c.status == .ready }
    var end: RecordingEnd?
    c.startRecording(to: Script.clip(), maxSeconds: 1) { end = $0 }
    Script.expect(end?.neverBegan != nil && c.status == .ready, "a tap in a picture records nothing and changes nothing: \(c.status)")
    c.shutDown()
    Script.expect(c.status == .idle, "BACK still works")
}

scenario("a simulator with no flags at all: no camera, and it says so", standIn: false) { c in
    c.prepare()
    Script.expect(c.status == .noCamera, "Camera not available: \(c.status)")
    Script.stays("and it stays said", for: 1.2) { c.status == .noCamera }
}

// MARK: The new UI tests: a camera that stops behind the screen's back

scenario("testTheRecordScreenOpensOnACameraThatStoppedAndBringsItBackByItself") { c in
    UITestMode.cameraStops = .onOpen
    UITestMode.cameraRestartSeconds = 1.0
    c.prepare()
    Script.soon("ready on \"Is this the stop?\"") { c.status == .ready }
    Script.wait(0.5)
    c.prepare()             // THIS IS IT: the way in
    Script.expect(c.status == .starting, "STARTING CAMERA before the screen is drawn: \(c.status)")
    c.prepare()             // the record screen appears
    Script.stays("the button waits", for: 0.6) { c.status == .starting }
    Script.soon("back with no touch") { c.status == .ready }
    var end: RecordingEnd?
    c.startRecording(to: Script.clip(), maxSeconds: 1) { end = $0 }
    Script.soon("one tap records") { end != nil }
    Script.expect(end?.clip != nil && end?.cut == nil && end?.neverBegan == nil, "a whole clip: \(String(describing: end))")
}

scenario("testACameraThatStopsWhileTheRecordScreenIsOpenIsBroughtBackWithNoTouch") { c in
    UITestMode.cameraStops = .after(1.5)
    UITestMode.cameraRestartSeconds = 2.0
    openRecorder(c)
    Script.expect(c.status == .ready, "ready: \(c.status)")
    Script.soon("found within about a second of stopping: the button waits", within: 3.5) { c.status == .starting }
    Script.stays("while the camera comes back", for: 1.0) { c.status == .starting }
    Script.soon("back with no touch") { c.status == .ready }
}

scenario("testATapOnACameraThatHasJustStoppedStillRecords") { c in
    UITestMode.cameraStops = .onTap
    openRecorder(c)
    Script.soon("ready") { c.status == .ready }
    var end: RecordingEnd?
    c.startRecording(to: Script.clip(), maxSeconds: 1) { end = $0 }
    Script.soon("this very tap records, got \(String(describing: end))", within: 1) { c.status == .recording }
    Script.soon("handed over") { end != nil }
    Script.expect(end?.clip != nil && end?.cut == nil && end?.neverBegan == nil, "a whole clip: \(String(describing: end))")
    Script.expect(end?.camera == "started at the tap", "the trail says how: \(String(describing: end?.camera))")
}

scenario("testATapThatStartsNothingSaysSoAndNeverBlamesAClip") { c in
    UITestMode.cameraStops = .onTap
    UITestMode.cameraDeadSeconds = 0.6
    openRecorder(c)
    Script.soon("ready") { c.status == .ready }
    var end: RecordingEnd?
    c.startRecording(to: Script.clip(), maxSeconds: 1) { end = $0 }
    Script.soon("the tap is answered") { end != nil }
    Script.expect(
        end?.neverBegan?.hasPrefix("stopped") == true && end?.clip == nil && end?.cut == nil,
        "never began; no clip is blamed: \(String(describing: end))"
    )
    Script.expect(c.status == .starting && c.recordingStartedAt == nil, "nothing red, and the button waits: \(c.status)")
    Script.soon("back by itself") { c.status == .ready }
    end = nil
    c.startRecording(to: Script.clip(), maxSeconds: 1) { end = $0 }
    Script.soon("the next tap records") { end != nil }
    Script.expect(end?.clip != nil && end?.neverBegan == nil && end?.camera == nil, "a whole clip: \(String(describing: end))")
}

scenario("testACameraThatWillNotStartSaysSoAndTryAgainBringsItBack") { c in
    UITestMode.cameraStops = .onOpen
    UITestMode.cameraDeadSeconds = 6
    openRecorder(c)
    let dead = Date().addingTimeInterval(5.5)   // (it stopped half a second ago)
    Script.soon("Camera problem: it could not start", within: 4.5) { failed(c) }
    c.prepare()             // TRY AGAIN, too soon
    Script.expect(c.status == .starting, "trying: \(c.status)")
    Script.soon("it says so again", within: 4.5) { failed(c) }
    Script.wait(max(0, dead.timeIntervalSinceNow) + 0.3)
    c.prepare()             // TRY AGAIN
    Script.soon("the record screen is back, and the button is offered") { c.status == .ready }
}

Script.finish("SIMULATOR")
