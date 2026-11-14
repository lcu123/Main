import XCTest

/// A call (or anything else) interrupts a recording.
///
/// The phone does not say whether it took the microphone for a call, for
/// Siri or for an alarm, and the app does not ask (that would mean linking
/// Apple's call framework, which it must not). So the words are "A call or
/// alarm stopped this video early."
///
/// The owner's rules: nothing lost, never stuck, one question per screen,
/// plain words, red only while really recording.
///
/// The simulator cannot be rung. What these tests interrupt is the stand-in
/// recorder (`UITestMode`, point 10): it ends the clip where the "call"
/// comes, says the camera is away, and gives it back after a while, by the
/// same hand-over the real camera uses. Everything after that hand-over is
/// the real app: what it keeps, what it says, where it goes, what it sends.
///
/// What this does NOT show is iOS itself interrupting the real camera when
/// a real call rings, and that the reason it gives is the one the app
/// expects. Only a real phone and a real call can show that.
final class InterruptionUITests: ZestUITestCase {
    private var record: XCUIElement { app.buttons["record-button"] }
    private var recordingSign: XCUIElement { element("recording-pill") }
    private var ring: XCUIElement { app.buttons["countdown-ring"] }

    private func startAClip(file: StaticString = #filePath, line: UInt = #line) {
        tap(record, "the red record button", file: file, line: line)
        waitFor(recordingSign, "the red RECORDING sign", timeout: 10, file: file, line: line)
    }

    /// The `record_stop` events the server holds for one customer, oldest first.
    private func recordStops(for customer: Int) -> [[String: Any]] {
        server.state().events.filter { ($0["type"] as? String) == "record_stop" && ($0["customerId"] as? Int) == customer }
    }

    private func sendTheAuditTrail() {
        leaveAndComeBack()
    }

    /// A call two seconds in. Two seconds is nothing to send: there is no
    /// "Save this video?". The camera screen says what happened in one
    /// line, nothing red is left on it, the camera comes back by itself
    /// when the call is over, and the same big button records again.
    func testACallTwoSecondsInSaysSoAndTheCameraComesBackByItself() {
        // (A long call, so that a slow test machine still finds the camera
        // away when it looks.)
        server.set(["maxSeconds": 6])
        interruption = (at: 2, lasting: 20, cause: "call_or_alarm")
        launchSignedIn()
        openRecorder(for: Self.smith)
        startAClip()

        // The call.
        waitForLabel(
            element("record-problem"), "the line on the camera screen",
            contains: "A call or alarm stopped the video too soon. Please record it again.", timeout: 15
        )
        XCTAssertTrue(record.exists, "The camera screen stays, with its one big button.")
        XCTAssertFalse(record.isEnabled, "While the call has the camera, the button waits.")
        XCTAssertFalse(recordingSign.exists, "Red only while really recording: the RECORDING sign is gone.")
        XCTAssertFalse(ring.exists, "No ring is left counting down.")
        XCTAssertNotEqual(screenTitle, "Save this video?", "Two seconds is not offered for saving.")

        // The call is over: nobody touches anything, and the camera is back.
        waitUntil("the camera came back by itself after the call", timeout: 60) { self.record.isEnabled }
        XCTAssertTrue(server.state().videos.isEmpty, "Nothing was sent.")
        XCTAssertTrue(element("record-problem").exists, "The line stays until he records again.")

        // The same button records again, and this clip runs its course.
        tap(record, "the red record button")
        waitFor(recordingSign, "the red RECORDING sign", timeout: 10)
        XCTAssertFalse(element("record-problem").exists, "A new recording clears the line.")
        waitForTitle("Save this video?", timeout: 40)
        XCTAssertFalse(element("cut-note").exists, "Nothing stopped this clip early, so nothing is said about it.")
        saveClip()
        waitForHeld()

        let video = theOnlyVideo()
        XCTAssertEqual(video.customerId, Self.smith)
        XCTAssertEqual(video.registers, 1, "One video, the second clip. The first never left the phone.")
        XCTAssertEqual(video.completes, [200])

        // The audit trail says what happened to each clip.
        sendTheAuditTrail()
        waitUntil("the server holds both record_stop events", timeout: 60) { self.recordStops(for: Self.smith).count >= 2 }
        let stops = recordStops(for: Self.smith)
        let first = stops.first?["detail"] as? [String: Any]
        XCTAssertEqual(first?["endedBy"] as? String, "call_or_alarm", "The first clip: \(String(describing: first))")
        XCTAssertEqual(first?["kept"] as? String, "no")
        XCTAssertNotEqual(stops.first?["videoId"] as? String, video.id, "It is not the clip that was sent.")
        let second = stops.last?["detail"] as? [String: Any]
        XCTAssertNil(second?["endedBy"], "The second clip ended at its time limit: \(String(describing: second))")
        XCTAssertEqual(stops.last?["videoId"] as? String, video.id)
    }

