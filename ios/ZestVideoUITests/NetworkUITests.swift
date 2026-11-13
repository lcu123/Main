import XCTest

/// A bad network and a server that says no. In every case the video stays on
/// the phone, the screen tells the truth (WAITING, never SENT), and it sends
/// once things are right again, with nothing lost and nothing sent twice.
final class NetworkUITests: ZestUITestCase {
    /// The video for the first stop is on the phone and shown as waiting.
    private func expectWaitingOnPhone(file: StaticString = #filePath, line: UInt = #line) {
        waitFor(app.buttons["waiting-banner"], "the \"1 video waiting to send\" line", timeout: 20, file: file, line: line)
        // A video still on the phone would be lost with the app: both
        // screens say so for as long as it is there.
        let banner = label(of: app.buttons["waiting-banner"])
        XCTAssertTrue(banner.contains("1 video waiting to send"), "The line says how many. It says: \(banner)", file: file, line: line)
        XCTAssertTrue(banner.contains("Do not delete this app"), "The line warns not to delete the app. It says: \(banner)", file: file, line: line)
        XCTAssertTrue(routeHeader.contains("Waiting 1"), "The header counts it as waiting. It says: \(routeHeader)", file: file, line: line)
        XCTAssertTrue(routeHeader.contains("Sent 1 of 5"), "It must not be counted as sent. The header says: \(routeHeader)", file: file, line: line)
        tap(app.buttons["waiting-banner"], "the waiting line", file: file, line: line)
        waitForBar("My Videos", file: file, line: line)
        waitForLabel(
            element("keep-app-warning"), "the warning at the top of My Videos",
            contains: "Do not delete this app. 1 video is still on this phone.", timeout: 10, file: file, line: line
        )
        let row = label(of: videoRow(Self.smith))
        XCTAssertTrue(row.contains("Waiting"), "The row says WAITING. It says: \(row)", file: file, line: line)
        XCTAssertFalse(row.contains("Sent"), "A video the server does not have must never say SENT. It says: \(row)", file: file, line: line)
        waitForLabel(element("held-space"), "the space line", contains: "holding 1 MB", timeout: 10, file: file, line: line)
        tap(app.buttons["tab-route"], "the My Route tab", file: file, line: line)
        waitForBar("My Route", file: file, line: line)
    }

    /// The video for the first stop reached the server and the screen says so.
    private func expectSent(file: StaticString = #filePath, line: UInt = #line) {
        waitForHeld(timeout: 90, file: file, line: line)
        waitForLabel(element("route-header"), "the header", contains: "Sent 2 of 5", timeout: 40, file: file, line: line)
        waitForLabel(element("route-header"), "the header", contains: "Waiting 0", timeout: 20, file: file, line: line)
        XCTAssertFalse(app.buttons["waiting-banner"].exists, "Nothing is left waiting.", file: file, line: line)
        tap(app.buttons["tab-videos"], "the My Videos tab", file: file, line: line)
        waitForLabel(videoRow(Self.smith), "the video's row", contains: "Sent", file: file, line: line)
        waitForLabel(element("held-space"), "the space line", contains: "holding 0 MB", file: file, line: line)
        XCTAssertFalse(element("keep-app-warning").exists, "Nothing is on the phone any more, so nothing warns about deleting the app.", file: file, line: line)
        XCTAssertFalse(hasText("Do not delete this app"), "The warning goes once the video has been sent.", file: file, line: line)
    }

    /// The server answers 500 to everything at SAVE. The video waits on the
    /// phone; when the server is well and the app comes back to the front,
    /// it sends, once.
    func testServerErrorsThenRecoverySendsWithoutLoss() {
        launchSignedIn()
        server.set(["failNext": 100_000])
        recordAndSave(for: Self.smith)
        waitUntil("the app tried to register and was refused") { server.state().registerCalls(status: 500) >= 1 }
        XCTAssertTrue(server.state().videos.isEmpty, "The server has no video yet.")
        expectWaitingOnPhone()

        server.set(["failNext": 0])
        leaveAndComeBack()
        expectSent()

        let video = theOnlyVideo()
        XCTAssertEqual(video.registers, 1, "Registered once the server was well: \(video.summary)")
        XCTAssertEqual(video.puts, ["200"], "Uploaded once: \(video.summary)")
        XCTAssertEqual(video.completes, [200], "Confirmed once: \(video.summary)")
    }

