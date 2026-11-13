import XCTest

/// Recording, reviewing and saving a video, and changing or deleting it
/// inside its ten minutes. Every test checks the screen and the server.
final class RecordUITests: ZestUITestCase {
    /// Tap the stop, THIS IS IT, record, SAVE. The server gets exactly one
    /// register, the whole file once, and exactly one "complete". The phone
    /// then says SENT and holds no file.
    func testHappyPathRecordSaveSend() {
        launchAndSignIn()
        openRecorder(for: Self.smith)
        XCTAssertTrue(hasText("Smith, Jane"), "The record screen names the stop.")
        XCTAssertTrue(app.buttons["record-button"].isEnabled, "The camera is ready at once.")

        recordClip()
        XCTAssertEqual(barTitle, "Review")
        XCTAssertTrue(hasText("Smith, Jane"), "The review screen names the stop.")
        XCTAssertTrue(app.buttons["clip"].exists, "The clip plays back on the review screen.")
        XCTAssertTrue(server.state().videos.isEmpty, "Nothing goes to the server before SAVE.")

        saveClip()
        waitForHeld()

        let seen = server.state()
        let video = theOnlyVideo()
        XCTAssertEqual(video.customerId, Self.smith)
        XCTAssertEqual(seen.registerCalls(), 1, "Exactly one register call.")
        XCTAssertEqual(video.registers, 1)
        XCTAssertEqual(video.puts, ["200"], "The file is uploaded exactly once.")
        XCTAssertGreaterThan(video.bytes, 1000, "A real video file arrived.")
        XCTAssertEqual(video.bytes, video.sizeSent, "Every byte the phone promised arrived.")
        XCTAssertEqual(video.completes, [200], "Exactly one complete call.")
        XCTAssertFalse(video.override)
        XCTAssertFalse(video.recommend)
        XCTAssertFalse(video.notOnMyList)
        XCTAssertEqual(video.signals["spoken"], "match", "The words named this stop.")
        XCTAssertEqual(video.signals["checkIn"], "match", "The clip was recorded inside the check-in.")

        // The screen agrees with the server.
        waitForLabel(element("route-header"), "the header", contains: "Sent 2 of 5")
        XCTAssertTrue(routeHeader.contains("Waiting 0"), "Nothing is left waiting. The header says: \(routeHeader)")
        waitForLabel(stop(Self.smith), "the stop's card", contains: "Change Video")
        XCTAssertFalse(app.buttons["waiting-banner"].exists)

        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForBar("My Videos")
        waitForLabel(videoRow(Self.smith), "the video's row", contains: "Sent")
        waitForLabel(element("held-space"), "the space line", contains: "holding 0 MB")
    }

    /// The record screen's own buttons: FLIP, BACK, and the ring, which only
    /// ends the clip early once three seconds have passed. A clip ended
    /// early still saves and sends.
    func testRecordScreenBackFlipAndEarlyStop() {
        server.set(["maxSeconds": 15])
        launchAndSignIn()
        openRecorder(for: Self.smith)

        let flip = app.buttons["flip-button"]
        waitFor(flip, "FLIP")
        XCTAssertEqual(flip.label, "Flip to front camera")
        tap(flip, "FLIP")
        waitForLabel(flip, "FLIP", contains: "back camera", timeout: 10)
        pause(0.5)
        tap(flip, "FLIP")
        waitForLabel(flip, "FLIP", contains: "front camera", timeout: 10)

        tap(button("BACK"), "BACK")
        waitForBar("My Route")
        XCTAssertFalse(app.buttons["draft-card"].exists, "BACK before recording leaves nothing behind.")

        openRecorder(for: Self.smith)
        let ring = app.buttons["countdown-ring"]
        waitUntilTappable(app.buttons["record-button"])
        let tappedRecord = Date()
        app.buttons["record-button"].tap()
        let recordTapReturned = Date()
        waitFor(element("recording-pill"), "the red RECORDING sign", timeout: 10)
        waitFor(ring, "the countdown ring", timeout: 5)

        // A tap in the first three seconds must do nothing. Recording began
        // at least 0.35 s after the record tap, so a ring tap that is over
        // within 3 s of it was certainly early. (The exact rule is also
        // checked without a clock in ZestCore:
        // testRingTapOnlyEndsTheClipAfterThreeSeconds.)
        ring.tap()
        if Date().timeIntervalSince(tappedRecord) < 3.0 {
            pause(1.2)
            XCTAssertTrue(
                ring.exists && screenTitle != "Save this video?",
                "A tap on the ring in the first 3 seconds must not end the clip."
            )
        } else {
            print("NOTE \(shortName): the simulator was too slow to tap inside the first 3 seconds; that check was skipped.")
        }

        // After three seconds the same tap ends the clip.
        if ring.exists && screenTitle != "Save this video?" {
            let wait = 4.3 - Date().timeIntervalSince(recordTapReturned)
            if wait > 0 { pause(wait) }
            ring.tap()
        }
        waitForTitle("Save this video?", timeout: 8)
        XCTAssertLessThan(
            Date().timeIntervalSince(tappedRecord), 13,
            "The clip should have ended at the tap, well before its 15 seconds were up."
        )

        saveClip()
        waitForHeld()
        XCTAssertEqual(theOnlyVideo().completes, [200])
    }

