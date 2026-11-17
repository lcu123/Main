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
///
/// **A camera that stops with no word at all** (the last five tests). The
/// first build on a real phone opened the record screen black, still
/// offering TAP TO RECORD, and answered every tap with "The video stopped
/// too soon. Please record it again.": the capture session had stopped and
/// the app went on believing it was ready. The stand-in's session can now
/// be stopped the same way (`UITestMode`, point 13): no notice, the status
/// untouched. What notices, what the screen says meanwhile and what a tap
/// does is the code a phone runs.
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

    /// RECORD AGAIN on "Save this video?", and proof that the app took it.
    ///
    /// The first time this test ran, the tap was made on the button in
    /// plain view, about three seconds after the screen came up (as the
    /// clip was reaching its end), and the app stayed where it was. Nothing
    /// in that run could say whether the touch ever reached the button. Now
    /// the app says what it feels and where it goes (UI-test mode: a finger
    /// down on a big button, each change of screen, a tap kept by its
    /// double-tap shield), so the cases can be told apart:
    ///
    /// - the app changed screen: it took the tap (the checks after this
    ///   say whether it went to the right place);
    /// - the button felt the finger and the app did not move, or its
    ///   shield kept the tap: the app's fault, and this fails saying which;
    /// - the app felt nothing at all: the simulator did not deliver the
    ///   touch. That is said out loud as a NOTE in the log, and the tap is
    ///   made once more. If the second goes the same way, the test fails.
    private func tapRecordAgainOnTheReview(file: StaticString = #filePath, line: UInt = #line) {
        // Only what the app says from here on counts.
        let start = server.state().notes.count
        var said: [String] = []
        func heard(_ prefix: String) -> Bool { said.contains { $0.hasPrefix(prefix) } }
        var all: String { said.isEmpty ? "nothing" : said.joined(separator: "; ") }

        for attempt in 1...2 {
            tap(button("RECORD AGAIN"), "RECORD AGAIN", file: file, line: line)
            // The app says "screen review to record" the moment it moves.
            let deadline = Date().addingTimeInterval(5)
            repeat {
                pause(0.3)
                said = Array(server.state().notes.dropFirst(start))
            } while !heard("screen ") && Date() < deadline
            if heard("screen ") { return }

            if heard("shield kept") {
                XCTFail("RECORD AGAIN was tapped on a screen that had been up for seconds, and the app's double-tap shield kept the tap. The app said: \(all)", file: file, line: line)
                return
            }
            if heard("press RECORD AGAIN") {
                XCTFail("The RECORD AGAIN button felt the finger and the app did not act on it. The app said: \(all)", file: file, line: line)
                return
            }
            if attempt == 1 {
                print("NOTE \(shortName): a tap on RECORD AGAIN changed nothing, and the app felt nothing (no button pressed, no tap kept by its shield, no change of screen): the simulator did not deliver the touch. Tapping once more. Since the tap the app said: \(all)")
            }
        }
        XCTFail("RECORD AGAIN was tapped twice and the app stayed on \"\(screenTitle)\". Since the first tap the app said: \(all)", file: file, line: line)
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

        tapRecordAgainOnTheReview()
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

    // MARK: A camera that stops behind the screen's back

    /// What the words under the record button say. (They are the button's
    /// value: TAP TO RECORD, STARTING CAMERA, CAMERA OFF, GET READY.)
    private var underTheButton: String {
        record.exists ? ((record.value as? String) ?? "") : ""
    }

    /// The line the owner's phone showed on every tap. It is for a clip
    /// that really was cut short, and must never answer a tap that
    /// started nothing.
    private static let stoppedTooSoon = "stopped too soon"

    /// The black record screen. The camera was started on "Is this the
    /// stop?" and has stopped since, with no word to the app. The record
    /// screen must not open offering TAP TO RECORD over it: it asks the
    /// camera, says STARTING CAMERA while the camera comes back by itself,
    /// and only then offers the button. Nobody touches anything, nobody
    /// goes BACK, and no line about a video that "stopped" is shown.
    func testTheRecordScreenOpensOnACameraThatStoppedAndBringsItBackByItself() {
        cameraStops = "open"
        cameraRestartSeconds = 8
        launchSignedIn()
        openRecorder(for: Self.smith)

        XCTAssertFalse(record.isEnabled, "The camera is not running: the record button must wait, not be offered.")
        XCTAssertEqual(underTheButton, "STARTING CAMERA", "The screen says what is happening.")
        XCTAssertFalse(element("record-problem").exists, "Nothing has gone wrong that he needs to read about.")

        waitUntil("the camera came back by itself, with no touch", timeout: 60) { self.record.isEnabled }
        XCTAssertEqual(underTheButton, "TAP TO RECORD")
        XCTAssertFalse(element("record-problem").exists)
        XCTAssertFalse(element("camera-away").exists)

        // One tap records, and the clip runs its course.
        tap(record, "the red record button")
        waitFor(recordingSign, "the red RECORDING sign", timeout: 10)
        waitForTitle("Save this video?", timeout: 40)
        XCTAssertFalse(element("cut-note").exists, "Nothing stopped this clip early.")
        saveClip()
        waitForHeld()
        XCTAssertEqual(theOnlyVideo().registers, 1)

        sendTheAuditTrail()
        waitUntil("the server holds the record_stop event", timeout: 60) { !self.recordStops(for: Self.smith).isEmpty }
        let stops = recordStops(for: Self.smith)
        XCTAssertEqual(stops.count, 1, "One tap, one clip: \(stops)")
        XCTAssertNil((stops.first?["detail"] as? [String: Any])?["endedBy"], "And nothing cut it short.")
    }

    /// The camera stops while the record screen is open and nobody is
    /// touching it. Within about a second the screen stops offering the
    /// button and says STARTING CAMERA; the camera comes back by itself;
    /// then one tap records.
    func testACameraThatStopsWhileTheRecordScreenIsOpenIsBroughtBackWithNoTouch() {
        cameraStops = "3"
        cameraRestartSeconds = 8
        launchSignedIn()
        openRecorder(for: Self.smith)

        waitUntil("the screen noticed that the camera had stopped, and the button waits", timeout: 30) { !self.record.isEnabled }
        XCTAssertEqual(underTheButton, "STARTING CAMERA", "The screen says what is happening.")
        XCTAssertFalse(element("record-problem").exists, "No line about a video: none was being recorded.")

        waitUntil("the camera came back by itself, with no touch", timeout: 60) { self.record.isEnabled }
        XCTAssertEqual(underTheButton, "TAP TO RECORD")
        recordClip()
        XCTAssertFalse(element("cut-note").exists)
        saveClip()
        waitForHeld()
        XCTAssertEqual(theOnlyVideo().registers, 1)
    }

    /// The owner's own case, at its worst moment: the screen says TAP TO
    /// RECORD, and the camera stops just as he taps. That tap still
    /// records: the camera is started again on the spot, the red RECORDING
    /// sign comes up, and the clip is saved and sent. "The video stopped
    /// too soon" is never shown. The audit trail says the camera had to be
    /// started at the tap.
    func testATapOnACameraThatHasJustStoppedStillRecords() {
        cameraStops = "tap"
        launchSignedIn()
        openRecorder(for: Self.smith)
        XCTAssertTrue(record.isEnabled, "The camera is ready.")

        tap(record, "the red record button")
        waitFor(recordingSign, "the red RECORDING sign: this very tap records", timeout: 15)
        XCTAssertFalse(element("record-problem").exists, "No line: the tap recorded.")
        waitForTitle("Save this video?", timeout: 40)
        XCTAssertFalse(element("cut-note").exists, "Nothing stopped this clip early.")
        saveClip()
        waitForHeld()
        let video = theOnlyVideo()
        XCTAssertEqual(video.customerId, Self.smith)
        XCTAssertEqual(video.registers, 1)
        XCTAssertEqual(video.completes, [200])

        sendTheAuditTrail()
        waitUntil("the server holds the record_stop event", timeout: 60) { !self.recordStops(for: Self.smith).isEmpty }
        let stops = recordStops(for: Self.smith)
        XCTAssertEqual(stops.count, 1, "One tap, one clip: \(stops)")
        let detail = stops.first?["detail"] as? [String: Any]
        XCTAssertNil(detail?["endedBy"], "Nothing cut it short: \(String(describing: detail))")
        XCTAssertEqual(detail?["camera"] as? String, "started at the tap", "The trail says what the camera needed.")
    }

    /// The same tap, and this time the camera will not start for a moment.
    /// Nothing was recorded, so nothing "stopped too soon": the line says
    /// "The camera did not start. Tap again.", nothing red was ever shown,
    /// the camera comes back by itself, and the next tap records. The audit
    /// trail calls the first tap "not_started", not a clip of no seconds.
    func testATapThatStartsNothingSaysSoAndNeverBlamesAClip() {
        cameraStops = "tap"
        cameraDeadSeconds = 0.6
        launchSignedIn()
        openRecorder(for: Self.smith)

        tap(record, "the red record button")
        waitForLabel(
            element("record-problem"), "the line on the camera screen",
            contains: "The camera did not start. Tap again.", timeout: 15
        )
        XCTAssertFalse(label(of: element("record-problem")).contains(Self.stoppedTooSoon), "No clip is blamed: none began.")
        XCTAssertFalse(recordingSign.exists, "Red only while really recording.")
        XCTAssertFalse(ring.exists)
        XCTAssertNotEqual(screenTitle, "Save this video?", "There is nothing to save.")
        XCTAssertTrue(server.state().videos.isEmpty)

        waitUntil("the camera came back by itself", timeout: 60) { self.record.isEnabled }
        XCTAssertEqual(underTheButton, "TAP TO RECORD")
        tap(record, "the red record button")
        waitFor(recordingSign, "the red RECORDING sign", timeout: 10)
        XCTAssertFalse(element("record-problem").exists, "A new recording clears the line.")
        waitForTitle("Save this video?", timeout: 40)
        XCTAssertFalse(element("cut-note").exists)
        saveClip()
        waitForHeld()
        let video = theOnlyVideo()
        XCTAssertEqual(video.registers, 1, "One video: the tap that recorded.")

        sendTheAuditTrail()
        waitUntil("the server holds both record_stop events", timeout: 60) { self.recordStops(for: Self.smith).count >= 2 }
        let stops = recordStops(for: Self.smith)
        let first = stops.first?["detail"] as? [String: Any]
        XCTAssertEqual(first?["endedBy"] as? String, "not_started", "The first tap: \(String(describing: first))")
        XCTAssertEqual(first?["kept"] as? String, "no")
        XCTAssertEqual(first?["seconds"] as? String, "0.0")
        XCTAssertTrue(((first?["camera"] as? String) ?? "").hasPrefix("stopped"), "And why: \(String(describing: first))")
        XCTAssertNotEqual(stops.first?["videoId"] as? String, video.id)
        let second = stops.last?["detail"] as? [String: Any]
        XCTAssertNil(second?["endedBy"], "The second tap recorded to its limit: \(String(describing: second))")
        XCTAssertEqual(stops.last?["videoId"] as? String, video.id)
    }

    /// A camera that will not start at all. After a few tries of its own
    /// the app stops and says so on a page of its own: "Camera problem",
    /// "The camera could not start.", TRY AGAIN and BACK. No record button
    /// is offered over a camera that is not there. TRY AGAIN asks again,
    /// and once the camera can start the record screen is back and a tap
    /// records.
    func testACameraThatWillNotStartSaysSoAndTryAgainBringsItBack() {
        cameraStops = "open"
        cameraDeadSeconds = 7
        launchSignedIn()
        tap(stop(Self.smith), "the card for stop \(Self.smith)")
        waitForTitle("Is this the stop?")
        tap(button("THIS IS IT"), "THIS IS IT")

        waitForTitle("Camera problem", timeout: 30)
        XCTAssertTrue(hasText("The camera could not start."), "It says so in plain words.")
        XCTAssertTrue(button("TRY AGAIN").exists && button("BACK").exists, "One way on and one way back.")
        XCTAssertFalse(record.exists, "No record button over a camera that is not running.")
        XCTAssertFalse(hasText(Self.stoppedTooSoon))

        // TRY AGAIN, as often as it takes for the camera to be there again.
        var presses = 0
        let deadline = Date().addingTimeInterval(90)
        while !(record.exists && record.isEnabled), Date() < deadline {
            if button("TRY AGAIN").exists {
                tap(button("TRY AGAIN"), "TRY AGAIN")
                presses += 1
            }
            pause(1.5)
        }
        XCTAssertTrue(record.exists && record.isEnabled, "TRY AGAIN was pressed \(presses) time(s) and the camera never came back.")
        XCTAssertGreaterThanOrEqual(presses, 1)
        XCTAssertEqual(underTheButton, "TAP TO RECORD")
        XCTAssertTrue(hasText("Smith, Jane"), "It is still the stop he picked.")

        recordClip()
        saveClip()
        waitForHeld()
        XCTAssertEqual(theOnlyVideo().registers, 1)
    }
}
