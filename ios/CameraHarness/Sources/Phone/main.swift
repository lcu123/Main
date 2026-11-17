import AVFoundation
import Foundation
import UIKit
import ZestCore

// CameraService.swift exactly as a phone compiles it, played through what a
// phone can do to a capture session. (The session is a stand-in a script
// steers: see ../AVFoundation.) Each scenario is one thing that happens on
// a route, the calls the screens make, and what must be true afterwards.

func scenario(_ name: String, _ body: (CameraService) -> Void) {
    Script.begin(name)
    AVCaptureMovieFileOutput.behaviour = .records
    AVCaptureDevice.hasCamera = true
    AVCaptureDevice.allowed = true
    let camera = CameraService()
    body(camera)
    // BACK: whatever happened, nothing may be left on.
    camera.shutDown()
    Script.soon("the session is off after BACK") { !camera.session.isRunning }
    Script.wait(0.15)
    Script.expect(camera.status == .idle && !camera.session.isRunning, "off after BACK: \(camera.status)")
}

/// Tap a stop: "Is this the stop?" warms the camera.
func ready(_ camera: CameraService) {
    camera.prepare()
    Script.soon("ready after prepare, is \(camera.status)") { camera.status == .ready }
    Script.expect(camera.session.isRunning, "ready means running")
}

func failed(_ camera: CameraService) -> Bool {
    if case .failed = camera.status { return true }
    return false
}

/// The session stops and says nothing, as on the first real phone.
func stopUnseen(_ camera: CameraService) {
    camera.session.saysNothingOfStartAndStop = true
    camera.session.stopUnseen()
}

// MARK: The usual day

scenario("the usual visit: three prepares, a clip to the limit, BACK") { c in
    c.prepare()
    Script.expect(c.status == .starting, "STARTING CAMERA at once: \(c.status)")
    Script.soon("ready") { c.status == .ready && c.session.isRunning }
    c.prepare()     // THIS IS IT
    c.prepare()     // the record screen appears
    Script.stays("ready, with one start", for: 0.3) { c.status == .ready && c.session.starts == 1 }

    var end: RecordingEnd?
    c.startRecording(to: Script.clip(), maxSeconds: 1) { end = $0 }
    Script.soon("recording") { c.status == .recording && c.recordingStartedAt != nil }
    Script.soon("handed over") { end != nil }
    Script.expect(
        end?.clip != nil && end?.cut == nil && end?.neverBegan == nil && end?.camera == nil,
        "a clean clip, nothing to say about it: \(String(describing: end))"
    )
    Script.expect(c.status == .ready && c.recordingStartedAt == nil, "ready the moment the red frame comes down: \(c.status)")

    c.shutDown()
    Script.expect(c.status == .idle, "idle at once")
    Script.soon("stopped") { !c.session.isRunning && c.session.stops == 1 }
    // The once-a-second look stops with it.
    Script.stays("nothing starts it again", for: 1.4) { c.session.starts == 1 && c.status == .idle }
}

// MARK: The black record screen (build 27 on the owner's phone)

scenario("THE BUG: the record screen opens on a session that stopped with no word") { c in
    ready(c)
    stopUnseen(c)
    c.prepare()     // the way into the record screen
    Script.expect(c.status == .starting, "no TAP TO RECORD over a stopped session, before the screen is drawn: \(c.status)")
    Script.soon("brought back by itself") { c.status == .ready && c.session.isRunning }
    Script.expect(c.session.starts == 2, "by one start: \(c.session.starts)")
}

scenario("a session that stops unseen while a screen is up is found within about a second") { c in
    ready(c)
    stopUnseen(c)
    let began = Date()
    Script.soon("found: the button is no longer offered", within: 3) { c.status != .ready || c.session.starts == 2 }
    Script.expect(Date().timeIntervalSince(began) < 2.5, "it took \(Date().timeIntervalSince(began)) s")
    Script.soon("started again with no touch") { c.status == .ready && c.session.isRunning && c.session.starts == 2 }
}

