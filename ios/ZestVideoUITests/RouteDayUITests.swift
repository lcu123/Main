import XCTest

/// The office changes the route during the day, and the tech does not
/// drive it in the planned order.
///
/// The fake server plays the office: `server.changeRoute(...)` cancels a
/// stop, gives one to another tech, puts one back, adds one. The app is the
/// real app. What these tests hold it to:
///
/// - **Nothing lost.** A clip filmed at a stop that has since left the list
///   still reaches the server, whole, exactly once.
/// - **Never the wrong customer.** No call ever names a customer other than
///   the one the tech picked.
/// - **No loop.** A "no" from the server is asked about a fixed number of
///   times, not every few seconds for ever.
/// - **Sent as filmed.** A video for a stop that has left the list goes for
///   the customer the tech picked, with the appointments the list had. If
///   the server will not take it for that customer, it ends at SENT TO
///   OFFICE.
final class RouteDayUITests: ZestUITestCase {
    private static let tidewater = 206

    private var newStop: FakeServer.NewStop {
        FakeServer.NewStop(
            customerId: Self.tidewater, name: "Morgan Tidewater", lastName: "Tidewater", address: "50 Oak Ave",
            north: 300, west: 280, after: Self.johnson
        )
    }

    private var officeRow: XCUIElement { app.buttons["video-row-offlist"] }

    /// Every register call the server saw, as one line each, for a failure message.
    private var registerLog: String {
        server.state().registerTries.map { "\($0.summary) for \($0.customerId.map(String.init) ?? "the office")" }.joined(separator: "; ")
    }

    /// No call ever named a customer the tech did not pick.
    private func expectNoOtherCustomer(than picked: Int, file: StaticString = #filePath, line: UInt = #line) {
        let state = server.state()
        for attempt in state.registerTries {
            XCTAssertTrue(
                attempt.customerId == nil || attempt.customerId == picked,
                "A register named customer \(String(describing: attempt.customerId)); the tech picked \(picked). All: \(registerLog)",
                file: file, line: line
            )
        }
        for video in state.videos {
            XCTAssertTrue(
                video.customerId == nil || video.customerId == picked,
                "The server holds a video for another customer: \(video.summary)", file: file, line: line
            )
        }
    }

    /// The server's answer does not change, so the app must not keep asking.
    private func expectNoMoreRegisters(than count: Int, during seconds: TimeInterval, file: StaticString = #filePath, line: UInt = #line) {
        pause(seconds)
        XCTAssertEqual(
            server.state().registerTries.count, count,
            "The app went on asking. All register calls: \(registerLog)", file: file, line: line
        )
    }

    // MARK: A stop removed while it is open