    /// The server cannot be reached at all at SAVE (the same as no signal).
    /// The screen says so, the video waits, and it sends when it is back.
    func testUnreachableServerThenBackSends() {
        launchSignedIn()
        server.set(["down": true])
        recordAndSave(for: Self.smith)
        waitForText("No signal", timeout: 30)
        XCTAssertTrue(stop(Self.johnson).exists, "The saved list still shows with no signal.")
        XCTAssertTrue(server.state().videos.isEmpty)
        expectWaitingOnPhone()

        server.set(["down": false])
        leaveAndComeBack()
        expectSent()

        let video = theOnlyVideo()
        XCTAssertEqual(video.goodUploads, 1, "Uploaded once: \(video.summary)")
        XCTAssertEqual(video.completes, [200], "Confirmed once: \(video.summary)")
        tap(app.buttons["tab-route"], "the My Route tab")
        waitUntil("the \"No signal\" line is gone", timeout: 30) { !hasText("No signal") }
    }

    /// The upload address had run out (403). The app asks for a new one and
    /// sends the file again by itself.
    func testExpiredUploadURLRegistersAgain() {
        launchSignedIn()
        server.set(["expireNextPut": 1])
        recordAndSave(for: Self.smith)
        expectSent()

        let video = theOnlyVideo()
        XCTAssertEqual(video.puts, ["403", "200"], "Refused once, then sent: \(video.summary)")
        XCTAssertEqual(video.registers, 2, "A second register for a fresh address, same video: \(video.summary)")
        XCTAssertEqual(video.completes, [200], "Confirmed once: \(video.summary)")
    }

    /// The server says it did not get the whole file ("not_uploaded"), once.
    /// The app uploads it again, and only then says SENT.
    func testNotUploadedOnceUploadsAgain() {
        launchSignedIn()
        server.set(["notUploadedNext": 1])
        recordAndSave(for: Self.smith)
        waitUntil("the server said not_uploaded") { server.state().videos.first?.completes.first == 409 }
        XCTAssertTrue(routeHeader.contains("Sent 1 of 5"), "Not counted as sent while the server lacks the file. The header says: \(routeHeader)")
        expectSent()

        let video = theOnlyVideo()
        XCTAssertEqual(video.puts, ["200", "200"], "The file is uploaded a second time: \(video.summary)")
        XCTAssertEqual(video.completes, [409, 200], "Refused once, then confirmed: \(video.summary)")
        XCTAssertEqual(server.state().videos.count, 1, "Still one video, with the same ID.")
    }

    /// The server signs the phone out (401) while a video is waiting. The
    /// app goes to sign-in, keeps the video, and sends it after signing in.
    func testSignedOutByServerKeepsTheQueue() {
        launchSignedIn()
        openRecorder(for: Self.smith)
        recordClip()
        server.set(["unauthorized": true])
        tap(button("SAVE"), "SAVE")

        waitFor(app.textFields["contact-field"], "the sign-in screen", timeout: 30)
        pause(2.5)
        XCTAssertEqual(barTitle, "Sign In", "The sign-in screen stays; \"Saved\" must not put My Route back over it.")
        XCTAssertTrue(server.state().videos.isEmpty, "The server took nothing while signed out.")

        server.set(["unauthorized": false])
        signIn()
        waitForHeld(timeout: 90)

        let seen = server.state()
        let video = theOnlyVideo()
        XCTAssertEqual(seen.logins, 1, "Signed in again once, through the screen.")
        XCTAssertEqual(video.customerId, Self.smith, "The video kept its stop through the sign-out.")
        XCTAssertEqual(video.puts, ["200"], "Uploaded once: \(video.summary)")
        XCTAssertEqual(video.completes, [200], "Confirmed once: \(video.summary)")
        waitForLabel(element("route-header"), "the header", contains: "Sent 2 of 5", timeout: 40)
    }

    /// The server says the app is too old (426). The update screen shows,
    /// and it is still there after a restart.
    func testUpdateRequiredShowsUpdateScreen() {
        launchSignedIn()
        server.set(["updateRequired": 999])
        leaveAndComeBack()

        waitForTitle("Please update Zest Video", timeout: 30)
        waitForText("This version is too old to send videos.")
        XCTAssertFalse(app.buttons["tab-route"].exists, "There is no way round the update screen.")

        kill()
        relaunch()
        waitForTitle("Please update Zest Video", timeout: 60)
    }

    /// The server says the stop's video already went to the customer
    /// (409 visit_has_video). The video is not sent, the screen says why in
    /// the server's words, and OK clears it.
    func testLockedByServerShowsWhyAndClears() {
        launchSignedIn()
        server.set(["visitHasVideo": true])
        recordAndSave(for: Self.smith)

        waitForText("already went to the customer", timeout: 30)
        waitForLabel(stop(Self.smith), "the stop's card", contains: "Not Sent")
        XCTAssertTrue(server.state().videos.isEmpty)

        tap(stop(Self.smith), "the stop whose video was refused")
        waitForBar("Video")
        waitForText("Call the office to change it.")
        tap(button("OK"), "OK")
        waitForBar("My Route")
        waitForLabel(stop(Self.smith), "the stop's card", contains: "Record Video")
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForText("No videos yet today.")
        waitForLabel(element("held-space"), "the space line", contains: "holding 0 MB")
        XCTAssertTrue(server.state().videos.isEmpty)
    }