scenario("THE BUG: a tap on a session that stopped unseen records") { c in
    ready(c)
    stopUnseen(c)
    var end: RecordingEnd?
    c.startRecording(to: Script.clip(), maxSeconds: 1) { end = $0 }
    Script.soon("this very tap records, got \(String(describing: end))") { c.status == .recording }
    Script.soon("handed over") { end != nil }
    Script.expect(end?.clip != nil && end?.cut == nil && end?.neverBegan == nil, "a whole clip: \(String(describing: end))")
    Script.expect(end?.camera == "started at the tap", "and the trail says how: \(String(describing: end?.camera))")
    Script.expect(c.status == .ready, "ready again: \(c.status)")
}

scenario("a tap that lands while the camera is being started again still records") { c in
    ready(c)
    c.session.startSeconds = 1.0
    stopUnseen(c)
    Script.soon("being started again", within: 3) { c.status == .starting }
    var end: RecordingEnd?
    c.startRecording(to: Script.clip(), maxSeconds: 1) { end = $0 }
    Script.soon("it records once the camera is up, got \(String(describing: end))") { c.status == .recording }
    Script.expect(end == nil, "and nothing was said meanwhile")
    Script.soon("handed over") { end != nil }
    Script.expect(end?.clip != nil && end?.neverBegan == nil && end?.cut == nil, "a whole clip: \(String(describing: end))")
    c.session.startSeconds = 0.05
}

scenario("a tap when the camera will not start: never began, three more tries, then TRY AGAIN") { c in
    ready(c)
    stopUnseen(c)
    c.session.startWorks = { false }
    var end: RecordingEnd?
    c.startRecording(to: Script.clip(), maxSeconds: 1) { end = $0 }
    Script.soon("the tap is answered") { end != nil }
    Script.expect(
        end?.neverBegan?.hasPrefix("stopped") == true && end?.clip == nil && end?.cut == nil,
        "never began; no clip is called cut short: \(String(describing: end))"
    )
    Script.expect(c.status != .ready, "the button is not offered: \(c.status)")
    Script.soon("it gives up and says so", within: 6) { failed(c) }
    Script.expect(c.session.starts == 5, "the first start, the tap's own, and three more: \(c.session.starts)")
    Script.stays("and then leaves it alone", for: 2.5) { c.session.starts == 5 && failed(c) }
    c.session.startWorks = { true }
    c.prepare()     // TRY AGAIN
    Script.soon("TRY AGAIN starts it") { c.status == .ready && c.session.isRunning }
}

scenario("the phone will not record on a session that says it is running") { c in
    ready(c)
    AVCaptureMovieFileOutput.behaviour = .refuses(AVError.Code.recordingFailed.rawValue)
    var end: RecordingEnd?
    c.startRecording(to: Script.clip(), maxSeconds: 1) { end = $0 }
    Script.soon("the tap is answered") { end != nil }
    Script.expect(
        end?.neverBegan?.hasPrefix("refused -11803") == true && end?.cut == nil && end?.clip == nil,
        "never began, with the phone's own number: \(String(describing: end))"
    )
    Script.expect(c.status == .starting, "the button waits: \(c.status)")
    Script.soon("the session is stopped and started afresh") {
        c.status == .ready && c.session.stops == 1 && c.session.starts == 2
    }
    AVCaptureMovieFileOutput.behaviour = .records
    end = nil
    c.startRecording(to: Script.clip(), maxSeconds: 1) { end = $0 }
    Script.soon("the next tap records") { end != nil }
    Script.expect(end?.clip != nil && end?.neverBegan == nil, "a whole clip: \(String(describing: end))")
}

scenario("a call takes the camera at the very tap: nothing began, and the screen says CAMERA OFF, not \"tap again\"") { c in
    ready(c)
    AVCaptureMovieFileOutput.behaviour = .refuses(AVError.Code.sessionWasInterrupted.rawValue)
    var end: RecordingEnd?
    var awayWhenTold: RecordingCut?
    c.startRecording(to: Script.clip(), maxSeconds: 1) {
        end = $0
        awayWhenTold = c.away
    }
    // The phone's own notice comes a moment after the refusal.
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
        c.session.interrupt(.audioDeviceInUseByAnotherClient)
    }
    Script.soon("the tap is answered") { end != nil }
    Script.expect(end?.neverBegan?.hasPrefix("refused -11818") == true && end?.cut == nil, "never began: \(String(describing: end))")
    Script.expect(awayWhenTold == .callOrAlarm, "by then the screen knows a call has the camera: \(String(describing: awayWhenTold))")
    Script.expect(CameraRule.didNotStartLine(away: awayWhenTold) == nil, "so it does not say \"Tap again.\"")
    Script.expect(c.status == .idle && c.session.stops == 0, "CAMERA OFF, and the session is left for the phone to give back: \(c.status), stops \(c.session.stops)")
    c.session.endInterruption(resumes: true)
    Script.soon("back by itself") { c.status == .ready && c.away == nil }
}

