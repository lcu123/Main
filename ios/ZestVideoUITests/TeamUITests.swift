import XCTest

/// One video per tech, per property, per day.
///
/// When two techs visit the same property on the same day, each sends his
/// own video and the customer gets both. A second video from the same tech
/// replaces his own first one, and never another tech's.
///
/// The other tech here ("Pat") never signs in: the fake server is told that
/// he already recorded a stop (`otherTechRecorded`). Every name is made up.
final class TeamUITests: ZestUITestCase {
    /// The tech's script, with the first name of the customer at stop 202.
    static let robertWords = "Hi Robert. Here's what I did. Here's what I recommend. I closed the backyard gate."

    private var videoRows: XCUIElementQuery {
        app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "video-row-"))
    }

    /// True when the app asked for the day again after its last DELETE:
    /// that answer is what tells it how the stop stands now.
    private func askedForTheDayAfterTheDelete() -> Bool {
        let requests = server.state().requests
        guard let deleted = requests.lastIndex(where: { ($0["method"] as? String) == "DELETE" }) else { return false }
        return requests[(deleted + 1)...].contains { entry in
            (entry["method"] as? String) == "GET" && ((entry["path"] as? String) ?? "").contains("/v1/today")
        }
    }

    /// The lead: he has his own appointment at a stop another tech already
    /// recorded. He is not shown that video, his own is taken with no fuss,
    /// and recording again replaces his own first video and nothing else.
    func testLeadRecordsBesideAnotherTechAndOnlyHisOwnIsEverReplaced() {
        XCTAssertNotNil(
            server.otherTechRecorded(customerId: Self.johnson, state: "sent", helper: false),
            "The fake server did not take the other tech's video."
        )
        // An hour to change a video, so a slow run cannot let it lapse
        // between the steps below.
        server.set(["holdMinutes": 60])
        launchSignedIn(transcript: Self.robertWords)

        // Nothing of the other tech's shows: the stop is his to record.
        waitForLabel(stop(Self.johnson), "the stop's card", contains: "Record Video")
        XCTAssertFalse(label(of: stop(Self.johnson)).contains("Team"), "The lead is not told about the other tech's video.")
        waitForLabel(element("route-header"), "the header", contains: "Sent 1 of 5")

        recordAndSave(for: Self.johnson)
        waitForHeld()
        var seen = server.state()
        XCTAssertEqual(seen.registerCalls(status: 409), 0, "His own video must not be refused because another tech recorded the stop.")
        XCTAssertEqual(seen.videos.count, 1)
        XCTAssertEqual(seen.videos.first?.customerId, Self.johnson)
        XCTAssertFalse(seen.videos.first?.override ?? true, "A clean save: nothing was overridden.")
        XCTAssertEqual(seen.otherTechVideos.map { $0.state }, ["sent"], "The other tech's video is kept: the customer gets both.")

        // His own video, inside its ten minutes: RECORD AGAIN replaces it.
        waitForLabel(stop(Self.johnson), "the stop's card", contains: "Change Video")
        tap(stop(Self.johnson), "the stop with his own video")
        waitForBar("Video")
        waitForText("You can change it for")
        XCTAssertFalse(hasText("Another tech"), "This is his own video, and the screen is about his own.")
        tap(button("RECORD AGAIN"), "RECORD AGAIN")
        waitFor(app.buttons["record-button"], "the record screen")
        recordClip()
        saveClip()

        waitUntil("his second video is confirmed and his first is gone", timeout: 60) {
            let videos = server.state().videos
            return videos.count == 2 && videos[1].isHeld && videos[0].state != "held"
        }
        seen = server.state()
        XCTAssertTrue(["replaced", "deleted"].contains(seen.videos[0].state), "His first video no longer counts: \(seen.videos[0].summary)")
        XCTAssertTrue(seen.videos[1].isHeld, "His second video is the one kept: \(seen.videos[1].summary)")
        XCTAssertEqual(seen.otherTechVideos.map { $0.state }, ["sent"], "Recording again never replaces another tech's video.")
        XCTAssertEqual(seen.otherTechVideos.map { $0.deletes }, [0], "Nor deletes it.")
        XCTAssertEqual(seen.registerCalls(status: 409), 0)

        waitForLabel(element("route-header"), "the header", contains: "Sent 2 of 5")
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForLabel(videoRow(Self.johnson), "his video's row", contains: "Sent")
        XCTAssertEqual(videoRows.count, 1, "My Videos lists his own newest video and nothing of the other tech's.")
        tap(app.buttons["tab-route"], "the My Route tab")
        waitForBar("My Route")

        // He deletes his own. For him the stop simply reopens: he was never
        // shown the other tech's video, and is not shown it now.
        tap(stop(Self.johnson), "the stop with his own video")
        waitForBar("Video")
        tap(button("DELETE VIDEO"), "DELETE VIDEO")
        waitForTitle("Delete this video?")
        tap(button("YES, DELETE"), "YES, DELETE")
        waitForBar("My Route")
        waitUntil("the server marks his second video deleted") { self.server.state().videos.last?.state == "deleted" }
        waitUntil("the app asks for the day again after the delete is confirmed", timeout: 30) {
            self.askedForTheDayAfterTheDelete()
        }
        waitForLabel(stop(Self.johnson), "the stop's card", contains: "Record Video")
        XCTAssertFalse(label(of: stop(Self.johnson)).contains("Team"))
        XCTAssertEqual(server.state().otherTechVideos.map { $0.state }, ["sent"], "Deleting his own leaves the other tech's alone.")
        waitForLabel(element("route-header"), "the header", contains: "Sent 1 of 5")
    }

    /// The helper: he has no appointment of his own at a stop the lead
    /// already recorded. He is told so, the stop reads as covered, nothing
    /// lets him change or delete the lead's video, and he can still record
    /// his own, which is kept beside it.
    func testHelperSeesTheTeamsVideoAndCanStillRecordHisOwn() {
        // One that has gone to the customer, one still inside its ten minutes.
        XCTAssertNotNil(server.otherTechRecorded(customerId: Self.johnson, state: "sent", helper: true))
        XCTAssertNotNil(server.otherTechRecorded(customerId: Self.business, state: "held", helper: true))
        server.set(["holdMinutes": 60])     // see the other test
        launchSignedIn(transcript: Self.robertWords)

        // Both read as covered, whatever became of the lead's video.
        waitForLabel(stop(Self.johnson), "the stop the lead recorded", contains: "Done By Team")
        waitForLabel(stop(Self.business), "the other stop the lead recorded", contains: "Done By Team")
        XCTAssertFalse(label(of: stop(Self.johnson)).contains("Record Again"), "Another tech's video is not his to record again.")
        // The stop he is standing on still needs his video.
        XCTAssertTrue(label(of: stop(Self.smith)).contains("Record Video"))
        waitForLabel(element("route-header"), "the header", contains: "Sent 3 of 5")

        // Another tech's video is never one of "my videos".
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForBar("My Videos")
        waitForText("No videos yet today.")
        XCTAssertEqual(videoRows.count, 0, "My Videos must not list another tech's video.")
        tap(app.buttons["tab-route"], "the My Route tab")
        waitForBar("My Route")

        // The stop's screen says what happened and what he can do.
        tap(stop(Self.johnson), "the stop the lead recorded")
        waitForBar("Video")
        waitForText("Another tech already recorded this stop.")
        waitForText("You can record your own too. The customer gets both.")
        XCTAssertTrue(hasText("Johnson, Robert"))
        XCTAssertFalse(hasText("takes its place"), "Nothing of the lead's is replaced, and the screen must not say so.")
        XCTAssertFalse(hasText("already went to the customer"), "A sent video of the lead's must not lock the helper out.")
        XCTAssertFalse(button("RECORD AGAIN").exists, "RECORD AGAIN would mean replacing: not offered on another tech's video.")
        XCTAssertFalse(button("DELETE VIDEO").exists, "Another tech's video is not his to delete.")
        XCTAssertFalse(button("CALL OFFICE").exists, "There is nothing to call the office about.")
        XCTAssertTrue(button("RECORD VIDEO").exists)

        // Both ways back work, and nothing was sent or changed by looking.
        tap(button("BACK"), "BACK")
        waitForBar("My Route")
        XCTAssertTrue(server.state().videos.isEmpty)

        // RECORD VIDEO is a plain new recording, by the usual steps.
        tap(stop(Self.johnson), "the stop the lead recorded, again")
        waitForBar("Video")
        tap(button("RECORD VIDEO"), "RECORD VIDEO")
        waitForTitle("Is this the stop?")
        XCTAssertTrue(hasText("Johnson, Robert"))
        tap(button("THIS IS IT"), "THIS IS IT")
        waitFor(app.buttons["record-button"], "the red record button")
        recordClip()
        saveClip()
        waitForHeld()

        var seen = server.state()
        let mine = theOnlyVideo()
        XCTAssertEqual(mine.customerId, Self.johnson)
        XCTAssertEqual(mine.completes, [200])
        XCTAssertFalse(mine.override, "A clean save: nothing was overridden.")
        XCTAssertEqual(seen.registerCalls(), 1)
        XCTAssertEqual(seen.registerCalls(status: 409), 0, "A sent video of the lead's must not refuse the helper's own.")
        XCTAssertEqual(seen.otherTechVideos.map { $0.state }, ["sent", "held"], "Both of the lead's videos are kept: the customer gets both.")
        XCTAssertEqual(seen.otherTechVideos.map { $0.deletes }, [0, 0])

        // Now the stop shows his own video, which is his to change.
        waitForLabel(stop(Self.johnson), "the stop's card", contains: "Change Video")
        waitForLabel(stop(Self.business), "the stop he did not record", contains: "Done By Team")
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitFor(videoRow(Self.johnson), "his own video's row")
        XCTAssertEqual(videoRows.count, 1, "My Videos lists his own video only.")
        tap(app.buttons["tab-route"], "the My Route tab")
        waitForBar("My Route")

        tap(stop(Self.johnson), "the stop with his own video")
        waitForBar("Video")
        waitForText("You can change it for")
        XCTAssertFalse(hasText("Another tech"))
        XCTAssertTrue(button("RECORD AGAIN").exists)

        // Deleting his own leaves the lead's where it was.
        tap(button("DELETE VIDEO"), "DELETE VIDEO")
        waitForTitle("Delete this video?")
        tap(button("YES, DELETE"), "YES, DELETE")
        waitForBar("My Route")
        waitUntil("the server marks his own video deleted") { server.state().videos.first?.state == "deleted" }
        // The day is asked for again once the server has confirmed the
        // delete: that answer is what says the lead's video covers the stop.
        waitUntil("the app asks for the day again after the delete is confirmed", timeout: 30) {
            self.askedForTheDayAfterTheDelete()
        }
        seen = server.state()
        XCTAssertEqual(seen.videos.count, 1)
        XCTAssertEqual(seen.videos.first?.deletes, 1)
        XCTAssertEqual(seen.otherTechVideos.map { $0.state }, ["sent", "held"], "Deleting his own must not touch the lead's.")
        XCTAssertEqual(seen.otherTechVideos.map { $0.deletes }, [0, 0])
        waitForLabel(stop(Self.johnson), "the stop's card", contains: "Done By Team")
    }
}