    /// RECORD AGAIN on the review screen throws the first clip away: only
    /// the second one ever reaches the server. The "I recommended something"
    /// switch is sent with it.
    func testRecordAgainDiscardsFirstClipAndRecommendIsSent() {
        launchAndSignIn()
        openRecorder(for: Self.smith)
        recordClip()

        tap(button("RECORD AGAIN"), "RECORD AGAIN")
        waitFor(app.buttons["record-button"], "the record screen again")
        XCTAssertTrue(hasText("Smith, Jane"), "RECORD AGAIN keeps the same stop.")
        recordClip()

        let toggle = app.buttons["recommend-switch"]
        waitFor(toggle, "the \"I recommended something\" switch")
        XCTAssertEqual(toggle.value as? String, "No", "The switch starts off.")
        // A double tap turns it on once; it must not flip straight back off.
        doubleTap(toggle, "the \"I recommended something\" switch")
        waitUntil("the switch says Yes", timeout: 10) { (toggle.value as? String) == "Yes" }
        pause(1)
        XCTAssertEqual(toggle.value as? String, "Yes", "A double tap must leave the switch on.")

        saveClip()
        waitForHeld()
        let seen = server.state()
        let video = theOnlyVideo()
        XCTAssertEqual(seen.registerCalls(), 1, "The first clip was never sent.")
        XCTAssertTrue(video.recommend, "recommend: true goes with the video.")
        XCTAssertEqual(video.customerId, Self.smith)
        XCTAssertEqual(video.completes, [200])
    }

    /// The words name a different customer: one warning. SAVE ANYWAY sends
    /// the video as it is, marked as an override.
    func testTwoKeysWarningSaveAnywaySendsOverride() {
        launchAndSignIn(transcript: "This is the Smith home")
        openRecorder(for: Self.johnson)
        recordClip()

        tap(button("SAVE"), "SAVE")
        waitForTitle("Is this the right stop?")
        waitForText("I did not hear this name or street.")
        waitForText("It sounded like Smith, Jane.")
        XCTAssertTrue(hasText("Johnson, Robert"), "The warning shows the stop that was picked.")
        XCTAssertTrue(server.state().videos.isEmpty, "Nothing is sent while the warning is up.")

        tap(button("SAVE ANYWAY"), "SAVE ANYWAY")
        waitFor(app.buttons["tab-route"], "My Route after SAVE ANYWAY", timeout: 30)
        answerFirstSaveQuestion()
        waitForHeld()

        let video = theOnlyVideo()
        XCTAssertEqual(video.customerId, Self.johnson)
        XCTAssertTrue(video.override, "override: true goes with a video saved past the warning.")
        XCTAssertEqual(video.signals["spoken"], "mismatch")
        XCTAssertEqual(video.completes, [200])
    }