scenario("a full phone at the tap keeps its own line") { c in
    ready(c)
    AVCaptureMovieFileOutput.behaviour = .refuses(AVError.Code.diskFull.rawValue)
    var end: RecordingEnd?
    c.startRecording(to: Script.clip(), maxSeconds: 1) { end = $0 }
    Script.soon("the tap is answered") { end != nil }
    Script.expect(end?.cut == .phoneFull && end?.neverBegan == nil && end?.clip == nil, "the phone ran out of room: \(String(describing: end))")
    Script.expect(c.status == .ready, "and the camera is still there: \(c.status)")
}

scenario("a camera that never answers, with nothing on disk, is not a clip cut short") { c in
    ready(c)
    AVCaptureMovieFileOutput.behaviour = .silent
    var end: RecordingEnd?
    let url = Script.clip()
    c.startRecording(to: url, maxSeconds: 1) { end = $0 }
    Script.wait(0.4)
    c.giveUpRecording(of: url)
    Script.expect(end?.neverBegan?.hasPrefix("silent") == true && end?.cut == nil, "never began: \(String(describing: end))")
    Script.soon("ready") { c.status == .ready }
}

// (Before this, the silent session was left running and "ready" came back
// over it; the second tap was refused as "busy", and so was every tap
// after it until BACK.)
scenario("a session that says it is running and gives the file output nothing is stopped and started afresh; the next tap records") { c in
    ready(c)
    AVCaptureMovieFileOutput.behaviour = .silent
    var end: RecordingEnd?
    let url = Script.clip()
    c.startRecording(to: url, maxSeconds: 1) { end = $0 }
    Script.soon("the file output has been asked to record") { AVCaptureMovieFileOutput.last?.isRecording == true }
    Script.wait(0.3)
    Script.expect(end == nil && c.recordingStartedAt == nil, "and says nothing")
    c.giveUpRecording(of: url)      // AppModel's own watch, a few seconds past the limit
    Script.expect(end?.neverBegan?.hasPrefix("silent") == true && end?.cut == nil, "never began: \(String(describing: end))")
    Script.expect(c.status != .ready, "the button waits meanwhile: \(c.status)")
    Script.soon("stopped and started afresh, as BACK would") {
        c.status == .ready && c.session.stops == 1 && c.session.starts == 2
    }
    AVCaptureMovieFileOutput.behaviour = .records
    end = nil
    c.startRecording(to: Script.clip(), maxSeconds: 1) { end = $0 }
    Script.soon("the next tap records") { end != nil }
    Script.expect(end?.clip != nil && end?.neverBegan == nil && end?.cut == nil, "a whole clip, not \"busy\": \(String(describing: end))")
}

