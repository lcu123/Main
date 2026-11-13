import XCTest

/// Auto-save. The app is killed with no warning at each point where a video
/// could be lost, then started again WITHOUT wiping anything. Every time the
/// video must still be there, go to the right customer, and reach the server
/// exactly once.
///
/// (`terminate()` is how a test kills the app. It is the closest a simulator
/// gets to a crash or a dead battery; see the README for what it cannot show.)
final class KillUITests: ZestUITestCase {
    /// The screen and the server agree that one video for the stop is sent.
    private func expectOneSentVideo(customer: Int = ZestUITestCase.smith, file: StaticString = #filePath, line: UInt = #line) {
        waitForHeld(timeout: 90, file: file, line: line)
        let video = theOnlyVideo(file: file, line: line)
        XCTAssertEqual(video.customerId, customer, "The video belongs to the stop it was recorded for.", file: file, line: line)
        XCTAssertGreaterThanOrEqual(video.goodUploads, 1, "The whole file arrived: \(video.summary)", file: file, line: line)
        XCTAssertEqual(video.bytes, video.sizeSent, "Every byte arrived: \(video.summary)", file: file, line: line)
        XCTAssertGreaterThanOrEqual(video.goodCompletes, 1, "The server confirmed it: \(video.summary)", file: file, line: line)

        waitForLabel(element("route-header"), "the header", contains: "Sent 2 of 5", timeout: 40, file: file, line: line)
        waitForLabel(element("route-header"), "the header", contains: "Waiting 0", timeout: 20, file: file, line: line)
        tap(app.buttons["tab-videos"], "the My Videos tab", file: file, line: line)
        waitForLabel(videoRow(customer), "the video's row", contains: "Sent", file: file, line: line)
        waitForLabel(element("held-space"), "the space line", contains: "holding 0 MB", file: file, line: line)
        XCTAssertEqual(
            app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "video-row-")).count, 1,
            "My Videos lists one video.", file: file, line: line
        )
    }

    /// (a) Killed in the middle of recording. On the next start the app has
    /// the clip and asks "Save this video?" for the right customer.
    func testKillMidRecordingRecoversTheClip() {
        server.set(["maxSeconds": 15])
        launchAndSignIn()
        openRecorder(for: Self.smith)
        tap(app.buttons["record-button"], "the red record button")
        waitFor(element("recording-pill"), "the red RECORDING sign", timeout: 10)
        pause(1.5)

        kill()
        XCTAssertTrue(server.state().videos.isEmpty)
        relaunch()

        waitForTitle("Save this video?", timeout: 60)
        XCTAssertTrue(hasText("Smith, Jane"), "The recovered clip is for the stop that was being recorded.")
        XCTAssertTrue(server.state().videos.isEmpty, "Nothing is sent until the tech says SAVE.")

        saveClip()
        expectOneSentVideo()
        XCTAssertEqual(server.state().logins, 1, "A restart does not ask the tech to sign in again.")
    }

    /// (b) Killed on the review screen, before SAVE. On the next start My
    /// Route says one video is not saved yet, and finishing it sends it.
    func testKillOnReviewKeepsTheDraft() {
        launchAndSignIn()
        openRecorder(for: Self.smith)
        recordClip()

        kill()
        relaunch()

        let card = app.buttons["draft-card"]
        waitFor(card, "the \"1 video not saved yet\" card", timeout: 60)
        XCTAssertEqual(barTitle, "My Route")
        waitForLabel(stop(Self.smith), "the stop's own card", contains: "Not Saved Yet")
        XCTAssertTrue(server.state().videos.isEmpty, "Nothing is sent until the tech says SAVE.")

        tap(card, "the \"1 video not saved yet\" card")
        waitForTitle("Save this video?")
        XCTAssertTrue(hasText("Smith, Jane"))
        saveClip()
        XCTAssertFalse(app.buttons["draft-card"].exists, "Once saved, the card is gone.")
        expectOneSentVideo()
    }

    /// (c) Killed while the file is half-way to the server. The upload is
    /// finished (by the system, or started again), and the server ends with
    /// one whole video and one confirmation.
    func testKillMidUploadFinishesOnce() {
        // About 15 seconds for the file, so the kill lands in the middle.
        server.set(["uploadBytesPerSecond": 1000])
        launchAndSignIn()
        recordAndSave(for: Self.smith)
        waitUntil("the upload has begun", timeout: 40) { (server.state().videos.first?.putsStarted ?? 0) >= 1 }
        XCTAssertEqual(server.state().videos.first?.goodUploads, 0, "The kill has to land before the upload is done.")

        kill()
        server.set(["uploadBytesPerSecond": 0])
        pause(1)
        relaunch()
        waitForRoute()

        expectOneSentVideo()
        let video = theOnlyVideo()
        XCTAssertEqual(video.goodCompletes, 1, "Exactly one confirmation: \(video.summary)")
        XCTAssertEqual(server.state().videos.count, 1, "One recording, one video on the server.")
    }

    /// (d) Killed after the file arrived, while "complete" was still in the
    /// air. The file is not uploaded a second time and the video ends sent.
    func testKillBeforeCompleteIsAnsweredFinishesOnce() {
        server.set(["completeDelay": 8])
        launchAndSignIn()
        recordAndSave(for: Self.smith)
        waitUntil("the whole file reached the server", timeout: 40) { (server.state().videos.first?.goodUploads ?? 0) >= 1 }
        // The app is now waiting for the server's answer to "complete".
        pause(1)

        kill()
        server.set(["completeDelay": 0])
        relaunch()
        waitForRoute()

        expectOneSentVideo()
        let video = theOnlyVideo()
        XCTAssertEqual(video.puts, ["200"], "The file is uploaded once, not again after the restart: \(video.summary)")
        XCTAssertTrue(video.completes.allSatisfy { $0 == 200 }, "Every confirmation was accepted: \(video.summary)")
    }

    /// (e) Killed the moment after SAVE, before the server answered the
    /// first call. The video is already on disk; it sends after the restart
    /// and the server never has two.
    func testKillRightAfterSaveStillSendsOnce() {
        server.set(["registerDelay": 6])
        launchAndSignIn()
        openRecorder(for: Self.smith)
        recordClip()
        tap(button("SAVE"), "SAVE")
        waitUntil("the app says Saved", timeout: 15) { element("saved-screen").exists || app.buttons["tab-route"].exists }

        kill()
        server.set(["registerDelay": 0])
        relaunch()
        waitForRoute()
        XCTAssertFalse(app.buttons["draft-card"].exists, "A saved video is not offered again as unsaved.")

        expectOneSentVideo()
        let video = theOnlyVideo()
        XCTAssertEqual(video.goodUploads, 1, "The file is uploaded once: \(video.summary)")
        XCTAssertEqual(video.goodCompletes, 1, "Exactly one confirmation: \(video.summary)")
    }

    /// A plain restart with nothing in the air: still signed in, the route
    /// is there at once, and a sent video is still shown as sent, with
    /// nothing sent twice.
    func testRestartKeepsSignInRouteAndSentVideos() {
        launchAndSignIn()
        recordAndSave(for: Self.smith)
        waitForHeld()
        waitForLabel(element("route-header"), "the header", contains: "Sent 2 of 5")
        let before = server.state()

        kill()
        relaunch()
        waitForRoute()

        XCTAssertEqual(barTitle, "My Route", "A restart opens on My Route, not on sign-in.")
        waitForLabel(element("route-header"), "the header", contains: "Sent 2 of 5")
        waitForLabel(stop(Self.smith), "the stop's card", contains: "Change Video")
        XCTAssertFalse(app.buttons["draft-card"].exists)
        pause(2)

        let after = server.state()
        XCTAssertEqual(after.logins, 1, "No second sign-in.")
        XCTAssertEqual(after.registerCalls(), before.registerCalls(), "Nothing is registered again after a restart.")
        XCTAssertEqual(theOnlyVideo().puts, ["200"], "Nothing is uploaded again after a restart.")
        XCTAssertEqual(theOnlyVideo().completes, [200])
    }
}