    /// The tech has a stop open on "Is this the stop?" when the office
    /// cancels it. The screen stays, says so in one line, and still
    /// records. The video is sent exactly as it was filmed, for the
    /// customer he picked and with the appointments the list had; the
    /// server takes it, and the office decides what becomes of it.
    func testAStopCancelledWhileItIsOpenStillRecordsAndTheVideoIsSentAsFilmed() {
        launchSignedIn()
        tap(stop(Self.smith), "the card for Smith")
        waitForTitle("Is this the stop?")
        XCTAssertFalse(element("left-list").exists, "Nothing is said while the stop is on the list.")

        // The office cancels the stop. The phone hears at its next look.
        XCTAssertEqual(server.changeRoute(remove: [Self.smith]), [Self.locked, Self.johnson, Self.garcia, Self.business])
        leaveAndComeBack()

        // Same screen, same stop, one more line.
        waitForLabel(
            element("left-list"), "the line under the stop's card",
            contains: "This stop is no longer on your list. You can still record it.", timeout: 30
        )
        XCTAssertEqual(screenTitle, "Is this the stop?", "He is not thrown off the screen he was on.")
        XCTAssertTrue(hasText("Smith, Jane") && hasText("123 Main St"), "It still says which stop.")

        tap(button("THIS IS IT"), "THIS IS IT")
        waitFor(app.buttons["record-button"], "the red record button")
        recordClip()
        XCTAssertTrue(hasText("Smith, Jane"), "\"Save this video?\" is still for Smith.")
        saveClip()

        // It reaches the server, whole, once, as it was filmed.
        waitForHeld(timeout: 90)
        let video = theOnlyVideo()
        XCTAssertEqual(video.customerId, Self.smith, "For the customer he picked: \(video.summary)")
        XCTAssertFalse(video.notOnMyList)
        XCTAssertEqual(video.appointmentIds, [2010, 2011], "With the appointments the list had when he picked the stop.")
        XCTAssertEqual(video.registers, 1)
        XCTAssertEqual(video.puts, ["200"], "One upload: \(video.summary)")
        XCTAssertEqual(video.completes, [200])
        expectNoOtherCustomer(than: Self.smith)

        // The stop is off the list; the video is still his, under its name.
        tap(app.buttons["tab-route"], "the My Route tab")
        waitUntil("the cancelled stop has left the list", timeout: 30) { !self.stop(Self.smith).exists }
        waitForLabel(element("route-header"), "the header", contains: "Sent 1 of 4", timeout: 30)
        XCTAssertTrue(routeHeader.contains("Waiting 0"), "Nothing is left waiting. The header says: \(routeHeader)")
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForLabel(videoRow(Self.smith), "the video's row", contains: "Sent", timeout: 30)
        XCTAssertTrue(label(of: videoRow(Self.smith)).contains("Smith, Jane"), "The row says whose it is: \(label(of: videoRow(Self.smith)))")
        waitForLabel(element("held-space"), "the space line", contains: "holding 0 MB")
        tap(videoRow(Self.smith), "the video's row")
        waitForBar("Video")
        XCTAssertTrue(hasText("Smith, Jane") && hasText("123 Main St"), "The stop's address is still shown, though the stop has left the list.")
    }

    // MARK: A stale list

    /// The phone has had no signal since the list was loaded. The office
    /// cancels a stop; the phone cannot know, and the tech films there.
    /// When the signal is back the video is sent as it was filmed, the
    /// server takes it, the list drops the stop, and no other customer is
    /// ever named.
    func testFilmedFromAStaleListAtAStopCancelledHoursAgo() {
        launchSignedIn()
        server.set(["down": true])
        server.changeRoute(remove: [Self.smith])

        recordAndSave(for: Self.smith)
        waitForText("No signal", timeout: 30)
        XCTAssertTrue(stop(Self.smith).exists, "With no signal the saved list still has the stop.")
        XCTAssertFalse(hasText("no longer on your list"), "The phone cannot know yet, and says nothing.")
        XCTAssertTrue(server.state().videos.isEmpty)
        XCTAssertTrue(routeHeader.contains("Waiting 1"), "The header says: \(routeHeader)")

        server.set(["down": false])
        leaveAndComeBack()

        waitForHeld(timeout: 120)
        let video = theOnlyVideo()
        XCTAssertEqual(video.customerId, Self.smith, "For the customer he picked: \(video.summary)")
        XCTAssertEqual(video.appointmentIds, [2010, 2011], "With the appointments his list had.")
        XCTAssertEqual(video.registers, 1, "Taken at the first asking that got through: \(video.summary)")
        XCTAssertEqual(video.goodUploads, 1, "One whole upload: \(video.summary)")
        expectNoOtherCustomer(than: Self.smith)

        waitUntil("the cancelled stop has left the list", timeout: 30) { !self.stop(Self.smith).exists }
        waitForLabel(element("route-header"), "the header", contains: "Waiting 0", timeout: 30)
        XCTAssertFalse(hasText("goes to the office"), "The server took it for the customer: nothing says otherwise.")
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForLabel(videoRow(Self.smith), "the video's row", contains: "Sent", timeout: 30)
        expectNoMoreRegisters(than: server.state().registerTries.count, during: 5)
    }