scenario("a clip given up on that the file output never ends: the next tap says so once and ends it with the session, and the tap after records") { c in
    ready(c)
    AVCaptureMovieFileOutput.behaviour = .neverEnds
    var end: RecordingEnd?
    let url = Script.clip()
    c.startRecording(to: url, maxSeconds: 1) { end = $0 }
    Script.soon("recording") { c.status == .recording }
    c.stopRecording()               // the ring: not answered either
    Script.wait(0.3)
    Script.expect(end == nil && c.status == .recording, "not a word: \(c.status)")
    c.giveUpRecording(of: url)      // AppModel's own watch
    Script.expect(
        end?.clip != nil && end?.cut == .unknown && end?.neverBegan == nil && end?.camera == "silent",
        "what is on disk is handed over as a clip: \(String(describing: end))"
    )
    Script.soon("ready") { c.status == .ready }
    Script.expect(c.session.stops == 0, "a clip that did begin: the session is left as it is: stops \(c.session.stops)")
    Script.expect(AVCaptureMovieFileOutput.last?.isRecording == true, "and the file output still holds that recording")

    AVCaptureMovieFileOutput.behaviour = .records
    end = nil
    var statusWhenTold: CameraService.Status?
    c.startRecording(to: Script.clip(), maxSeconds: 1) {
        end = $0
        statusWhenTold = c.status
    }
    Script.soon("the tap is answered") { end != nil }
    Script.expect(
        end?.neverBegan?.hasPrefix("busy") == true && end?.clip == nil && end?.cut == nil,
        "never began; no clip is blamed: \(String(describing: end))"
    )
    Script.expect(statusWhenTold == .starting, "and the button waits: \(String(describing: statusWhenTold))")
    Script.soon("the old recording is ended with the session, and the session is started afresh") {
        c.status == .ready && c.session.stops == 1 && c.session.starts == 2
            && AVCaptureMovieFileOutput.last?.isRecording == false
    }
    end = nil
    c.startRecording(to: Script.clip(), maxSeconds: 1) { end = $0 }
    Script.soon("the tap after records") { end != nil }
    Script.expect(end?.clip != nil && end?.neverBegan == nil && end?.cut == nil, "a whole clip: \(String(describing: end))")
}

scenario("given up on while the camera was still starting at the tap: nothing is recorded afterwards") { c in
    ready(c)
    c.session.startSeconds = 1.5
    stopUnseen(c)
    var end: RecordingEnd?
    let url = Script.clip()
    c.startRecording(to: url, maxSeconds: 1) { end = $0 }
    Script.wait(0.4)
    c.giveUpRecording(of: url)
    Script.expect(end?.neverBegan?.hasPrefix("silent") == true, "never began: \(String(describing: end))")
    Script.soon("the camera is there for the next tap", within: 6) { c.status == .ready }
    Script.stays("no recording with nobody waiting for it", for: 0.5) {
        AVCaptureMovieFileOutput.last?.isRecording == false && c.recordingStartedAt == nil && c.status == .ready
    }
    // (The file output was never asked: this is not the silent session above.)
    Script.expect(
        c.session.stops == 0 && c.session.starts == 2,
        "the session the tap started is kept, not stopped and started once more: starts \(c.session.starts), stops \(c.session.stops)"
    )
    c.session.startSeconds = 0.05
}

// MARK: Start and stop overtaking each other

scenario("shut down and asked for again at once (RECORD AGAIN tapped fast)") { c in
    ready(c)
    c.shutDown()
    c.prepare()
    Script.expect(c.status == .starting, "STARTING CAMERA: \(c.status)")
    Script.soon("not left off for ever") { c.status == .ready && c.session.isRunning }
    Script.expect(c.session.starts == 2 && c.session.stops >= 1, "with a start of its own: starts \(c.session.starts), stops \(c.session.stops)")
}

scenario("asked for and shut down at once (BACK tapped fast)") { c in
    c.prepare()
    c.shutDown()
    Script.expect(c.status == .idle, "idle at once")
    Script.stays("off, and it stays off", for: 1.8) { c.status == .idle }
    Script.expect(!c.session.isRunning && c.session.starts <= 1, "not running: starts \(c.session.starts)")
}

scenario("signed out in the middle of a clip") { c in
    ready(c)
    var end: RecordingEnd?
    c.startRecording(to: Script.clip(), maxSeconds: 6) { end = $0 }
    Script.soon("recording") { c.status == .recording }
    c.shutDown()
    Script.expect(c.status == .recording, "the clip is handed over first")
    AVCaptureMovieFileOutput.last?.cut(code: AVError.Code.sessionWasInterrupted.rawValue)
    Script.soon("handed over, then off") { end != nil && c.status == .idle && !c.session.isRunning }
}

// MARK: The phone takes the camera, and gives it back

scenario("a call while the screen waits, then over") { c in
    ready(c)
    c.session.interrupt(.audioDeviceInUseByAnotherClient)
    Script.soon("CAMERA OFF, and why") { c.status == .idle && c.away == .callOrAlarm }
    Script.stays("waited for, not fought", for: 1.6) { c.session.starts == 1 && c.status == .idle && c.away == .callOrAlarm }
    c.session.endInterruption(resumes: false)
    Script.soon("back by itself") { c.status == .ready && c.away == nil && c.session.isRunning }
}

