import XCTest

/// Location refused. The app must still do its whole job: the stops show in
/// route order, a video is recorded, saved and sent, and the GPS key is
/// simply "unknown". (Every other UI test answers "Allow".)
///
/// This class is named to run last among the UI tests of its job, and puts
/// the phone's answer back to "not asked yet" when it is done, so the next
/// test to sign in is asked again and says yes.
final class WithoutLocationUITests: ZestUITestCase {
    override func setUpWithError() throws {
        try super.setUpWithError()
        // CI grants location ahead of time; forget that, and say no this time.
        Self.refusing = true
        forgetLocationAnswer()
    }

    override func tearDownWithError() throws {
        try super.tearDownWithError()
        Self.refusing = false
        forgetLocationAnswer()
    }

    func testWithLocationRefusedAVideoStillArrivesWithGpsUnknown() {
        launchSignedIn()
        // In case the question came late: the answer is still no.
        allowSystemQuestions(wait: 3)
        XCTAssertEqual(app.state, .runningForeground, "The app is still running after location was refused.")

        // No "closest stop" without a fix: the stops that need a video are in
        // route order, and the one that is done comes last.
        let order = [Self.smith, Self.johnson, Self.garcia, Self.business, Self.locked]
        for customer in order {
            XCTAssertTrue(stop(customer).exists, "Stop \(customer) should be on the list.")
        }
        let tops = order.map { stop($0).frame.minY }
        XCTAssertEqual(tops, tops.sorted(), "With no location the stops are in route order: \(order). Their tops are \(tops).")
        XCTAssertFalse(label(of: stop(Self.smith)).contains("Closest"), "Nothing is called closest without a location.")
        XCTAssertFalse(hasText(" ft away") || hasText(" mi away"), "No distances are shown without a location.")

        // The far-away question cannot be asked (nothing to measure), so
        // THIS IS IT goes straight to the camera.
        openRecorder(for: Self.smith)
        recordClip()
        saveClip()
        waitForHeld()

        let video = theOnlyVideo()
        XCTAssertEqual(video.customerId, Self.smith)
        XCTAssertEqual(video.signals["gps"], "unknown", "No fix, so the GPS key says nothing either way.")
        XCTAssertTrue(video.sentNoLocation, "And no location is sent with the video: \(String(describing: video.payload["location"]))")
        XCTAssertEqual(video.signals["spoken"], "match", "The name was still heard.")
        XCTAssertEqual(video.signals["checkIn"], "match", "The check-in still matched.")
        XCTAssertFalse(video.override, "Two keys still agree, so there was no warning to get past.")
        XCTAssertEqual(video.completes, [200])
        XCTAssertEqual(server.state().registerCalls(), 1)

        waitForLabel(element("route-header"), "the header", contains: "Sent 2 of 5")
        XCTAssertEqual(app.state, .runningForeground, "No crash anywhere on the way.")
    }
}
