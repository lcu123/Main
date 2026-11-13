import XCTest

/// "The draft stays." A clip the tech recorded and has not saved yet is one
/// thing at a time until he deals with it, and it is never thrown away
/// because the day changed.
///
/// The day-change tests make the unsaved clip older at the next launch
/// (`ageDraftDays`), as if the phone had been put away with it. What the app
/// then does with an old clip is its real code.
final class DraftUITests: ZestUITestCase {
    private var videoRows: XCUIElementQuery {
        app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "video-row-"))
    }

    /// Records a clip at stop 201 and leaves it unsaved, with the app closed.
    private func leaveAnUnsavedClip(file: StaticString = #filePath, line: UInt = #line) {
        launchSignedIn()
        openRecorder(for: Self.smith, file: file, line: line)
        recordClip(file: file, line: line)
        waitForTitle("Save this video?", file: file, line: line)
        kill()
        XCTAssertTrue(server.state().videos.isEmpty, "Nothing is sent until the tech says SAVE.", file: file, line: line)
    }

    /// With an unsaved clip, tapping a different stop (or STOP NOT ON MY
    /// LIST) opens "Save this video?" for the clip's own customer. It is
    /// dealt with first, and saved for the stop it was recorded at.
    func testAnUnsavedClipIsDealtWithBeforeAnotherStop() {
        leaveAnUnsavedClip()
        relaunch()
        waitFor(app.buttons["draft-card"], "the \"1 video not saved yet\" card", timeout: 60)
        waitForLabel(stop(Self.smith), "the clip's own stop", contains: "Not Saved Yet")

        tap(stop(Self.johnson), "a different stop")
        waitForTitle("Save this video?")
        XCTAssertTrue(hasText("Smith, Jane"), "The question is about the clip's own customer.")
        XCTAssertFalse(hasText("Johnson, Robert"), "Not about the stop that was tapped.")
        XCTAssertFalse(app.buttons["record-button"].exists, "The camera does not open for another stop while a clip is unsaved.")
        XCTAssertTrue(button("RECORD AGAIN").exists, "A clip filmed today can still be recorded again.")
        XCTAssertFalse(button("DELETE VIDEO").exists)

        // The same from STOP NOT ON MY LIST and from a stop with a video.
        kill()
        relaunch()
        waitFor(app.buttons["draft-card"], "the \"1 video not saved yet\" card", timeout: 60)
        tap(app.buttons["not-on-my-list"], "STOP NOT ON MY LIST")
        waitForTitle("Save this video?")
        XCTAssertTrue(hasText("Smith, Jane"))
        kill()
        relaunch()
        waitFor(app.buttons["draft-card"], "the \"1 video not saved yet\" card", timeout: 60)
        tap(stop(Self.locked), "a stop whose video already went out")
        waitForTitle("Save this video?")
        XCTAssertTrue(hasText("Smith, Jane"))

        saveClip()
        waitForHeld()
        let video = theOnlyVideo()
        XCTAssertEqual(video.customerId, Self.smith, "The clip went to the stop it was recorded at.")
        XCTAssertFalse(app.buttons["draft-card"].exists)
        waitForLabel(stop(Self.johnson), "the other stop", contains: "Record Video")
    }

    /// The day changes with a clip unsaved. On the next open the tech gets
    /// the one question for it, with the customer and the day it was filmed:
    /// SAVE or DELETE VIDEO (RECORD AGAIN makes no sense a day later). SAVE
    /// sends it for the day it was filmed, and today's stop for the same
    /// customer is still open.
    func testYesterdaysUnsavedClipIsStillThereAndCanBeSaved() {
        leaveAnUnsavedClip()
        ageDraftDays = 1
        relaunch()

        // Straight to the question: not a card to go and find.
        waitForTitle("Save this video?", timeout: 60)
        XCTAssertTrue(hasText("Smith, Jane"), "It says whose video it is.")
        let filmed = label(of: element("draft-filmed"))
        XCTAssertTrue(filmed.hasPrefix("Filmed "), "It says which day it was filmed. It says: \"\(filmed)\"")
        XCTAssertTrue(button("DELETE VIDEO").exists)
        XCTAssertFalse(button("RECORD AGAIN").exists, "A clip from another day cannot be recorded again.")
        XCTAssertTrue(server.state().videos.isEmpty)

        // DELETE VIDEO asks first, and NO, KEEP IT keeps it.
        tap(button("DELETE VIDEO"), "DELETE VIDEO")
        waitForTitle("Delete this video?")
        tap(button("NO, KEEP IT"), "NO, KEEP IT")
        waitForTitle("Save this video?")

        saveClip()
        waitForHeld()
        let video = theOnlyVideo()
        XCTAssertEqual(video.customerId, Self.smith)
        XCTAssertEqual(video.completes, [200])
        let yesterday = Self.day(daysAgo: 1)
        XCTAssertEqual(video.stopDate, yesterday, "It is sent for the day it was filmed.")
        XCTAssertFalse(video.override, "The words and the place still matched the stop as it was that day.")
        XCTAssertEqual(video.signals["spoken"], "match")

        // Yesterday's video is not today's video for the same customer.
        XCTAssertFalse(app.buttons["draft-card"].exists)
        waitForLabel(stop(Self.smith), "today's stop for the same customer", contains: "Record Video")
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForLabel(videoRow(Self.smith), "the video's row", contains: "Sent")
        XCTAssertTrue(label(of: videoRow(Self.smith)).contains("Filmed "), "My Videos says which day it was filmed. It says: \(label(of: videoRow(Self.smith)))")
        XCTAssertEqual(videoRows.count, 1)
    }

    /// The same clip, a week old, deleted instead: YES, DELETE removes it
    /// from the phone, nothing is sent, and the server is told it was
    /// thrown away (`draft_discarded`).
    func testAnOldUnsavedClipCanBeDeleted() {
        leaveAnUnsavedClip()
        ageDraftDays = 7
        relaunch()

        waitForTitle("Save this video?", timeout: 60)
        XCTAssertTrue(hasText("Smith, Jane"))
        tap(button("DELETE VIDEO"), "DELETE VIDEO")
        waitForTitle("Delete this video?")
        waitForText("The customer will not get a video for this visit.")
        doubleTap(button("YES, DELETE"), "YES, DELETE")
        waitForBar("My Route")
        waitForText("Video deleted.")
        XCTAssertFalse(app.buttons["draft-card"].exists)
        waitForLabel(stop(Self.smith), "the stop's card", contains: "Record Video")

        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForText("No videos yet today.")
        waitForLabel(element("held-space"), "the space line", contains: "holding 0 MB")
        XCTAssertFalse(element("keep-app-warning").exists)
        XCTAssertTrue(server.state().videos.isEmpty, "Nothing was sent.")

        // It does not come back, and the server hears it was thrown away.
        kill()
        relaunch()
        waitForRoute()
        XCTAssertFalse(app.buttons["draft-card"].exists)
        waitUntil("the server is told the clip was thrown away", timeout: 40) {
            self.eventTypes(for: Self.smith).contains("draft_discarded")
        }
        XCTAssertEqual(eventTypes(for: Self.smith).filter { $0 == "draft_discarded" }.count, 1, "Told once, even after a double tap.")
    }

    /// Only a clip too old for the server to take (more than 14 days) is
    /// dropped by itself. Nothing is asked, the file goes, and it is logged.
    /// One day younger and it is still there to save.
    func testOnlyAClipOlderThanFourteenDaysIsDroppedByItself() {
        leaveAnUnsavedClip()
        ageDraftDays = 14
        relaunch()
        waitForTitle("Save this video?", timeout: 60)
        XCTAssertTrue(label(of: element("draft-filmed")).hasPrefix("Filmed "))
        XCTAssertTrue(button("DELETE VIDEO").exists, "Fourteen days old: still the tech's to save or delete.")
        XCTAssertFalse(eventTypes(for: Self.smith).contains("draft_discarded"))

        // One more day and the server would refuse it.
        kill()
        ageDraftDays = 1
        relaunch()
        waitForRoute()
        pause(2)
        XCTAssertEqual(barTitle, "My Route", "Nothing is asked about a clip that can no longer be sent.")
        XCTAssertFalse(app.buttons["draft-card"].exists)
        waitForLabel(stop(Self.smith), "the stop's card", contains: "Record Video")
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForLabel(element("held-space"), "the space line", contains: "holding 0 MB")
        XCTAssertTrue(server.state().videos.isEmpty)

        waitUntil("the server is told the clip was dropped", timeout: 40) {
            self.eventTypes(for: Self.smith).contains("draft_discarded")
        }
        let dropped = server.state().events.first { ($0["type"] as? String) == "draft_discarded" }
        let detail = dropped?["detail"] as? [String: Any]
        XCTAssertEqual(detail?["reason"] as? String, "too_old", "The log says why: \(String(describing: dropped))")
        XCTAssertEqual(detail?["daysOld"] as? String, "15")
    }

    /// `yyyy-MM-dd` for a day before today, the way the fake server counts.
    static func day(daysAgo: Int) -> String {
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd"
        let today = formatter.date(from: formatter.string(from: Date())) ?? Date()
        return formatter.string(from: today.addingTimeInterval(TimeInterval(-daysAgo * 86_400) + 43_200))
    }
}