scenario("interrupted in the very moment it starts") { c in
    let session = c.session
    session.afterStart = { session.interrupt(.audioDeviceInUseByAnotherClient) }
    c.prepare()
    Script.soon("CAMERA OFF") { c.status == .idle && c.away == .callOrAlarm }
    Script.stays("never left saying ready", for: 1.3) { c.status == .idle }
    session.afterStart = nil
    session.endInterruption(resumes: true)
    Script.soon("back") { c.status == .ready && c.away == nil }
}

scenario("the screen opens on a session the phone has interrupted without a word") { c in
    ready(c)
    c.session.interrupt(.audioDeviceInUseByAnotherClient, notice: false)
    c.session.startIsInterrupted = { true }
    c.prepare()
    Script.expect(c.status == .starting, "not ready, before the screen is drawn: \(c.status)")
    Script.soon("CAMERA OFF") { c.status == .idle && c.away != nil }
    var end: RecordingEnd?
    c.startRecording(to: Script.clip(), maxSeconds: 1) { end = $0 }
    Script.expect(end?.neverBegan?.hasPrefix("away") == true && end?.cut == nil, "a press now starts nothing and blames no clip: \(String(describing: end))")
    c.session.startIsInterrupted = { false }
    c.session.endInterruption(resumes: false)
    Script.soon("back") { c.status == .ready && c.away == nil }
}

scenario("the app comes back to the front while a call still has the camera") { c in
    ready(c)
    c.session.interrupt(.audioDeviceInUseByAnotherClient)
    c.session.startIsInterrupted = { true }
    Script.soon("CAMERA OFF") { c.status == .idle && c.away == .callOrAlarm }
    NotificationCenter.default.post(name: UIApplication.didBecomeActiveNotification, object: nil)
    Script.wait(0.5)
    Script.soon("still off, and it says why") { c.status == .idle && c.away == .callOrAlarm }
    c.session.startIsInterrupted = { false }
    c.session.endInterruption(resumes: true)
    Script.soon("back") { c.status == .ready && c.away == nil }
}

scenario("an interrupted session is stopped by BACK; one the phone starts again with nobody wanting it is stopped again") { c in
    ready(c)
    c.session.interrupt(.videoDeviceNotAvailableInBackground)
    Script.soon("CAMERA OFF") { c.status == .idle && c.away == .appClosed }
    c.shutDown()
    Script.soon("stopped, though it was not running") { c.session.stops == 1 && !c.session.isInterrupted }
    Script.soon("and nothing stale is left to say") { c.away == nil }
    c.session.endInterruption(resumes: true)    // the phone starts it again all the same
    Script.soon("no camera left on under My Route") { !c.session.isRunning && c.status == .idle }
}

// (The side button near the end of a clip, the clip kept, "Save this
// video?" shutting the camera down behind the lock screen, RECORD AGAIN.
// If a phone's "interrupted" outlives that stop, the session then says both
// at once. Before this, the button was lit, the line said the camera was
// off, and every tap was refused without a word.)
scenario("a session that says it is running and interrupted at once is running: no line about the camera being off, and a tap records") { c in
    ready(c)
    c.session.interruptedStaysUp = true
    c.session.interrupt(.videoDeviceNotAvailableInBackground)
    Script.soon("CAMERA OFF") { c.status == .idle && c.away == .appClosed }
    c.shutDown()    // "Save this video?"
    Script.soon("stopped") { c.session.stops == 1 }
    c.prepare()     // RECORD AGAIN
    Script.soon("running, and still saying interrupted") { c.session.isRunning && c.session.isInterrupted }
    Script.soon("TAP TO RECORD, with nothing said about the camera being off") { c.status == .ready && c.away == nil }
    Script.stays("and the once-a-second look does not bring the line back", for: 1.3) { c.status == .ready && c.away == nil }
    var end: RecordingEnd?
    c.startRecording(to: Script.clip(), maxSeconds: 1) { end = $0 }
    Script.soon("the tap is answered") { end != nil }
    Script.expect(end?.clip != nil && end?.neverBegan == nil && end?.cut == nil, "it recorded: \(String(describing: end))")
    Script.expect(c.status == .ready && c.away == nil, "and the button is offered again: \(c.status)")
}

