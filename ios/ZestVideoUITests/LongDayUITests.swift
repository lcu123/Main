import XCTest

/// The brief, phase 2: "Done when 20 videos in a row arrive after
/// airplane-mode tests, with none lost."
///
/// A long day with no signal: twenty stops, twenty videos saved with the
/// server out of reach, then the signal comes back. All twenty arrive
/// exactly once, in the order they were recorded; the phone ends holding no
/// files; and My Videos tells the truth the whole way (WAITING until the
/// server really has each one, never SENT before).
///
/// The same day is also played through without a phone in ZestCore
/// (`testTwentyVideosSavedWithNoSignalAllArriveOnceInOrder`).
final class LongDayUITests: ZestUITestCase {
    static let count = 20

    private var videoRows: XCUIElementQuery {
        app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "video-row-"))
    }

    func testTwentyVideosSavedWithNoSignalAllArriveOnceInOrder() throws {
        // The route is wiped when the day changes: twenty stops take a while.
        try skipIfMidnightIsNear(within: 20)
        executionTimeAllowance = 900
        let count = Self.count
        // Twenty houses in a row on Maple Row, customers 301 to 320.
        resetServer(route: "long")
        let customers = Array(301..<(301 + count))
        // One set of words for twenty customers, so no first name: the street,
        // a silent backup, is what the spoken key goes by here.
        launchSignedIn(transcript: "this is the stop on Maple Row", firstStop: customers[0])
        XCTAssertEqual(routeHeader, "Sam. Sent 0 of \(count). Waiting 0.")

        server.set(["down": true])
        for (index, customer) in customers.enumerated() {
            recordAndSave(for: customer)
            let header = routeHeader
            XCTAssertTrue(
                header.hasSuffix("Sent 0 of \(count). Waiting \(index + 1)."),
                "After video \(index + 1) with no signal the header must count it as waiting, not sent. It says: \(header)"
            )
        }
        XCTAssertTrue(server.state().videos.isEmpty, "The server heard of nothing while it was out of reach.")
        let banner = label(of: app.buttons["waiting-banner"])
        XCTAssertTrue(banner.contains("\(count) videos waiting to send"), "The waiting line counts all of them. It says: \(banner)")
        XCTAssertTrue(banner.contains("Do not delete this app"), "And warns not to delete the app. It says: \(banner)")

        // My Videos: twenty rows, every one WAITING, none SENT.
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForBar("My Videos")
        XCTAssertEqual(videoRows.count, count, "My Videos lists all \(count).")
        waitForLabel(
            element("keep-app-warning"), "the warning at the top of My Videos",
            contains: "Do not delete this app. \(count) videos are still on this phone.", timeout: 10
        )
        for customer in customers {
            let row = label(of: videoRow(customer))
            XCTAssertTrue(row.contains("Waiting"), "The row for \(customer) says WAITING. It says: \(row)")
            XCTAssertFalse(row.contains("Sent"), "A video the server does not have must never say SENT. It says: \(row)")
        }
        let held = label(of: element("held-space"))
        XCTAssertTrue(held.contains("holding"), "My Videos says what the phone is holding. It says: \(held)")
        XCTAssertFalse(held.contains("holding 0 MB"), "Twenty videos are on the phone: it cannot be holding nothing. It says: \(held)")
        tap(app.buttons["tab-route"], "the My Route tab")
        waitForBar("My Route")

        // The signal is back and the tech opens the app.
        server.set(["down": false])
        leaveAndComeBack()
        waitForHeld(count: count, timeout: 420)

        let seen = server.state()
        XCTAssertEqual(seen.videos.count, count, "\(count) recordings, \(count) videos on the server: no more, no fewer.")
        XCTAssertEqual(
            seen.videos.map { $0.customerId ?? 0 }, customers,
            "The server heard of them in the order they were recorded (oldest first)."
        )
        for video in seen.videos {
            XCTAssertEqual(video.registers, 1, "Registered once: \(video.summary)")
            XCTAssertEqual(video.puts, ["200"], "Uploaded once: \(video.summary)")
            XCTAssertEqual(video.completes, [200], "Confirmed once: \(video.summary)")
            XCTAssertEqual(video.bytes, video.sizeSent, "Every byte arrived: \(video.summary)")
            XCTAssertGreaterThan(video.bytes, 0, "And it was a real file: \(video.summary)")
        }
        XCTAssertEqual(Set(seen.videos.map { $0.id }).count, count, "\(count) different videos.")
        checkRegisterTries(seen, count: count)

        // The phone agrees, and holds nothing.
        waitForLabel(element("route-header"), "the header", contains: "Sent \(count) of \(count)", timeout: 90)
        waitForLabel(element("route-header"), "the header", contains: "Waiting 0", timeout: 30)
        XCTAssertFalse(app.buttons["waiting-banner"].exists, "Nothing is left waiting.")
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForBar("My Videos")
        XCTAssertEqual(videoRows.count, count)
        for customer in customers {
            waitForLabel(videoRow(customer), "the row for \(customer)", contains: "Sent", timeout: 30)
        }
        waitForLabel(element("held-space"), "the space line", contains: "holding 0 MB")
        XCTAssertFalse(element("keep-app-warning").exists, "Nothing is on the phone, so nothing warns about deleting the app.")
    }

    /// What the day may cost in register calls.
    ///
    /// With no signal the app cannot know the server is out of reach without
    /// trying, so it does try: the OLDEST video only (the line is held behind
    /// it), again after 5, 10, 20, 40... seconds. Those tries get no answer
    /// and the server never hears of the video. Once the signal is back every
    /// video costs exactly one register, one upload, one complete.
    ///
    /// So a register is repeated only after a try that failed, and never
    /// once one has been answered.
    private func checkRegisterTries(_ seen: ServerState, count: Int) {
        let tries = seen.registerTries
        let story = tries.map { $0.summary }.joined(separator: ", ")
        let answered = tries.filter { $0.gotThrough }
        let unanswered = tries.filter { !$0.gotThrough }

        XCTAssertEqual(answered.count, count, "One answered register for each video. All tries: \(story)")
        XCTAssertEqual(
            answered.map { $0.videoId }, seen.videos.map { $0.id },
            "Each video was answered once, oldest first. All tries: \(story)"
        )
        XCTAssertTrue(
            unanswered.allSatisfy { $0.answer == "dropped" },
            "The only tries that were not answered are the ones made with no signal. All tries: \(story)"
        )
        if let first = tries.firstIndex(where: { $0.gotThrough }) {
            XCTAssertTrue(
                tries[first...].allSatisfy { $0.gotThrough },
                "Once the signal was back nothing was registered twice. All tries: \(story)"
            )
        }
        if let oldest = seen.videos.first?.id {
            XCTAssertTrue(
                unanswered.allSatisfy { $0.videoId == oldest },
                "With no signal only the oldest video is tried; the rest wait in line behind it. All tries: \(story)"
            )
        }
        // It waits longer each time; it never hammers a server it cannot reach.
        for (earlier, later) in zip(unanswered, unanswered.dropFirst()) {
            XCTAssertGreaterThanOrEqual(
                later.at - earlier.at, 3,
                "Two tries with no signal came \(later.at - earlier.at) s apart. All tries: \(story)"
            )
        }
        XCTAssertLessThanOrEqual(
            unanswered.count, 12,
            "The waits double (5, 10, 20, 40, 80, 160, then 300 s), so a day this long has room for only a few tries. All tries: \(story)"
        )
    }
}