    /// The same day against a server that answers "That stop is not on the
    /// list for that day. Use Not On My List." (400): one from before the
    /// rule above, or any server asked about a customer who was never a
    /// stop that day. The app does what the answer says, by itself: the
    /// same clip under the same ID goes to the office. Two register calls,
    /// one upload, SENT TO OFFICE, and then no more calls.
    func testAServerThatRefusesTheStopGetsTheVideoAsNotOnMyList() {
        launchSignedIn()
        server.set(["down": true, "offList": "refuse"])
        server.changeRoute(remove: [Self.smith])
        recordAndSave(for: Self.smith)
        waitForText("No signal", timeout: 30)

        server.set(["down": false])
        leaveAndComeBack()

        // It reaches the office, whole, once.
        waitUntil("the office has the video", timeout: 120) { self.server.state().videos.first?.isWithTheOffice ?? false }
        let video = theOnlyVideo()
        XCTAssertTrue(video.notOnMyList, "It was sent as not on my list: \(video.summary)")
        XCTAssertNil(video.customerId)
        XCTAssertEqual(video.puts, ["200"], "One upload: \(video.summary)")
        XCTAssertEqual(video.completes, [200])
        let answered = server.state().registerTries.filter { $0.answer != "dropped" }
        XCTAssertEqual(answered.map { $0.answer }, ["400", "200"], "Asked once for the customer, once for the office: \(registerLog)")
        XCTAssertEqual(answered.first?.customerId, Self.smith)
        XCTAssertEqual(Set(answered.map { $0.videoId }).count, 1, "The same video both times: \(registerLog)")
        expectNoOtherCustomer(than: Self.smith)
        expectNoMoreRegisters(than: server.state().registerTries.count, during: 6)

        // My Route says what happened, and the stop is off the list.
        tap(app.buttons["tab-route"], "the My Route tab")
        waitForText("Smith, Jane is no longer on your list. The video goes to the office.", timeout: 20)
        waitUntil("the cancelled stop has left the list", timeout: 30) { !self.stop(Self.smith).exists }
        waitForLabel(element("route-header"), "the header", contains: "Sent 1 of 4", timeout: 30)
        XCTAssertTrue(routeHeader.contains("Waiting 0"), "Nothing is left waiting. The header says: \(routeHeader)")

        // My Videos keeps the customer's name on it, and tells the truth.
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForLabel(officeRow, "the video's row", contains: "Sent To Office", timeout: 30)
        XCTAssertTrue(label(of: officeRow).contains("Smith, Jane"), "The row still says whose stop it was: \(label(of: officeRow))")
        waitForLabel(element("held-space"), "the space line", contains: "holding 0 MB")
        XCTAssertFalse(element("keep-app-warning").exists, "The server has it: nothing is left on the phone.")
        tap(officeRow, "the video's row")
        waitForBar("Video")
        XCTAssertTrue(hasText("123 Main St"), "The stop's address is still shown, though the stop has left the list.")
        XCTAssertFalse(hasText("goes to the customer"), "It does not go to the customer, and no screen says it does.")

        // The audit trail has why, with the customer he picked.
        leaveAndComeBack()
        waitUntil("the server holds the upload_failed event", timeout: 60) {
            self.eventTypes(for: Self.smith).contains("upload_failed")
        }
        let event = server.state().events.first { ($0["type"] as? String) == "upload_failed" }
        let detail = event?["detail"] as? [String: Any]
        XCTAssertEqual(detail?["then"] as? String, "sent_to_office", "The detail: \(String(describing: detail))")
        XCTAssertEqual(detail?["status"] as? String, "400")
        XCTAssertEqual(event?["videoId"] as? String, video.id)
    }

    // MARK: The list follows the office