scenario("a session left running from an earlier use is not taken over: the next use starts afresh") { c in
    ready(c)
    c.shutDown()
    Script.soon("off") { !c.session.isRunning }
    c.session.saysNothingOfStartAndStop = true
    c.session.endInterruption(resumes: true, notice: false)     // running again, unseen
    let stops = c.session.stops
    c.prepare()
    Script.soon("ready") { c.status == .ready && c.session.isRunning }
    Script.expect(c.session.stops == stops + 1, "stopped first, then started: stops \(c.session.stops)")
}

scenario("nothing is started with the app off the screen; it is when the app is back") { c in
    ready(c)
    NotificationCenter.default.post(name: UIApplication.willResignActiveNotification, object: nil)
    stopUnseen(c)
    Script.soon("STARTING CAMERA", within: 3) { c.status == .starting }
    Script.stays("and it waits", for: 1.3) { c.session.starts == 1 && c.status == .starting }
    NotificationCenter.default.post(name: UIApplication.didBecomeActiveNotification, object: nil)
    Script.soon("back with the app") { c.status == .ready && c.session.isRunning }
}

// MARK: The session fails

scenario("a runtime error, the session stopping a moment after the notice") { c in
    ready(c)
    c.session.fail(code: AVError.Code.mediaServicesWereReset.rawValue, late: 0.2)
    Script.soon("started again") { c.session.starts == 2 && c.status == .ready && c.session.isRunning }
}

scenario("the same, with no word that it stopped") { c in
    ready(c)
    c.session.saysNothingOfStartAndStop = true
    c.session.fail(code: AVError.Code.mediaServicesWereReset.rawValue, late: 0.2)
    Script.soon("started again") { c.session.starts == 2 && c.status == .ready && c.session.isRunning }
}

scenario("a runtime error in the first seconds of a clip") { c in
    ready(c)
    var end: RecordingEnd?
    c.startRecording(to: Script.clip(), maxSeconds: 6) { end = $0 }
    Script.soon("recording") { c.status == .recording }
    c.session.fail(code: AVError.Code.mediaServicesWereReset.rawValue)
    AVCaptureMovieFileOutput.last?.cut(code: AVError.Code.sessionWasInterrupted.rawValue)
    Script.soon("the red frame is down") { c.recordingStartedAt == nil }
    Script.soon("handed over") { end != nil }
    Script.expect(
        end?.cut == .unknown && end?.neverBegan == nil && (end?.seconds ?? 9) < 3,
        "a real clip, really cut: \(String(describing: end))"
    )
    Script.expect(end?.camera == "error -11818", "with the phone's own number: \(String(describing: end?.camera))")
    Script.soon("and the camera is started again, which nothing did before") { c.status == .ready && c.session.isRunning }
}

scenario("a session that dies a moment after every start is given up on, not started for ever") { c in
    let session = c.session
    session.saysNothingOfStartAndStop = true
    session.afterStart = {
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.1) { session.stopUnseen() }
    }
    c.prepare()
    Script.soon("it says it could not start", within: 15) { failed(c) }
    Script.expect(session.starts <= 5, "after a few tries: \(session.starts)")
    let tries = session.starts
    Script.stays("and no more", for: 3) { session.starts == tries }
    session.afterStart = nil
}

// MARK: No camera, no permission

scenario("no permission, then given") { c in
    AVCaptureDevice.allowed = false
    c.prepare()
    Script.soon("Camera is turned off") { c.status == .denied }
    Script.stays("left alone", for: 1.3) { c.status == .denied && c.session.starts == 0 }
    AVCaptureDevice.allowed = true
    c.prepare()     // back from Settings
    Script.soon("ready") { c.status == .ready }
}

scenario("no camera") { c in
    AVCaptureDevice.hasCamera = false
    c.prepare()
    Script.expect(c.status == .noCamera, "said at once: \(c.status)")
    Script.stays("and it stays said", for: 1.3) { c.status == .noCamera }
}

Script.finish("PHONE")