    /// A long day with no signal: twelve stops, twelve videos saved with the
    /// server out of reach, then the signal comes back. All twelve arrive
    /// exactly once, in the order they were recorded; the phone ends holding
    /// no files; and My Videos tells the truth the whole way (WAITING until
    /// the server really has each one, never SENT before).
    ///
    /// The same day is also played through without a phone in ZestCore
    /// (`testTwelveVideosSavedWithNoSignalAllArriveOnceInOrder`).
    func testTwelveVideosSavedWithNoSignalAllArriveOnceInOrder() {
        executionTimeAllowance = 600
        // Twelve houses in a row on Maple Row, customers 301 to 312.
        resetServer(route: "long")
        let customers = Array(301...312)
        // One set of words for twelve customers, so no first name: the street,
        // a silent backup, is what the spoken key goes by here.
        launchSignedIn(transcript: "this is the stop on Maple Row", firstStop: customers[0])
        XCTAssertEqual(routeHeader, "Sam. Sent 0 of 12. Waiting 0.")

        server.set(["down": true])
        for (index, customer) in customers.enumerated() {
            recordAndSave(for: customer)
            let header = routeHeader
            XCTAssertTrue(
                header.hasSuffix("Sent 0 of 12. Waiting \(index + 1)."),
                "After video \(index + 1) with no signal the header must count it as waiting, not sent. It says: \(header)"
            )
        }
        XCTAssertTrue(server.state().videos.isEmpty, "The server heard of nothing while it was out of reach.")

        // My Videos: twelve rows, every one WAITING, none SENT.
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForBar("My Videos")
        let rows = app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "video-row-"))
        XCTAssertEqual(rows.count, 12, "My Videos lists all twelve.")
        for customer in customers {
            let row = label(of: videoRow(customer))
            XCTAssertTrue(row.contains("Waiting"), "The row for \(customer) says WAITING. It says: \(row)")
            XCTAssertFalse(row.contains("Sent"), "A video the server does not have must never say SENT. It says: \(row)")
        }
        waitForLabel(element("held-space"), "the space line", contains: "holding 1 MB", timeout: 10)
        tap(app.buttons["tab-route"], "the My Route tab")
        waitForBar("My Route")

        // The signal is back and the tech opens the app.
        server.set(["down": false])
        leaveAndComeBack()
        waitForHeld(count: 12, timeout: 240)

        let seen = server.state()
        XCTAssertEqual(seen.videos.count, 12, "Twelve recordings, twelve videos on the server: no more, no fewer.")
        XCTAssertEqual(
            seen.videos.map { $0.customerId ?? 0 }, customers,
            "The server heard of them in the order they were recorded (oldest first)."
        )
        for video in seen.videos {
            XCTAssertEqual(video.registers, 1, "Registered once: \(video.summary)")
            XCTAssertEqual(video.puts, ["200"], "Uploaded once: \(video.summary)")
            XCTAssertEqual(video.completes, [200], "Confirmed once: \(video.summary)")
            XCTAssertEqual(video.bytes, video.sizeSent, "Every byte arrived: \(video.summary)")
        }
        XCTAssertEqual(Set(seen.videos.map { $0.id }).count, 12, "Twelve different videos.")

        // The phone agrees, and holds nothing.
        waitForLabel(element("route-header"), "the header", contains: "Sent 12 of 12", timeout: 60)
        waitForLabel(element("route-header"), "the header", contains: "Waiting 0", timeout: 30)
        XCTAssertFalse(app.buttons["waiting-banner"].exists, "Nothing is left waiting.")
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForBar("My Videos")
        XCTAssertEqual(rows.count, 12)
        for customer in customers {
            waitForLabel(videoRow(customer), "the row for \(customer)", contains: "Sent", timeout: 30)
        }
        waitForLabel(element("held-space"), "the space line", contains: "holding 0 MB")
    }

    /// The app is started with no server to reach. Yesterday's sign-in and
    /// today's saved list are used, a video can be recorded and saved, and
    /// it sends when the server is back.
    func testOfflineStartShowsSavedRouteAndRecords() {
        launchSignedIn()
        kill()
        server.set(["down": true])
        relaunch()

        waitFor(stop(Self.smith), "the saved stops, with no server", timeout: 60)
        XCTAssertEqual(barTitle, "My Route")
        waitForText("No signal. Showing your saved list.", timeout: 30)
        XCTAssertTrue(routeHeader.hasPrefix("Sam."), "The header still names the tech. It says: \(routeHeader)")

        recordAndSave(for: Self.smith)
        XCTAssertTrue(server.state().videos.isEmpty)
        expectWaitingOnPhone()

        server.set(["down": false])
        leaveAndComeBack()
        expectSent()
        XCTAssertEqual(theOnlyVideo().completes, [200])
    }
}