    /// The office changes the route three times: a stop cancelled, a stop
    /// added, the cancelled one put back. The list follows each time the
    /// app comes to the front, the numbers on the cards follow the route as
    /// it now is, and once, with no touch at all, inside its two minutes.
    func testTheListFollowsEveryChangeAndSoDoTheNumbers() {
        launchSignedIn()
        waitForLocation()
        // The morning: Wilson is 1 (his video went out, so his badge is a
        // tick), then Smith 2, Johnson 3, Garcia 4, the HOA 5.
        XCTAssertEqual([Self.smith, Self.johnson, Self.garcia, Self.business].map { number(onCard: $0) }, [2, 3, 4, 5])
        XCTAssertNil(number(onCard: Self.locked))
        XCTAssertTrue(routeHeader.contains("Sent 1 of 5"), "The header says: \(routeHeader)")

        // 1. Garcia is cancelled: gone from the list, and the HOA moves up.
        server.changeRoute(remove: [Self.garcia])
        leaveAndComeBack()
        waitUntil("the cancelled stop has left the list", timeout: 30) { !self.stop(Self.garcia).exists }
        waitForLabel(element("route-header"), "the header", contains: "Sent 1 of 4")
        XCTAssertEqual([Self.smith, Self.johnson, Self.business].map { number(onCard: $0) }, [2, 3, 4])
        XCTAssertFalse(hasText("no longer on your list"), "Nothing was open or filmed there: nothing needs saying.")

        // 2. A stop is added after Johnson: it shows, ready to record.
        server.changeRoute(add: [newStop])
        leaveAndComeBack()
        waitFor(stop(Self.tidewater), "the added stop", timeout: 30)
        let added = label(of: stop(Self.tidewater))
        XCTAssertTrue(added.contains("Tidewater, Morgan") && added.contains("50 Oak Ave") && added.contains("Record Video"), "Its card says: \(added)")
        XCTAssertEqual([Self.smith, Self.johnson, Self.tidewater, Self.business].map { number(onCard: $0) }, [2, 3, 4, 5])
        waitForLabel(element("route-header"), "the header", contains: "Sent 1 of 5")

        // 3. Garcia is put back, with no touch on the phone this time: the
        //    list looks again by itself every two minutes while it shows.
        server.changeRoute(restore: [Self.garcia])
        waitFor(stop(Self.garcia), "the stop put back, with no touch", timeout: 170)
        XCTAssertEqual(
            [Self.smith, Self.johnson, Self.tidewater, Self.garcia, Self.business].map { number(onCard: $0) }, [2, 3, 4, 5, 6]
        )
        XCTAssertTrue(label(of: stop(Self.garcia)).contains("Record Video"), "It is an open stop again: \(label(of: stop(Self.garcia)))")

        // The added stop is a stop like any other: it can be opened.
        tap(stop(Self.tidewater), "the added stop")
        waitForTitle("Is this the stop?")
        XCTAssertTrue(hasText("Tidewater, Morgan"))
        tap(button("WRONG STOP"), "WRONG STOP")
        waitForBar("My Route")
        XCTAssertTrue(server.state().videos.isEmpty, "Looking at the list sends nothing.")
    }