    /// The same warning, but the tech picks the stop the words fit: the same
    /// clip goes to that customer, with no override.
    func testTwoKeysWarningPickTheRightStopRetargets() {
        launchAndSignIn(transcript: "This is the Smith home")
        openRecorder(for: Self.johnson)
        recordClip()

        tap(button("SAVE"), "SAVE")
        waitForTitle("Is this the right stop?")
        tap(button("PICK THE RIGHT STOP"), "PICK THE RIGHT STOP")
        waitForTitle("Pick the right stop")
        waitForText("It sounded like this one")

        // BACK returns to the clip without changing anything.
        tap(button("BACK"), "BACK")
        waitForTitle("Save this video?")
        XCTAssertTrue(hasText("Johnson, Robert"))
        tap(button("SAVE"), "SAVE")
        waitForTitle("Is this the right stop?")
        tap(button("PICK THE RIGHT STOP"), "PICK THE RIGHT STOP")
        waitForTitle("Pick the right stop")

        tap(stop(Self.smith), "the stop it sounded like")
        waitForTitle("Save this video?")
        XCTAssertTrue(hasText("Smith, Jane"), "The clip now belongs to the stop that was picked.")

        saveClip()
        waitForHeld()
        let seen = server.state()
        let video = theOnlyVideo()
        XCTAssertEqual(video.customerId, Self.smith, "The video goes to the customer that was picked.")
        XCTAssertFalse(video.override)
        XCTAssertEqual(video.signals["spoken"], "match")
        XCTAssertEqual(seen.registerCalls(), 1)
        waitForLabel(stop(Self.johnson), "the stop first picked by mistake", contains: "Record Video")
    }

