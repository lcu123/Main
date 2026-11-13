import XCTest

/// Speed and weight. These tests measure; they are not pass-or-fail checks
/// of speed (a CI simulator is no phone), except where a number can only
/// mean a fault: video files left on disk after everything was sent, or
/// memory that climbs with every video.
///
/// Each test writes its part of a plain table (`performance.<part>.info.txt`
/// with the run's results); CI puts the parts together as `performance.txt`.
///
/// The app supplies its own numbers in UI-test mode (`ZEST_TEST_METRICS=1`):
/// a second after each screen change it posts its memory footprint, what it
/// holds on disk, and the time since its own code started.
final class PerformanceUITests: ZestUITestCase {
    private func seconds(_ values: [Double]) -> String {
        values.map { String(format: "%.2f", $0) }.joined(separator: "  ")
    }

    private func middle(_ values: [Double]) -> Double {
        guard !values.isEmpty else { return 0 }
        let sorted = values.sorted()
        return sorted[sorted.count / 2]
    }

    /// Waits for the app to post a number, and returns the newest one of that name.
    private func metric(_ name: String, after count: Int, timeout: TimeInterval = 15) -> AppMetric? {
        var found: AppMetric?
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            let all = server.state().metrics.filter { $0.name.hasPrefix(name) }
            if all.count > count {
                found = all.last
                break
            }
            pause(0.5)
        }
        return found
    }

    private func count(_ name: String) -> Int {
        server.state().metrics.filter { $0.name.hasPrefix(name) }.count
    }

    // MARK: 1. Cold launch

    /// From a cold start to the first screen (the sign-in screen).
    func testColdLaunchToTheFirstScreen() {
        // Once before measuring: the very first launch after an install is its own, slower thing.
        launch(metrics: true)
        waitFor(app.textFields["contact-field"], "the sign-in screen", timeout: 60)
        kill()

        // Apple's own measure of a launch. Its numbers are printed in the
        // log ("measured [Duration (AppLaunch), s] ..."); CI copies them out.
        let options = XCTMeasureOptions()
        options.iterationCount = 5
        measure(metrics: [XCTApplicationLaunchMetric()], options: options) {
            launch(reset: false)
        }
        kill()

        // And as the test sees it: from asking for the launch to the first
        // screen being there. This includes the test tool's own overhead.
        var outside: [Double] = []
        var inside: [Double] = []
        var memory: [Double] = []
        for _ in 0..<5 {
            let before = count("route=login")
            let started = Date()
            launch(reset: false)
            waitFor(app.textFields["contact-field"], "the sign-in screen", timeout: 60)
            outside.append(Date().timeIntervalSince(started))
            if let posted = metric("route=login", after: before) {
                // Posted one second after the screen was chosen: take that second off.
                inside.append(max(0, posted.milliseconds - 1000) / 1000)
                memory.append(posted.memoryMB)
            }
            kill()
        }
        saveNote(named: "performance.1-launch.info", """
        COLD LAUNCH TO THE FIRST SCREEN (sign-in), 5 launches
          seen from the test, launch call to screen (s):        \(seconds(outside))    middle \(String(format: "%.2f", middle(outside)))
            (this includes the test tool's own waiting)
          inside the app, its code start to the screen (s):     \(seconds(inside))    middle \(String(format: "%.2f", middle(inside)))
            (this leaves out the time iOS takes to load the app)
          memory on the sign-in screen (MB):                    \(memory.map { String(format: "%.0f", $0) }.joined(separator: " "))
          Apple's own launch measure is further down ("APPLE'S OWN LAUNCH MEASURE").
        """)
    }

    // MARK: 2. Launch to My Route, with the route saved on the phone

    /// Signed in, today's route already saved: how long until My Route can be used.
    func testLaunchToMyRouteWithASavedRoute() {
        // The first launch signs in and saves the route.
        launchSignedIn(metrics: true)
        pause(1.5)
        kill()

        var outside: [Double] = []
        var inside: [Double] = []
        for _ in 0..<5 {
            let before = count("routeUsable")
            let started = Date()
            relaunch()
            waitFor(stop(Self.smith), "the stops on My Route", timeout: 60)
            outside.append(Date().timeIntervalSince(started))
            if let posted = metric("routeUsable", after: before) {
                inside.append(posted.milliseconds / 1000)
            }
            kill()
        }
        XCTAssertEqual(server.state().logins, 0, "No sign-in was needed on any of these launches.")
        saveNote(named: "performance.2-route.info", """
        LAUNCH TO MY ROUTE, SIGNED IN, TODAY'S ROUTE SAVED ON THE PHONE, 5 launches
          seen from the test, launch call to stops on screen (s):   \(seconds(outside))    middle \(String(format: "%.2f", middle(outside)))
          inside the app, its code start to My Route with stops (s): \(seconds(inside))    middle \(String(format: "%.2f", middle(inside)))
        """)
    }

    // MARK: 3. Memory and disk over ten videos

    /// Ten videos in a row: record, save, wait until it is sent. Memory on
    /// each screen, memory back on My Route after every video (it should
    /// settle, not climb), and what is left on disk at the end (no video
    /// files).
    func testMemoryAndDiskOverTenVideos() {
        executionTimeAllowance = 600
        resetServer(route: "long")
        let customers = Array(301...310)
        launchSignedIn(transcript: "this is the stop on Maple Row", metrics: true, firstStop: customers[0])
        pause(1.5)

        var onRoute: [Double] = []
        var onConfirm: [Double] = []
        var onRecord: [Double] = []
        var onReview: [Double] = []
        let atStart = metric("route=today", after: 0)?.memoryMB ?? -1

        for (index, customer) in customers.enumerated() {
            var before = count("route=confirm")
            tap(stop(customer), "the card for stop \(customer)")
            waitForTitle("Is this the stop?")
            if let posted = metric("route=confirm", after: before) { onConfirm.append(posted.memoryMB) }

            before = count("route=record")
            tap(button("THIS IS IT"), "THIS IS IT")
            waitFor(app.buttons["record-button"], "the red record button")
            tap(app.buttons["record-button"], "the red record button")
            if let posted = metric("route=record", after: before) { onRecord.append(posted.memoryMB) }

            before = count("route=review")
            waitForTitle("Save this video?", timeout: 40)
            if let posted = metric("route=review", after: before) { onReview.append(posted.memoryMB) }

            before = count("route=today")
            saveClip()
            waitForHeld(count: index + 1, timeout: 60)
            waitForLabel(element("route-header"), "the header", contains: "Sent \(index + 1) of 12", timeout: 40)
            // Let it settle before the reading that counts.
            pause(2)
            _ = metric("route=today", after: before)
            tap(app.buttons["tab-videos"], "the My Videos tab")
            waitForBar("My Videos")
            before = count("route=today")
            tap(app.buttons["tab-route"], "the My Route tab")
            waitForBar("My Route")
            if let posted = metric("route=today", after: before) { onRoute.append(posted.memoryMB) }
        }

        // What is on disk now that all ten are sent.
        let last = server.state().metrics.last
        let files = last?.videoFiles ?? -1
        let videoBytes = last?.videoBytes ?? -1
        let stored = last?.storedBytes ?? -1
        let temp = last?.tempBytes ?? -1

        // Does memory climb with every video? Compare the first three
        // readings back on My Route with the last three.
        let early = middle(Array(onRoute.prefix(3)))
        let late = middle(Array(onRoute.suffix(3)))
        let perVideo = onRoute.count >= 6 ? (late - early) / Double(onRoute.count - 3) : 0
        let verdict: String
        if onRoute.count < 6 {
            verdict = "NOT ENOUGH READINGS to say"
        } else if perVideo > 1.0 {
            verdict = "LOOK AT THIS: memory grows about \(String(format: "%.1f", perVideo)) MB with each video"
        } else {
            verdict = "settles: about \(String(format: "%.2f", perVideo)) MB per video, which is noise"
        }
        let megabytes = { (values: [Double]) in values.map { String(format: "%.0f", $0) }.joined(separator: " ") }
        let sent = server.state().videos.filter { $0.isHeld }.count

        saveNote(named: "performance.3-memory.info", """
        MEMORY, in MB (the footprint iOS itself counts against the app)
          My Route at the start:                 \(String(format: "%.0f", atStart))
          "Is this the stop?" (10 readings):     \(megabytes(onConfirm))
          record screen, recording (10):         \(megabytes(onRecord))
          "Save this video?" with the clip (10): \(megabytes(onReview))
          back on My Route after each video:     \(megabytes(onRoute))
          verdict: \(verdict)

        DISK, after ten videos recorded, saved and sent (\(sent) of 10 confirmed by the server)
          video files still on the phone:   \(files)   (\(videoBytes) bytes)   \(files == 0 ? "good: none" : "LOOK AT THIS: files were left behind")
          everything the app stores:        \(stored) bytes (the saved route, the day's list of videos, the event log)
          temporary files:                  \(temp) bytes
        """)

        XCTAssertEqual(sent, 10, "All ten videos reached the server.")
        XCTAssertEqual(files, 0, "No video file may be left on the phone once every video is sent.")
        XCTAssertLessThan(perVideo, 5, "Memory must not climb by megabytes with every video (it grew about \(String(format: "%.1f", perVideo)) MB each).")
    }
}