    /// A stop with a video, cancelled and then put back. His video went to
    /// the customer before the change and stays as it is; the card comes
    /// back saying so, not as a stop to record.
    func testAFilmedStopTakenOffAndPutBackKeepsItsVideo() {
        launchSignedIn()
        recordAndSave(for: Self.smith)
        waitForHeld()
        waitForLabel(stop(Self.smith), "the stop's card", contains: "Change Video")

        server.changeRoute(remove: [Self.smith])
        leaveAndComeBack()
        waitUntil("the cancelled stop has left the list", timeout: 30) { !self.stop(Self.smith).exists }
        // The video is still his, on My Videos, with the stop's address.
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForLabel(videoRow(Self.smith), "the video's row", contains: "Sent")
        tap(videoRow(Self.smith), "the video's row")
        waitForBar("Video")
        XCTAssertTrue(hasText("Smith, Jane") && hasText("123 Main St"), "The screen still says whose video it is, and where.")
        tap(button("BACK"), "BACK")

        server.changeRoute(restore: [Self.smith])
        leaveAndComeBack()
        tap(app.buttons["tab-route"], "the My Route tab")
        waitFor(stop(Self.smith), "the stop put back", timeout: 30)
        waitForLabel(stop(Self.smith), "the stop's card", contains: "Change Video", timeout: 20)
        XCTAssertFalse(label(of: stop(Self.smith)).contains("Record Video"), "It is not offered as a stop with no video.")
        let video = theOnlyVideo()
        XCTAssertTrue(video.isHeld, "Nothing happened to the video: \(video.summary)")
        XCTAssertEqual(video.registers, 1)
        XCTAssertEqual(video.deletes, 0)
    }

    // MARK: A video the server holds for the office

    /// He films a stop; the office then cancels the visit; the email time
    /// comes. The server sends no email and holds the video for the office
    /// (`withOffice` in the day's answer). The phone says SENT TO OFFICE,
    /// not SENT ✓, never that it went to the customer, and offers nothing
    /// that the server would refuse. When the office puts the visit back
    /// the video goes out, and the phone says SENT ✓ again.
    func testAVideoHeldForTheOfficeReadsSentToOfficeUntilItIsLetGo() {
        launchSignedIn()
        recordAndSave(for: Self.smith)
        waitForHeld()
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForLabel(videoRow(Self.smith), "the video's row", contains: "Sent ✓")

        server.changeRoute(remove: [Self.smith])
        XCTAssertEqual(server.emailDue(customerId: Self.smith), "no_email", "The fake server did not hold the video.")
        XCTAssertTrue(server.state().videos.first?.withOffice ?? false)
        leaveAndComeBack()

        waitForLabel(videoRow(Self.smith), "the video's row", contains: "Sent To Office", timeout: 30)
        let row = label(of: videoRow(Self.smith))
        XCTAssertFalse(row.contains("Sent ✓"), "It has not gone to the customer: \(row)")
        XCTAssertFalse(row.contains("Change Video"), "The office has it; he cannot change it now: \(row)")
        XCTAssertTrue(row.contains("Smith, Jane"), "It is still under the customer's name: \(row)")

        tap(videoRow(Self.smith), "the video's row")
        waitForBar("Video")
        waitForText("The office is looking at this video. Call the office to change it.", timeout: 10)
        XCTAssertFalse(hasText("went to the customer"), "No screen says it went to the customer.")
        XCTAssertFalse(hasText("goes to the customer"))
        XCTAssertFalse(button("RECORD AGAIN").exists || button("DELETE VIDEO").exists, "Nothing is offered that the server would refuse.")
        tap(button("BACK"), "BACK")

        // The office puts the visit back: the server sends the video.
        server.changeRoute(restore: [Self.smith])
        leaveAndComeBack()
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForLabel(videoRow(Self.smith), "the video's row", contains: "Sent ✓", timeout: 30)
        XCTAssertFalse(label(of: videoRow(Self.smith)).contains("Sent To Office"), "The row says: \(label(of: videoRow(Self.smith)))")
        tap(app.buttons["tab-route"], "the My Route tab")
        waitFor(stop(Self.smith), "the stop put back", timeout: 30)
        XCTAssertFalse(label(of: stop(Self.smith)).contains("Record Video"), "It has its video: \(label(of: stop(Self.smith)))")

        let video = theOnlyVideo()
        XCTAssertEqual(video.registers, 1, "The phone sent it once and never again: \(video.summary)")
        XCTAssertEqual(video.deletes, 0)
        XCTAssertEqual(video.state, "sent")
    }

    // MARK: Record again at a stop that has left the list