    /// Inside its ten minutes a stop offers CHANGE VIDEO. RECORD AGAIN there
    /// replaces the old video with the new one, with no question asked.
    func testChangeVideoRecordAgainReplacesTheOldOne() {
        launchAndSignIn()
        recordAndSave(for: Self.smith)
        waitForHeld()
        waitForLabel(stop(Self.smith), "the stop's card", contains: "Change Video")
        XCTAssertTrue(label(of: stop(Self.smith)).contains("Min Left"), "The card says how long is left.")

        tap(stop(Self.smith), "the stop with a video")
        waitForBar("Video")
        waitForText("You can change it for")
        XCTAssertTrue(button("DELETE VIDEO").exists)
        tap(button("RECORD AGAIN"), "RECORD AGAIN")
        waitFor(app.buttons["record-button"], "the record screen")
        recordClip()
        saveClip()

        waitUntil("the new video is confirmed and the old one is gone", timeout: 60) {
            let videos = server.state().videos
            return videos.count == 2 && videos[1].isHeld && videos[0].state != "held"
        }
        let videos = server.state().videos
        XCTAssertEqual(videos.count, 2, "Two recordings, so the server heard of two videos.")
        XCTAssertTrue(["replaced", "deleted"].contains(videos[0].state), "The first video no longer counts: \(videos[0].summary)")
        XCTAssertTrue(videos[1].isHeld, "The second video is the one kept: \(videos[1].summary)")
        XCTAssertEqual(videos[1].customerId, Self.smith)
        XCTAssertNotEqual(videos[0].id, videos[1].id)

        waitForLabel(element("route-header"), "the header", contains: "Sent 2 of 5")
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForLabel(videoRow(Self.smith), "the video's row", contains: "Sent")
        XCTAssertEqual(
            app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "video-row-")).count, 1,
            "My Videos lists the new video only."
        )
    }

    /// DELETE VIDEO asks first. NO, KEEP IT keeps it; YES, DELETE (even
    /// tapped twice) deletes it once, and the stop is free to record again.
    func testDeleteVideoWithConfirmFreesTheStop() {
        launchAndSignIn()
        recordAndSave(for: Self.smith)
        waitForHeld()

        tap(stop(Self.smith), "the stop with a video")
        waitForBar("Video")
        tap(button("DELETE VIDEO"), "DELETE VIDEO")
        waitForTitle("Delete this video?")
        waitForText("The customer will not get a video for this visit.")
        tap(button("NO, KEEP IT"), "NO, KEEP IT")
        waitForBar("Video")
        XCTAssertEqual(theOnlyVideo().state, "held", "NO, KEEP IT changes nothing.")

        tap(button("DELETE VIDEO"), "DELETE VIDEO")
        waitForTitle("Delete this video?")
        doubleTap(button("YES, DELETE"), "YES, DELETE")
        waitForBar("My Route")
        waitForText("Video deleted.")

        waitUntil("the server marks the video deleted") { server.state().videos.first?.state == "deleted" }
        pause(1.5)
        let video = theOnlyVideo()
        XCTAssertEqual(video.deletes, 1, "A double tap must delete once.")
        XCTAssertEqual(video.state, "deleted")

        waitForLabel(stop(Self.smith), "the stop's card", contains: "Record Video")
        waitForLabel(element("route-header"), "the header", contains: "Sent 1 of 5")
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForText("No videos yet today.")
        waitForLabel(element("held-space"), "the space line", contains: "holding 0 MB")
    }

    /// Two quick taps on SAVE save one video.
    func testSaveCannotBeDoubleTapped() {
        launchAndSignIn()
        openRecorder(for: Self.smith)
        recordClip()

        doubleTap(button("SAVE"), "SAVE")
        waitFor(app.buttons["tab-route"], "My Route after SAVE", timeout: 30)
        answerFirstSaveQuestion()
        waitForHeld()
        pause(2)

        let seen = server.state()
        let video = theOnlyVideo()
        XCTAssertEqual(seen.registerCalls(), 1, "A double tap must register one video.")
        XCTAssertEqual(video.puts, ["200"])
        XCTAssertEqual(video.completes, [200])
        tap(app.buttons["tab-videos"], "the My Videos tab")
        XCTAssertEqual(
            app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "video-row-")).count, 1,
            "My Videos lists one video."
        )
    }

    /// STOP NOT ON MY LIST records with no customer; the office matches it.
    func testNotOnMyListRecordsWithoutCustomer() {
        launchAndSignIn()
        tap(app.buttons["not-on-my-list"], "STOP NOT ON MY LIST")
        waitForTitle("Not on my list")
        tap(button("RECORD"), "RECORD")
        waitFor(app.buttons["record-button"], "the red record button")
        recordClip()
        saveClip()
        waitForHeld()

        let video = theOnlyVideo()
        XCTAssertTrue(video.notOnMyList, "notOnMyList: true goes with the video.")
        XCTAssertNil(video.customerId, "No customer is sent for a stop that is not on the list.")
        XCTAssertFalse(video.override)
        XCTAssertEqual(video.completes, [200])

        // It does not count for any stop on the route.
        XCTAssertTrue(routeHeader.contains("Sent 1 of 5"), "The header says: \(routeHeader)")
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForLabel(app.buttons["video-row-offlist"], "the video's row", contains: "Sent")
    }

    /// A gloved thumb taps twice. The second tap must never answer the
    /// screen the first one brought up: not the confirm question, not the
    /// record button, not "Delete this video?".
    func testADoubleTapNeverAnswersTheNextScreen() {
        launchAndSignIn()

        // On the stop's card: the confirm question must still be asked.
        doubleTap(stop(Self.smith), "the first stop")
        waitForTitle("Is this the stop?")
        pause(1.2)
        XCTAssertEqual(screenTitle, "Is this the stop?", "A double tap on a stop must not also answer the confirm question.")
        XCTAssertFalse(app.buttons["record-button"].exists)

        // On THIS IS IT: the camera opens, but recording must not start.
        doubleTap(button("THIS IS IT"), "THIS IS IT")
        waitFor(app.buttons["record-button"], "the red record button")
        pause(1.5)
        XCTAssertFalse(element("recording-pill").exists, "A double tap on THIS IS IT must not start recording.")
        XCTAssertTrue(app.buttons["record-button"].exists)

        recordClip()
        saveClip()
        waitForHeld()

        // On DELETE VIDEO: the question comes up and stays up. YES, DELETE
        // sits exactly where DELETE VIDEO was.
        tap(stop(Self.smith), "the stop with a video")
        waitForBar("Video")
        doubleTap(button("DELETE VIDEO"), "DELETE VIDEO")
        waitForTitle("Delete this video?")
        pause(1.5)
        XCTAssertEqual(screenTitle, "Delete this video?", "A double tap on DELETE VIDEO must not also answer YES, DELETE.")
        XCTAssertEqual(theOnlyVideo().state, "held", "The video must still be there.")
        XCTAssertEqual(theOnlyVideo().deletes, 0)
        tap(button("NO, KEEP IT"), "NO, KEEP IT")
        waitForBar("Video")
    }

    /// With leave reminders on, the offer shows once, after the first saved
    /// video, and NOT NOW goes on to My Route. The video still sends.
    func testRemindersOfferShowsAfterTheFirstSave() {
        server.set(["leaveReminders": true])
        launchAndSignIn()
        openRecorder(for: Self.smith)
        recordClip()

        tap(button("SAVE"), "SAVE")
        waitForTitle("Want a reminder?", timeout: 30)
        XCTAssertTrue(button("TURN ON REMINDERS").exists)
        tap(button("NOT NOW"), "NOT NOW")
        waitFor(app.buttons["tab-route"], "My Route after NOT NOW", timeout: 20)
        answerFirstSaveQuestion()
        waitForHeld()
        XCTAssertEqual(theOnlyVideo().completes, [200])
    }
}