    /// A call nine seconds in. Nine seconds is worth sending: the clip is
    /// kept and the tech is asked the usual one question, "Save this
    /// video?", with one plain line saying a call or alarm stopped it early.
    func testACallNineSecondsInKeepsTheClipAndSaveThisVideoSaysWhy() {
        server.set(["maxSeconds": 30])
        interruption = (at: 9, lasting: 3, cause: "call_or_alarm")
        launchSignedIn()
        openRecorder(for: Self.smith)
        let began = Date()
        startAClip()

        waitForTitle("Save this video?", timeout: 25)
        XCTAssertLessThan(Date().timeIntervalSince(began), 25, "The clip ended at the call, long before its 30 seconds were up.")
        waitForLabel(element("cut-note"), "the line under the title", contains: "A call or alarm stopped this video early.", timeout: 5)
        XCTAssertTrue(hasText("Smith, Jane"), "It is for the stop that was being recorded.")
        XCTAssertTrue(button("SAVE").exists && button("RECORD AGAIN").exists, "The same two answers as ever.")
        XCTAssertFalse(recordingSign.exists)
        XCTAssertTrue(server.state().videos.isEmpty, "Nothing is sent until the tech says SAVE.")

        saveClip()
        waitForHeld()
        let video = theOnlyVideo()
        XCTAssertEqual(video.customerId, Self.smith)
        XCTAssertEqual(video.registers, 1)
        XCTAssertEqual(video.puts, ["200"])
        XCTAssertEqual(video.completes, [200])

        sendTheAuditTrail()
        waitUntil("the server holds the record_stop event", timeout: 60) { !self.recordStops(for: Self.smith).isEmpty }
        let stop = recordStops(for: Self.smith).last
        let detail = stop?["detail"] as? [String: Any]
        XCTAssertEqual(stop?["videoId"] as? String, video.id)
        XCTAssertEqual(detail?["endedBy"] as? String, "call_or_alarm", "The detail: \(String(describing: detail))")
        XCTAssertEqual(detail?["kept"] as? String, "yes")
        let seconds = Double((detail?["seconds"] as? String) ?? "") ?? 0
        XCTAssertTrue((8.5...11).contains(seconds), "It ran about nine seconds; the trail says \(seconds).")
    }