    /// His video is ten minutes from going to the customer when the office
    /// gives the stop to another tech. RECORD AGAIN on that video is still
    /// for the same customer, and the new clip replaces the old one.
    /// (It used to turn into a "not on my list" video here: the old clip
    /// went to the customer and the better one to the office.)
    func testRecordAgainAfterTheStopLeftTheListReplacesTheVideoForTheSameCustomer() {
        launchSignedIn()
        recordAndSave(for: Self.smith)
        waitForHeld()

        server.changeRoute(moveToOtherTech: [Self.smith])
        leaveAndComeBack()
        waitUntil("the stop has left his list", timeout: 30) { !self.stop(Self.smith).exists }

        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForLabel(videoRow(Self.smith), "the video's row", contains: "Change Video")
        tap(videoRow(Self.smith), "the video's row")
        waitForBar("Video")
        tap(button("RECORD AGAIN"), "RECORD AGAIN")
        waitFor(app.buttons["record-button"], "the record screen")
        XCTAssertTrue(hasText("Smith, Jane"), "The camera screen says whose stop it is.")
        XCTAssertFalse(hasText("Not on my list"), "It is not turned into a video with no customer.")
        recordClip()
        XCTAssertTrue(hasText("Smith, Jane"), "\"Save this video?\" names the same customer.")
        saveClip()

        waitUntil("the new video is confirmed and the old one is gone", timeout: 90) {
            let videos = self.server.state().videos
            return videos.count == 2 && videos[1].isHeld && videos[0].state != "held"
        }
        let videos = server.state().videos
        XCTAssertEqual(videos[1].customerId, Self.smith, "The second video is for the same customer: \(videos[1].summary)")
        XCTAssertFalse(videos[1].notOnMyList)
        XCTAssertTrue(["replaced", "deleted"].contains(videos[0].state), "The first no longer counts: \(videos[0].summary)")
        expectNoOtherCustomer(than: Self.smith)
        tap(app.buttons["tab-videos"], "the My Videos tab")
        XCTAssertEqual(
            app.buttons.matching(NSPredicate(format: "identifier BEGINSWITH %@", "video-row-")).count, 1,
            "My Videos lists the new video only."
        )
    }

    // MARK: A video the server will not take at all

    /// The server says a plain no to a video, and goes on saying it. The
    /// clip stays on the phone, the row says to call the office, and the
    /// app does not ask again every few seconds. When the server takes it
    /// (the next time the app is opened here), it goes, once.
    func testAVideoTheServerRefusesStaysOnThePhoneAndSaysCallTheOffice() {
        launchSignedIn()
        server.set(["refuseRegisters": true])
        tap(app.buttons["not-on-my-list"], "STOP NOT ON MY LIST")
        waitForTitle("Not on my list")
        tap(button("RECORD"), "RECORD")
        waitFor(app.buttons["record-button"], "the red record button")
        recordClip()
        saveClip()

        waitForText("This video could not be sent. It is safe on this phone. Please call the office.", timeout: 30)
        XCTAssertTrue(server.state().videos.isEmpty, "The server took nothing.")
        // Asked at SAVE, and once more if the app came back to the front
        // after the phone's own question about notifications. Then no more.
        let refusals = server.state().registerTries.count
        XCTAssertTrue((1...2).contains(refusals), "All register calls: \(registerLog)")
        expectNoMoreRegisters(than: refusals, during: 12)

        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForLabel(officeRow, "the video's row", contains: "Not Sent · Call Office", timeout: 20)
        waitForLabel(element("keep-app-warning"), "the warning at the top of My Videos", contains: "1 video is still on this phone", timeout: 10)
        tap(officeRow, "the video's row")
        waitForBar("Video")
        waitForLabel(element("stuck-note"), "the line about the video", contains: "It is safe on this phone.", timeout: 10)
        XCTAssertFalse(button("DELETE VIDEO").exists, "Nothing on this screen throws the clip away.")
        XCTAssertFalse(hasText("sends by itself"), "It will not send by itself, and the screen does not say it will.")
        tap(button("BACK"), "BACK")

        // The server is put right. Opening the app tries again, at once.
        server.set(["refuseRegisters": false])
        leaveAndComeBack()
        waitUntil("the office has the video", timeout: 90) { self.server.state().videos.first?.isWithTheOffice ?? false }
        let video = theOnlyVideo()
        XCTAssertEqual(video.registers, 1)
        XCTAssertEqual(video.puts, ["200"], "The clip was still on the phone, whole: \(video.summary)")
        let answers = server.state().registerTries.map { $0.answer }
        XCTAssertEqual(answers, Array(repeating: "400", count: refusals) + ["200"], "One more asking, and it was taken: \(registerLog)")
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForLabel(officeRow, "the video's row", contains: "Sent To Office", timeout: 30)
        XCTAssertFalse(element("keep-app-warning").exists)
    }

