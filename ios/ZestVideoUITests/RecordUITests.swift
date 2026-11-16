import XCTest

/// Recording, reviewing and saving a video, and changing or deleting it
/// inside its ten minutes. Every test checks the screen and the server.
final class RecordUITests: ZestUITestCase {
    /// Tap the stop, THIS IS IT, record, SAVE. The server gets exactly one
    /// register, the whole file once, and exactly one "complete". The phone
    /// then says SENT and holds no file.
    func testHappyPathRecordSaveSend() {
        launchSignedIn()
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
        launchSignedIn()
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
        launchSignedIn()
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

    /// The words name a different customer ("Hi Jane" at Robert Johnson's):
    /// one warning. SAVE ANYWAY sends the video as it is, marked as an override.
    func testTwoKeysWarningSaveAnywaySendsOverride() {
        launchSignedIn(transcript: "Hi Jane. Here's what I did.")
        openRecorder(for: Self.johnson)
        recordClip()

        tap(button("SAVE"), "SAVE")
        waitForTitle("Is this the right stop?")
        waitForText("I did not hear this customer's name.")
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
        launchSignedIn(transcript: "Hi Jane. Here's what I did.")
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
        launchSignedIn()
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
        launchSignedIn()
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
        launchSignedIn()
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

    /// STOP NOT ON MY LIST records with no customer. The video goes to the
    /// office to be matched, and the app says so: "SENT TO OFFICE", never
    /// "sent to the customer", and no countdown to an email (the server
    /// sends no email time for it). The ten minutes to change or delete it
    /// still run, counted from when it was filmed.
    func testNotOnMyListRecordsWithoutCustomer() {
        launchSignedIn()
        tap(app.buttons["not-on-my-list"], "STOP NOT ON MY LIST")
        waitForTitle("Not on my list")
        tap(button("RECORD"), "RECORD")
        waitFor(app.buttons["record-button"], "the red record button")
        recordClip()
        saveClip()
        waitUntil("the server holds the whole file, waiting for the office to match it", timeout: 60) {
            server.state().videos.first?.isWithTheOffice ?? false
        }

        let video = theOnlyVideo()
        XCTAssertTrue(video.notOnMyList, "notOnMyList: true goes with the video.")
        XCTAssertNil(video.customerId, "No customer is sent for a stop that is not on the list.")
        XCTAssertFalse(video.override)
        XCTAssertEqual(video.completes, [200])
        XCTAssertEqual(video.state, "unmatched", "The server keeps it for the office to match.")

        // It does not count for any stop on the route.
        XCTAssertTrue(routeHeader.contains("Sent 1 of 5"), "The header says: \(routeHeader)")
        tap(app.buttons["tab-videos"], "the My Videos tab")
        let row = app.buttons["video-row-offlist"]
        waitForLabel(row, "the video's row", contains: "Sent To Office")
        XCTAssertTrue(label(of: row).contains("Min Left"), "Inside its ten minutes it can still be changed. The row says: \(label(of: row))")
        waitForLabel(element("held-space"), "the space line", contains: "holding 0 MB")
        XCTAssertFalse(element("keep-app-warning").exists, "The server has it: nothing is left on the phone.")

        // Its screen, inside the ten minutes: where it goes, in true words.
        tap(row, "the video's row")
        waitForBar("Video")
        waitForText("Then the office matches it to the customer.")
        XCTAssertTrue(hasText("Sent To Office") || hasText("SENT TO OFFICE"))
        XCTAssertFalse(hasText("goes to the customer"), "No email is on its way: the screen must not say it goes to the customer.")
        XCTAssertFalse(hasText("email"))
        XCTAssertTrue(button("RECORD AGAIN").exists)
        XCTAssertTrue(button("DELETE VIDEO").exists, "The ten minutes to delete it run from when it was filmed.")
        tap(button("BACK"), "BACK")
        waitForBar("My Route")

        // Once the time to change it has passed (the office sets how long;
        // here it is set to nothing): it is the office's, and says so.
        server.set(["holdMinutes": 0])
        leaveAndComeBack()
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitUntil("the row no longer offers to change the video", timeout: 40) {
            let words = self.label(of: row)
            return words.contains("Sent To Office") && !words.contains("Min Left") && !words.contains("Change Video")
        }
        tap(row, "the video's row")
        waitForBar("Video")
        waitForText("This video went to the office.")
        waitForText("They will match it to the customer.")
        XCTAssertFalse(hasText("already went to the customer"))
        XCTAssertFalse(button("DELETE VIDEO").exists, "After the ten minutes it cannot be deleted from the phone.")
        XCTAssertFalse(button("RECORD AGAIN").exists)
        XCTAssertEqual(theOnlyVideo().state, "unmatched", "Looking changes nothing.")
        tap(button("BACK"), "BACK")
        waitForBar("My Route")

        // Back inside the time to change it: DELETE VIDEO really deletes it.
        // (An hour, so a slow run cannot let it lapse before the tap.)
        server.set(["holdMinutes": 60])
        leaveAndComeBack()
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForLabel(row, "the video's row", contains: "Min Left", timeout: 40)
        tap(row, "the video's row")
        waitForBar("Video")
        tap(button("DELETE VIDEO"), "DELETE VIDEO")
        waitForTitle("Delete this video?")
        tap(button("YES, DELETE"), "YES, DELETE")
        waitForBar("My Route")
        waitUntil("the server marks the video deleted") { server.state().videos.first?.state == "deleted" }
        let deleted = theOnlyVideo()
        XCTAssertEqual(deleted.deletes, 1)
        XCTAssertEqual(server.state().count("DELETE", "/v1/videos/", status: 409), 0, "The delete was not refused.")
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForText("No videos yet today.")
    }

    /// He says ANOTHER customer's name ("Hi Robert") at Jane Smith's, where
    /// everything else fits: the phone is on her lawn and he checked out a
    /// minute ago. The place and the time agree, and still the video does
    /// not go without asking: this is the wrong house next door. The
    /// warning names the customer it sounded like, PICK THE RIGHT STOP moves
    /// the clip there, and the audit trail records which kind of mismatch
    /// it was.
    func testAnotherCustomersNameAlwaysAsksEvenWhenPlaceAndTimeFit() {
        launchSignedIn(transcript: "Hi Robert. Here's what I did. Here's what I recommend. I closed the backyard gate.")
        openRecorder(for: Self.smith)
        recordClip()

        tap(button("SAVE"), "SAVE")
        waitForTitle("Is this the right stop?")
        waitForText("I did not hear this customer's name.")
        waitForText("It sounded like Johnson, Robert.")
        XCTAssertTrue(hasText("Smith, Jane"), "The warning shows the stop that was picked.")
        XCTAssertTrue(server.state().videos.isEmpty, "Nothing is sent while the warning is up.")

        tap(button("PICK THE RIGHT STOP"), "PICK THE RIGHT STOP")
        waitForTitle("Pick the right stop")
        waitForText("It sounded like this one")
        tap(stop(Self.johnson), "the stop it sounded like")
        waitForTitle("Save this video?")
        XCTAssertTrue(hasText("Johnson, Robert"), "The clip now belongs to the stop that was picked.")
        saveClip()
        waitForHeld()

        let video = theOnlyVideo()
        XCTAssertEqual(video.customerId, Self.johnson, "The video went to the customer whose name was said.")
        XCTAssertFalse(video.override, "With the right stop picked there was nothing to get past.")
        XCTAssertEqual(video.signals["spoken"], "match")
        XCTAssertEqual(video.signals["gps"], "match")
        waitForLabel(stop(Self.smith), "the stop first picked by mistake", contains: "Record Video")

        // The audit trail: asked about at Jane's, because another stop was named.
        leaveAndComeBack()
        waitUntil("the server holds the two-keys warning", timeout: 60) {
            self.eventTypes(for: Self.smith).contains("two_keys_warning")
        }
        let warning = server.state().events.first {
            ($0["type"] as? String) == "two_keys_warning" && ($0["customerId"] as? Int) == Self.smith
        }
        let detail = warning?["detail"] as? [String: Any]
        XCTAssertEqual(detail?["spoken"] as? String, "mismatch", "The warning's detail: \(String(describing: detail))")
        XCTAssertEqual(detail?["spokenMiss"] as? String, "other_stop", "It says another stop was named.")
        XCTAssertEqual(detail?["soundedLike"] as? String, String(Self.johnson), "And which one.")
        XCTAssertEqual(detail?["gps"] as? String, "match", "The place fitted: that is why this case has its own rule.")
        XCTAssertEqual(detail?["checkIn"] as? String, "match", "So did the time.")
        XCTAssertFalse(server.state().events.contains { ($0["type"] as? String) == "override" }, "Nothing was saved past the warning.")
    }

    /// He forgets to say a name. At the right house, on the pin, inside his
    /// check-in, he is not nagged: the place and the time agree and nothing
    /// contradicts them. At a house where he has not checked in, only the
    /// place agrees: he is asked, with no other customer to suggest, and
    /// SAVE ANYWAY is logged as "no name", not as "another stop".
    func testAForgottenNameAtTheRightHouseIsNotAskedAbout() {
        launchSignedIn(transcript: "Here's what I did. Here's what I recommend. I closed the backyard gate.")
        recordAndSave(for: Self.smith)     // SAVE goes straight through: no warning screen
        waitForHeld()
        let first = theOnlyVideo()
        XCTAssertEqual(first.customerId, Self.smith)
        XCTAssertFalse(first.override, "Nothing was overridden: he was not asked.")
        XCTAssertEqual(first.signals["spoken"], "mismatch", "No name was heard.")
        XCTAssertEqual(first.signals["gps"], "match")
        XCTAssertEqual(first.signals["checkIn"], "match")

        // Next door, not checked in: one key only.
        openRecorder(for: Self.johnson)
        recordClip()
        tap(button("SAVE"), "SAVE")
        waitForTitle("Is this the right stop?")
        waitForText("I did not hear this customer's name.")
        XCTAssertFalse(hasText("It sounded like"), "No other customer was named, so none is suggested.")
        tap(button("SAVE ANYWAY"), "SAVE ANYWAY")
        waitFor(app.buttons["tab-route"], "My Route after SAVE ANYWAY", timeout: 30)
        answerFirstSaveQuestion()
        waitForHeld(count: 2)
        let second = server.state().videos.last ?? ServerVideo(raw: [:])
        XCTAssertEqual(second.customerId, Self.johnson)
        XCTAssertTrue(second.override)

        leaveAndComeBack()
        waitUntil("the server holds the override", timeout: 60) { self.eventTypes(for: Self.johnson).contains("override") }
        let override = server.state().events.first { ($0["type"] as? String) == "override" }
        let detail = override?["detail"] as? [String: Any]
        XCTAssertEqual(detail?["spokenMiss"] as? String, "no_name", "The override says no name was heard: \(String(describing: detail))")
        XCTAssertNil(detail?["soundedLike"], "And names no other customer.")
        XCTAssertFalse(
            server.state().events.contains { ($0["type"] as? String) == "two_keys_warning" && ($0["customerId"] as? Int) == Self.smith },
            "At the right house there was no warning at all."
        )
    }

    /// The audit trail really reaches the server (`POST /v1/events`): after
    /// a recording, a retake and a video saved past a warning, the server
    /// holds each of those events, for the right customer.
    func testAuditEventsReachTheServer() {
        // "Hi Jane" said at Robert Johnson's: the spoken key does not match.
        launchSignedIn(transcript: "Hi Jane. Here's what I did.")
        openRecorder(for: Self.johnson)
        recordClip()
        tap(button("RECORD AGAIN"), "RECORD AGAIN")
        waitFor(app.buttons["record-button"], "the record screen again")
        recordClip()
        tap(button("SAVE"), "SAVE")
        waitForTitle("Is this the right stop?")
        tap(button("SAVE ANYWAY"), "SAVE ANYWAY")
        waitFor(app.buttons["tab-route"], "My Route after SAVE ANYWAY", timeout: 30)
        answerFirstSaveQuestion()
        waitForHeld()
        let video = theOnlyVideo()
        XCTAssertTrue(video.override)

        // Events go when a video moves, and whenever the app is opened.
        let wanted: Set<String> = ["record_start", "record_stop", "retake", "two_keys_warning", "override"]
        leaveAndComeBack()
        waitUntil("the server holds the record, retake and override events", timeout: 60) {
            wanted.isSubset(of: Set(self.eventTypes(for: Self.johnson)))
        }

        let seen = server.state()
        XCTAssertGreaterThanOrEqual(seen.calls("POST", exactly: "/v1/events", status: 200), 1, "The app posted its events.")
        let types = eventTypes(for: Self.johnson)
        func count(_ type: String) -> Int { types.filter { $0 == type }.count }
        XCTAssertEqual(count("record_start"), 2, "Two clips were recorded. Events: \(types)")
        XCTAssertEqual(count("record_stop"), 2, "Both stopped. Events: \(types)")
        XCTAssertEqual(count("retake"), 1, "RECORD AGAIN was tapped once. Events: \(types)")
        XCTAssertEqual(count("override"), 1, "SAVE ANYWAY was tapped once. Events: \(types)")
        XCTAssertGreaterThanOrEqual(count("two_keys_warning"), 1, "The warning was shown. Events: \(types)")

        // Each of them names the stop that was picked, and nobody else's.
        for event in seen.events where wanted.contains((event["type"] as? String) ?? "") {
            XCTAssertEqual(event["customerId"] as? Int, Self.johnson, "\(event["type"] ?? "?") is for the stop that was picked.")
            XCTAssertNotNil(event["at"], "Every event says when.")
        }
        let override = seen.events.first { ($0["type"] as? String) == "override" }
        XCTAssertEqual(override?["videoId"] as? String, video.id, "The override names the video that was saved.")
        // And which kind of mismatch was saved past: he said "Hi Jane" at
        // Robert Johnson's, so another stop was named, and which.
        let detail = override?["detail"] as? [String: Any]
        XCTAssertEqual(detail?["spokenMiss"] as? String, "other_stop", "The override's detail: \(String(describing: detail))")
        XCTAssertEqual(detail?["soundedLike"] as? String, String(Self.smith))
        let retake = seen.events.first { ($0["type"] as? String) == "retake" }
        XCTAssertNotEqual(retake?["videoId"] as? String, video.id, "The retake names the clip that was thrown away, not the one kept.")
    }

    /// A gloved thumb taps twice. The second tap must never answer the
    /// screen the first one brought up: not the confirm question, not the
    /// record button, not "Delete this video?".
    func testADoubleTapNeverAnswersTheNextScreen() {
        launchSignedIn()

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
        launchSignedIn()
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