    /// RECORD AGAIN while the call is still on. The camera screen opens
    /// with one line saying the camera is off during a call, its button
    /// waits, and when the call ends the camera is there with no touch.
    func testRecordAgainDuringTheCallWaitsForTheCameraAndThenRecords() {
        server.set(["maxSeconds": 6])
        interruption = (at: 4, lasting: 30, cause: "call_or_alarm")
        launchSignedIn()
        openRecorder(for: Self.smith)
        startAClip()
        waitForTitle("Save this video?", timeout: 20)
        waitForLabel(element("cut-note"), "the line under the title", contains: "A call or alarm stopped this video early.", timeout: 5)

        tap(button("RECORD AGAIN"), "RECORD AGAIN")
        waitFor(record, "the camera screen again", timeout: 15)
        waitForLabel(
            element("camera-away"), "the line on the camera screen",
            contains: "The camera is off during a call. It comes back by itself.", timeout: 10
        )
        XCTAssertFalse(record.isEnabled, "There is no camera to record with while the call has it.")
        XCTAssertFalse(recordingSign.exists)

        waitUntil("the camera came back by itself after the call", timeout: 60) { self.record.isEnabled }
        XCTAssertFalse(element("camera-away").exists, "The line goes when the camera is back.")
        recordClip()
        XCTAssertFalse(element("cut-note").exists)
        saveClip()
        waitForHeld()
        let video = theOnlyVideo()
        XCTAssertEqual(video.registers, 1, "Only the second clip was sent.")
    }

    /// What the phone does name is said exactly. Another app takes the
    /// camera (split screen on an iPad): the line says so, and does not
    /// blame a call.
    func testAnotherAppTakingTheCameraIsSaidExactly() {
        server.set(["maxSeconds": 30])
        interruption = (at: 5, lasting: 2, cause: "camera_taken")
        launchSignedIn()
        openRecorder(for: Self.smith)
        startAClip()
        waitForTitle("Save this video?", timeout: 25)
        let note = label(of: element("cut-note"))
        XCTAssertTrue(note.contains("Another app took the camera. This video stopped early."), "The line says: \(note)")
        XCTAssertFalse(note.lowercased().contains("call"), "No call is blamed: \(note)")
    }

    /// The home button in the middle of a clip (the same goes for locking
    /// the screen, or a call that is answered and takes the screen). The
    /// phone takes the camera from an app that is off the screen. The clip
    /// so far is saved while the app is in the background, and when the
    /// tech comes back he is on "Save this video?", told why it is short.
    func testLeavingTheAppMidClipKeepsWhatWasRecorded() {
        server.set(["maxSeconds": 15])
        launchSignedIn()
        openRecorder(for: Self.smith)
        startAClip()
        pause(5)

        XCUIDevice.shared.press(.home)
        pause(4)
        XCTAssertTrue(server.state().videos.isEmpty, "Nothing is sent while he is away.")
        app.activate()
        _ = app.wait(for: .runningForeground, timeout: 15)

        waitForTitle("Save this video?", timeout: 30)
        waitForLabel(
            element("cut-note"), "the line under the title",
            contains: "This video stopped early when the app closed.", timeout: 5
        )
        XCTAssertTrue(hasText("Smith, Jane"))
        XCTAssertFalse(recordingSign.exists, "No recording screen is left up.")
        saveClip()
        waitForHeld()
        XCTAssertEqual(theOnlyVideo().registers, 1)
    }

    /// Never stuck. The camera is meant to report the end of every
    /// recording. Here it never does: not at the time limit, not when the
    /// ring is tapped. A few seconds after the time is up the app ends the
    /// recording itself: the red frame comes down and the clip that is on
    /// the phone is offered for saving.
    func testACameraThatNeverReportsIsEndedByTheApp() {
        // (Three-second clips: the app waits four seconds past the limit.)
        silentCamera = true
        launchSignedIn()
        openRecorder(for: Self.smith)
        let began = Date()
        startAClip()

        waitForTitle("Save this video?", timeout: 30)
        XCTAssertGreaterThan(Date().timeIntervalSince(began), 6, "It was given its three seconds and a little more first.")
        XCTAssertFalse(recordingSign.exists, "The red sign is down.")
        waitForLabel(element("cut-note"), "the line under the title", contains: "This video stopped early.", timeout: 5)

        saveClip()
        waitForHeld()
        XCTAssertEqual(theOnlyVideo().completes, [200], "And the clip that was on the phone is sent.")
    }
}