    // MARK: The tech does not follow the route order

    /// The list is not the route order and nothing depends on it. He skips
    /// the stop the list puts first, films the next one, then comes back to
    /// the one he skipped. Each video is for the stop he picked, the
    /// numbers on the cards never move, and a stop he has filmed can be
    /// gone back to.
    func testStopsCanBeFilmedInAnyOrderAndTheNumbersStayPut() {
        // (The speech check "hears" the customer he is filming.)
        launchSignedIn(transcript: "Hi Robert. Here's what I did. I closed the backyard gate.")
        waitForLocation()
        XCTAssertEqual([Self.smith, Self.johnson, Self.garcia, Self.business].map { number(onCard: $0) }, [2, 3, 4, 5])
        XCTAssertTrue(label(of: stop(Self.smith)).contains("Closest stop"), "The list puts Smith (stop 2) first.")

        // Stop 3 before stop 2.
        recordAndSave(for: Self.johnson)
        waitForHeld()
        waitForLabel(stop(Self.johnson), "Johnson's card", contains: "Change Video")
        XCTAssertNil(number(onCard: Self.johnson), "A filmed stop shows its state, not its number.")
        XCTAssertEqual([Self.smith, Self.garcia, Self.business].map { number(onCard: $0) }, [2, 4, 5], "The others keep their numbers.")
        XCTAssertTrue(label(of: stop(Self.smith)).contains("Record Video"), "The skipped stop is still open: \(label(of: stop(Self.smith)))")

        // Back to the one he skipped.
        kill()
        hear(Self.smithWords)
        relaunch()
        waitForRoute()
        recordAndSave(for: Self.smith)
        waitForHeld(count: 2)

        let videos = server.state().videos
        XCTAssertEqual(videos.map { $0.customerId }, [Self.johnson, Self.smith], "Filmed in his order, each for its own customer.")
        XCTAssertEqual(videos[0].payload["appointmentIds"] as? [Int], [2020, 2021], "Johnson's appointments went with Johnson's video.")
        XCTAssertEqual(videos[1].payload["appointmentIds"] as? [Int], [2010, 2011])
        XCTAssertTrue(videos.allSatisfy { !$0.override }, "Neither needed a warning: out of order is not a fault.")
        waitForLabel(element("route-header"), "the header", contains: "Sent 3 of 5", timeout: 40)
        XCTAssertEqual([Self.garcia, Self.business].map { number(onCard: $0) }, [4, 5])

        // Going back to a stop he has filmed: its video, not a new recording.
        tap(stop(Self.johnson), "Johnson's card again")
        waitForBar("Video")
        XCTAssertTrue(hasText("Johnson, Robert"))
        XCTAssertTrue(button("RECORD AGAIN").exists && button("DELETE VIDEO").exists, "Inside its ten minutes he can still change it.")
        tap(button("BACK"), "BACK")
        waitForBar("My Route")
        XCTAssertEqual(server.state().videos.count, 2, "Looking at it sends nothing more.")
    }
}
