import XCTest

/// My Route: the list, its header, the two tabs, and the screens a stop's
/// card leads to before any recording.
final class RouteUITests: ZestUITestCase {
    /// The header counts, the stop the phone is standing on comes first, the
    /// stops are in the right order, and both tabs work.
    func testMyRouteShowsHeaderClosestStopAndTabs() {
        launchAndSignIn()
        waitForBar("My Route")
        // One of the five stops (Wilson) already has a video with its customer.
        XCTAssertEqual(routeHeader, "Sam. Sent 1 of 5. Waiting 0.")

        waitForLocation()
        let first = label(of: stop(Self.smith))
        for part in ["Closest stop", "Smith, Jane", "123 Main St", "Record Video"] {
            XCTAssertTrue(first.contains(part), "The first card should say \"\(part)\". It says: \(first)")
        }

        // Nearest three first, then the rest of the route, then the one that is done.
        let order = [Self.smith, Self.johnson, Self.garcia, Self.business, Self.locked]
        for customer in order {
            XCTAssertTrue(stop(customer).exists, "Stop \(customer) should be on the list.")
        }
        let tops = order.map { stop($0).frame.minY }
        XCTAssertEqual(tops, tops.sorted(), "The stops should be in this order from the top: \(order). Their tops are \(tops).")
        let done = label(of: stop(Self.locked))
        XCTAssertTrue(done.contains("Sent"), "The stop whose video went out should say SENT. It says: \(done)")
        XCTAssertTrue(label(of: stop(Self.business)).contains("Green Valley HOA"), "A business shows its name as it is.")

        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForBar("My Videos")
        waitForText("No videos yet today.")
        tap(app.buttons["tab-route"], "the My Route tab")
        waitForBar("My Route")
        XCTAssertTrue(stop(Self.smith).exists)
        XCTAssertTrue(server.state().videos.isEmpty)
    }

    /// STOP NOT ON MY LIST opens its own confirm screen, and BACK returns.
    func testStopNotOnMyListOpensAndBacksOut() {
        launchAndSignIn()
        tap(app.buttons["not-on-my-list"], "STOP NOT ON MY LIST")
        waitForTitle("Not on my list")
        waitForText("The office will match it to the customer.")
        XCTAssertTrue(button("RECORD").exists)

        tap(button("BACK"), "BACK")
        waitForBar("My Route")
        XCTAssertTrue(server.state().videos.isEmpty)
    }

    /// With a slow server the list says it is loading, the tech can still
    /// start a video, and the stops then show by themselves.
    func testLoadingStateWhileRouteIsSlow() {
        server.set(["todayDelay": 6])
        launch()
        typeMobileAndSend()
        typeCode(Self.code)

        waitFor(element("loading-route"), "the \"Getting your stops…\" line", timeout: 20)
        XCTAssertFalse(stop(Self.smith).exists, "No stops can show before the server has answered.")
        XCTAssertTrue(app.buttons["not-on-my-list"].exists, "STOP NOT ON MY LIST is there while the list loads.")

        answerLocationQuestion()
        waitFor(stop(Self.smith), "the stops, once the server answered", timeout: 40)
        XCTAssertFalse(element("loading-route").exists, "The loading line goes once the stops are in.")
        XCTAssertFalse(hasText("No signal"), "A slow answer is not \"no signal\".")
    }

    /// A stop whose video already went to the customer: no way to change it
    /// here, the office number is offered, and both ways back work.
    func testLockedStopShowsCallTheOffice() {
        launchAndSignIn()
        tap(stop(Self.locked), "the stop whose video already went out")
        waitForBar("Video")
        waitForText("already went to the customer")
        XCTAssertTrue(hasText("Wilson, Tom"))
        XCTAssertFalse(button("RECORD AGAIN").exists, "A locked stop must not offer RECORD AGAIN.")
        XCTAssertFalse(button("DELETE VIDEO").exists, "A locked stop must not offer DELETE VIDEO.")
        XCTAssertTrue(
            button("CALL OFFICE").exists || hasText("(916) 555-0100"),
            "A locked stop offers CALL OFFICE, or shows the office number where the device cannot call."
        )

        tap(button("BACK"), "BACK")
        waitForBar("My Route")
        tap(stop(Self.locked), "the locked stop again")
        waitForBar("Video")
        tap(app.buttons["bar-back"], "the back arrow")
        waitForBar("My Route")

        let seen = server.state()
        XCTAssertTrue(seen.videos.isEmpty)
        XCTAssertEqual(seen.count("DELETE", "/v1/videos"), 0)
    }

    /// The confirm screen names the stop in large type; WRONG STOP and the
    /// back arrow both return to the list with nothing recorded.
    func testWrongStopGoesBack() {
        launchAndSignIn()
        tap(stop(Self.smith), "the first stop")
        waitForTitle("Is this the stop?")
        XCTAssertTrue(hasText("Smith, Jane"))
        XCTAssertTrue(hasText("123 Main St"))
        tap(button("WRONG STOP"), "WRONG STOP")
        waitForBar("My Route")

        tap(stop(Self.johnson), "the second stop")
        waitForTitle("Is this the stop?")
        XCTAssertTrue(hasText("Johnson, Robert"))
        XCTAssertFalse(hasText("Smith, Jane"), "The confirm screen must show the stop that was tapped, not the one before.")
        tap(app.buttons["bar-back"], "the back arrow")
        waitForBar("My Route")
        XCTAssertTrue(server.state().videos.isEmpty)
    }

    /// Confirming a stop the phone is far from asks once more. PICK ANOTHER
    /// STOP goes back; YES, STILL RIGHT carries on to the camera.
    func testFarAwayWarning() {
        launchAndSignIn()
        waitForLocation()

        tap(stop(Self.garcia), "a stop about 375 m away")
        waitForTitle("Is this the stop?")
        tap(button("THIS IS IT"), "THIS IS IT")
        waitForTitle("You look far from this address")
        XCTAssertTrue(hasText("Garcia, Maria"))
        waitForText("Still right?")
        tap(button("PICK ANOTHER STOP"), "PICK ANOTHER STOP")
        waitForBar("My Route")

        tap(stop(Self.garcia), "the far stop again")
        waitForTitle("Is this the stop?")
        tap(button("THIS IS IT"), "THIS IS IT")
        waitForTitle("You look far from this address")
        tap(button("YES, STILL RIGHT"), "YES, STILL RIGHT")
        waitFor(app.buttons["record-button"], "the red record button")

        tap(button("BACK"), "BACK")
        waitForBar("My Route")
        XCTAssertTrue(server.state().videos.isEmpty)
    }
}
