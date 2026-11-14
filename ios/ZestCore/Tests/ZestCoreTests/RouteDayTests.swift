import XCTest
@testable import ZestCore

/// The office changes the route during the day, and the tech does not
/// drive it in the planned order. What the phone does about each.
final class RouteDayTests: XCTestCase {
    private let now = Fixtures.base

    private func run(_ item: QueueItem, _ events: QueueEvent...) -> QueueItem {
        events.reduce(item) { QueueMachine.reduce($0, $1, now: now) }
    }

    private let refusal = "That stop is not on the list for that day. Use Not On My List."

    // MARK: A video for a stop the office took off the list

    func testTheServersAnswersToRegister() {
        XCTAssertEqual(ServerSays.afterFailedRegister(status: 409, code: "visit_has_video"), .stopLocked)
        XCTAssertEqual(ServerSays.afterFailedRegister(status: 400, code: "bad_request"), .refused)
        // Whatever word comes with a 400, it is a plain no.
        XCTAssertEqual(ServerSays.afterFailedRegister(status: 400, code: ""), .refused)
        // Everything else is the usual handling: nothing here gives up on it.
        for (status, code) in [(401, "unauthorized"), (426, "update_required"), (429, "slow_down"), (500, "server_error"),
                               (503, ""), (404, "bad_request"), (409, "too_late"), (403, "")] {
            XCTAssertEqual(ServerSays.afterFailedRegister(status: status, code: code), .other, "\(status) \(code)")
        }
        XCTAssertEqual(ServerSays.afterFailedRegister(status: nil, code: ""), .other, "no answer at all")
    }

    func testAVideoForAStopThatLeftTheListGoesToTheOfficeInstead() {
        var item = run(Fixtures.item(), .registering, .refused(message: refusal))

        // Same video, same file, still on the phone, ready to go at once.
        XCTAssertEqual(item.id, Fixtures.item().id)
        XCTAssertEqual(item.phase, .queued)
        XCTAssertTrue(item.isWaiting)
        XCTAssertTrue(item.needsFile, "the file is kept")
        XCTAssertTrue(item.mayRetry(now: now), "no wait: the next call goes now")
        XCTAssertNil(item.notBefore)
        XCTAssertFalse(item.isStuck)

        // It is now a video for the office.
        XCTAssertTrue(item.notOnMyList)
        XCTAssertEqual(item.leftTheList, true)
        XCTAssertTrue(item.goesToOffice)
        let request = item.registerRequest
        XCTAssertEqual(request.id, item.id)
        XCTAssertTrue(request.notOnMyList)
        XCTAssertNil(request.customerId, "the contract: no customer on a not-on-my-list video")
        XCTAssertEqual(request.appointmentIds, [])
        XCTAssertEqual(request.spokenText, "This is the Quillfeather home", "what he said still goes with it")

        // The phone still knows who he picked: for the row, and the audit trail.
        XCTAssertEqual(item.customerId, 2)
        XCTAssertEqual(item.customerName, "Jordan Quillfeather")

        // And it ends as every not-on-my-list video does.
        item = run(item, .registering, .registered(Fixtures.ticket), .uploadFinished(status: 200),
                   .completed(state: .unmatched, emailAt: nil))
        XCTAssertEqual(item.phase, .done)
        XCTAssertEqual(item.displayText, "SENT TO OFFICE")
        XCTAssertNil(item.refusedMessage)
    }

    func testItNoLongerCountsAsThatStopsVideo() {
        let moved = run(Fixtures.item(), .registering, .refused(message: refusal))
        // If the office puts the stop back, its card is open again: the
        // customer has no video, the office has one to match.
        XCTAssertNil([moved].liveItem(customerId: 2, day: "2026-11-13"))
        XCTAssertEqual([moved].customersWithVideo(day: "2026-11-13"), [])
        XCTAssertEqual([moved].waitingCount, 1, "it is still a video waiting on this phone")
    }

    func testARefusalDuringAFreshAddressIsTheSame() {
        // Registered once, then the address ran out; the stop left the
        // list before the second register.
        let item = run(Fixtures.item(), .registering, .registered(Fixtures.ticket), .registering, .refused(message: refusal))
        XCTAssertEqual(item.phase, .queued)
        XCTAssertNil(item.uploadURL, "the old address is not used for a video that changed")
        XCTAssertTrue(item.notOnMyList)
    }

    func testAVideoTheServerWillNotTakeAtAllStaysOnThePhoneAndSaysSo() {
        let words = "This video is too old to send."
        // Already for the office (or just moved there), and refused again.
        var item = run(Fixtures.item(), .registering, .refused(message: refusal), .registering, .refused(message: words))
        XCTAssertEqual(item.phase, .queued, "never dropped, never blocked: blocked rows lose their file")
        XCTAssertTrue(item.needsFile)
        XCTAssertTrue(item.isStuck)
        XCTAssertEqual(item.refusedMessage, words)
        XCTAssertEqual(item.displayText, "NOT SENT · CALL OFFICE")
        XCTAssertEqual(item.display, .waiting)

        // It does not ask again every few seconds, or every five minutes.
        XCTAssertFalse(item.mayRetry(now: now.addingTimeInterval(59 * 60)))
        XCTAssertTrue(item.mayRetry(now: now.addingTimeInterval(60 * 60)))
        XCTAssertNotEqual(item.waitingForSignal, true, "the videos behind it are not held up: there is signal")

        // Opening the app tries once more.
        item = run(item, .wake)
        XCTAssertTrue(item.mayRetry(now: now))
        XCTAssertTrue(item.isStuck, "and it still says so until the server takes it")

        // The day the server takes it, it is an ordinary video again.
        item = run(item, .registering, .registered(Fixtures.ticket))
        XCTAssertFalse(item.isStuck)
        XCTAssertEqual(item.displayText, "WAITING")
        item = run(item, .uploadFinished(status: 200), .completed(state: .unmatched, emailAt: nil))
        XCTAssertEqual(item.displayText, "SENT TO OFFICE")
    }

    func testAVideoFilmedAsNotOnMyListThatIsRefusedIsNotTurnedIntoAnythingElse() {
        let item = run(Fixtures.item(customerId: nil, notOnMyList: true), .registering, .refused(message: "No."))
        XCTAssertTrue(item.notOnMyList)
        XCTAssertNil(item.leftTheList)
        XCTAssertTrue(item.isStuck)
        XCTAssertEqual(item.phase, .queued)
    }

    func testARefusalNeverTouchesAVideoTheServerAlreadyHas() {
        let done = run(Fixtures.item(), .registering, .registered(Fixtures.ticket), .uploadFinished(status: 200),
                       .completed(state: .held, emailAt: nil))
        XCTAssertEqual(run(done, .refused(message: refusal)), done)
        let uploaded = run(Fixtures.item(), .registering, .registered(Fixtures.ticket), .uploadFinished(status: 200))
        XCTAssertEqual(run(uploaded, .refused(message: refusal)), uploaded, "the bucket has the file: complete decides")
        let deleting = run(Fixtures.item(), .registering, .deletedByTech)
        XCTAssertEqual(run(deleting, .refused(message: refusal)), deleting)
    }

    func testOldRowsOnDiskStillLoad() throws {
        // A row written before these two fields existed.
        let data = try JSONEncoder().encode(Fixtures.item())
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        object.removeValue(forKey: "leftTheList")
        object.removeValue(forKey: "refusedMessage")
        let old = try JSONDecoder().decode(QueueItem.self, from: JSONSerialization.data(withJSONObject: object))
        XCTAssertNil(old.leftTheList)
        XCTAssertFalse(old.isStuck)
        // And a moved row survives a restart of the app.
        let moved = run(Fixtures.item(), .registering, .refused(message: refusal))
        let again = try JSONDecoder().decode(QueueItem.self, from: JSONEncoder().encode(moved))
        XCTAssertEqual(again.leftTheList, true)
        XCTAssertTrue(again.notOnMyList)
    }

    func testTheWords() {
        XCTAssertEqual(VideoWords.stopLeftList, "This stop is no longer on your list. You can still record it.")
        XCTAssertEqual(
            VideoWords.wentToOfficeInstead(name: "Quillfeather, Jordan"),
            "Quillfeather, Jordan is no longer on your list. The video goes to the office."
        )
        XCTAssertEqual(VideoWords.wentToOfficeInstead(name: "  "), "That stop is no longer on your list. The video goes to the office.")
        XCTAssertEqual(VideoWords.stuck, "This video could not be sent. It is safe on this phone. Please call the office.")
        for line in [VideoWords.stopLeftList, VideoWords.wentToOfficeInstead(name: "Maple, Avery"), VideoWords.stuck] {
            XCTAssertFalse(line.contains("?"), "a statement, not a second question: \(line)")
            for jargon in ["server", "register", "upload", "400", "error", "unmatched", "sync"] {
                XCTAssertFalse(line.lowercased().contains(jargon), "\(jargon) in: \(line)")
            }
        }
    }

    // MARK: A video the server is holding for the office (`withOffice`)

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try ZestJSON.decoder().decode(type, from: Data(json.utf8))
    }

    func testWithOfficeIsReadFromBothVideoObjectsAndIsOptional() throws {
        // Present and true only while the server holds the video.
        let onStop = try decode(StopVideo.self, #"{"id": "v1", "state": "no_email", "byYou": true, "withOffice": true}"#)
        XCTAssertEqual(onStop.withOffice, true)
        XCTAssertTrue(onStop.isWithOffice)
        let listed = try decode(TodayVideo.self, #"{"id": "v1", "customerId": 2, "state": "no_email", "emailAt": null, "withOffice": true}"#)
        XCTAssertTrue(listed.isWithOffice)

        // An older server never sends it: nothing breaks, nothing is claimed.
        let old = try decode(StopVideo.self, #"{"id": "v1", "state": "no_email", "byYou": true}"#)
        XCTAssertNil(old.withOffice)
        XCTAssertFalse(old.isWithOffice)
        XCTAssertFalse(try decode(TodayVideo.self, #"{"id": "v1", "customerId": 2, "state": "sent"}"#).isWithOffice)
        // False, null, or not a yes-or-no at all: not held.
        for junk in ["false", "null", "\"yes\""] {
            let video = try decode(TodayVideo.self, #"{"id": "v1", "customerId": 2, "state": "no_email", "withOffice": \#(junk)}"#)
            XCTAssertFalse(video.isWithOffice, "withOffice: \(junk)")
        }
        // A plain "no_email" (no address on file, say) is not "with the office".
        XCTAssertFalse(StopVideo(id: "v", state: .noEmail, byYou: true).isWithOffice)
    }

    func testAHeldVideoReadsSentToOfficeAndNeverSentToTheCustomer() {
        // Filmed for a customer, taken by the server, on its way.
        var item = run(Fixtures.item(), .registering, .registered(Fixtures.ticket), .uploadFinished(status: 200),
                       .completed(state: .held, emailAt: Fixtures.at(10)))
        XCTAssertEqual(item.displayText, "SENT ✓")
        XCTAssertFalse(item.goesToOffice)

        // The office takes the visit off the day; the email time comes; the
        // server holds the video and says so in the day's answer.
        item = run(item, .serverState(.noEmail, emailAt: nil, withOffice: true))
        XCTAssertEqual(item.withOffice, true)
        XCTAssertTrue(item.goesToOffice)
        XCTAssertEqual(item.displayText, "SENT TO OFFICE")
        XCTAssertEqual(item.phase, .done)
        XCTAssertFalse(item.notOnMyList, "it is still that customer's video")
        XCTAssertEqual([item].liveItem(customerId: 2, day: "2026-11-13")?.id, item.id, "and still counts for the stop if it comes back")

        // It stays so for as long as the server says so (it can be hours).
        item = run(item, .serverState(.noEmail, emailAt: nil, withOffice: true))
        XCTAssertEqual(item.displayText, "SENT TO OFFICE")

        // The office sends it (or the stop is back on the route): SENT ✓.
        item = run(item, .serverState(.sent, emailAt: nil))
        XCTAssertNil(item.withOffice)
        XCTAssertEqual(item.displayText, "SENT ✓")

        // Or the office pulls it: the row goes.
        let pulled = run(run(item, .serverState(.noEmail, emailAt: nil, withOffice: true)), .serverState(.pulled, emailAt: nil))
        XCTAssertEqual(pulled.phase, .removed)
    }

    func testAPlainNoEmailVideoStillReadsSent() {
        // No usable address, emails switched off: the tech's part is done,
        // and the server has not said the office is deciding anything.
        let item = run(Fixtures.item(), .registering, .registered(Fixtures.ticket), .uploadFinished(status: 200),
                       .completed(state: .held, emailAt: nil), .serverState(.noEmail, emailAt: nil))
        XCTAssertEqual(item.displayText, "SENT ✓")
        XCTAssertFalse(item.goesToOffice)
    }

    func testAVideoWhoseAnswerWasLostIsHeldForTheOfficeAtOnce() {
        // The upload got there but the phone never heard; the next list
        // says the server has it and is holding it.
        let item = run(Fixtures.item(), .registering, .registered(Fixtures.ticket), .uploadFinished(status: 200),
                       .serverState(.noEmail, emailAt: nil, withOffice: true))
        XCTAssertEqual(item.phase, .done)
        XCTAssertEqual(item.displayText, "SENT TO OFFICE")
        // A video still waiting on the phone is not touched by the flag alone.
        let waiting = run(Fixtures.item(), .serverState(.awaitingUpload, emailAt: nil, withOffice: true))
        XCTAssertEqual(waiting.displayText, "WAITING")
        XCTAssertNil(waiting.withOffice)
    }

    func testTheStopsCoverCarriesTheFlagFromEitherList() {
        var stop = Fixtures.quill
        // On the stop's own video object.
        stop.video = StopVideo(id: "v1", state: .noEmail, byYou: true, withOffice: true)
        XCTAssertEqual(StopCover.of(stop).video?.isWithOffice, true)
        XCTAssertEqual(StopCover.of(stop).standing(now: now), .locked, "he cannot change it: the office has it")
        // Only on the list of his videos.
        stop.video = StopVideo(id: "v1", state: .noEmail, byYou: true)
        let mine = [TodayVideo(id: "v1", customerId: 2, state: .noEmail, emailAt: nil, withOffice: true)]
        XCTAssertEqual(StopCover.of(stop, ownVideos: mine).video?.isWithOffice, true)
        // The stop shows a teammate's video, he has his own, and his is held.
        stop.video = StopVideo(id: "team", state: .sent, byYou: false)
        XCTAssertEqual(StopCover.of(stop, ownVideos: mine).video?.isWithOffice, true)
        XCTAssertTrue(StopCover.of(stop, ownVideos: mine).isMine)
        // A teammate's video the office holds is not his to be told about.
        stop.video = StopVideo(id: "team", state: .noEmail, byYou: false, withOffice: true)
        XCTAssertEqual(StopCover.of(stop).standing(now: now), .teamCovered)
    }

    func testTheWordsForAHeldVideo() {
        XCTAssertEqual(VideoWords.officeDeciding, "The office is looking at this video. Call the office to change it.")
        XCTAssertEqual(VideoWords.locked(toOffice: true, deciding: true), VideoWords.officeDeciding)
        // The customer is known: nothing is said about matching, and nothing says it went out.
        XCTAssertFalse(VideoWords.officeDeciding.contains("match"))
        XCTAssertFalse(VideoWords.officeDeciding.contains("went to the customer"))
        // The other two lines are as they were.
        XCTAssertEqual(VideoWords.locked(toOffice: false), "This video already went to the customer. Call the office to change it.")
        XCTAssertTrue(VideoWords.locked(toOffice: true).contains("They will match it to the customer."))
    }

    // MARK: A stop that is no longer on the list

    func testHasTheOpenStopLeftTheList() {
        XCTAssertFalse(RouteList.hasLeft(customerId: 2, list: Fixtures.route))
        XCTAssertTrue(RouteList.hasLeft(customerId: 2, list: Fixtures.route.filter { $0.customerId != 2 }))
        XCTAssertTrue(RouteList.hasLeft(customerId: 2, list: []), "the office emptied his day")
        // Taken off and put back: on the list again, whatever else changed.
        var back = Fixtures.quill
        back.order = 9
        back.checkIn = Fixtures.at(-5)
        XCTAssertFalse(RouteList.hasLeft(customerId: 2, list: [Fixtures.maple, back]))
        // Nothing is claimed without a list, or about a video with no customer.
        XCTAssertFalse(RouteList.hasLeft(customerId: 2, list: nil))
        XCTAssertFalse(RouteList.hasLeft(customerId: nil, list: Fixtures.route))
    }

    // MARK: The numbers on the cards

    func testTheNumberIsTheStopsPlaceOnHisOwnRoute() {
        // The server counts every tech's stops together: his are 3, 7, 12, 20.
        let mine = [
            Fixtures.stop(41, "A One", last: "One", "1 First St", order: 12),
            Fixtures.stop(42, "B Two", last: "Two", "2 First St", order: 3),
            Fixtures.stop(43, "C Three", last: "Three", "3 First St", order: 20),
            Fixtures.stop(44, "D Four", last: "Four", "4 First St", order: 7),
        ]
        XCTAssertEqual(RouteNumbers.numbers(for: mine), [42: 1, 44: 2, 41: 3, 43: 4])
        XCTAssertEqual(RouteNumbers.number(of: 41, in: mine), 3)
        XCTAssertEqual(RouteNumbers.inRouteOrder(mine).map { $0.customerId }, [42, 44, 41, 43])
        // The order the list arrives in changes nothing.
        XCTAssertEqual(RouteNumbers.numbers(for: mine.reversed()), RouteNumbers.numbers(for: mine))
        XCTAssertEqual(RouteNumbers.numbers(for: []), [:])
    }

    func testFilmingCheckingInOrFinishingAStopNeverChangesANumber() {
        let before = RouteNumbers.numbers(for: Fixtures.route)
        XCTAssertEqual(before, [1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6])
        // He does the last stop first, and films the fourth.
        var route = Fixtures.route
        route[5].done = true
        route[5].checkIn = Fixtures.at(-30)
        route[5].checkOut = Fixtures.at(-10)
        route[3].video = StopVideo(id: "v4", state: .held, byYou: true)
        route[4].checkIn = Fixtures.at(-2)
        XCTAssertEqual(RouteNumbers.numbers(for: route), before)
    }

    func testTheNumbersCloseUpWhenTheOfficeTakesAStopOff() {
        // Stop 3 leaves: 4, 5 and 6 become 3, 4 and 5. No gap, as in FieldRoutes.
        let without = Fixtures.route.filter { $0.customerId != 3 }
        XCTAssertEqual(RouteNumbers.numbers(for: without), [1: 1, 2: 2, 4: 3, 5: 4, 6: 5])
        // Put back: every stop has its morning number again.
        XCTAssertEqual(RouteNumbers.numbers(for: without + [Fixtures.ostrander]), RouteNumbers.numbers(for: Fixtures.route))
        // A stop added in the middle pushes the later ones down by one.
        let extra = Fixtures.stop(9, "Morgan Tidewater", last: "Tidewater", "14 Quarry Bend", order: 2)
        XCTAssertEqual(RouteNumbers.numbers(for: Fixtures.route + [extra]), [1: 1, 2: 2, 9: 3, 3: 4, 4: 5, 5: 6, 6: 7])
    }

    func testAStopThatIsNotOnTheListStillGetsANumberToDraw() {
        XCTAssertEqual(RouteNumbers.number(of: 99, in: Fixtures.route), 1)
    }

    // MARK: The tech does not follow the route order

    private func fix(at stop: Stop) -> VideoLocation {
        VideoLocation(lat: stop.lat!, lng: stop.lng!, accuracyM: 8)
    }

    private func ids(_ ranked: [StopRanker.RankedStop]) -> [Int] { ranked.map { $0.stop.customerId } }

    func testDoingTheLastStopFirst() {
        // He starts the day at stop 5 of 6 (the last one with a map pin).
        let result = StopRanker.rank(stops: Fixtures.route, fix: fix(at: Fixtures.business), now: now)
        XCTAssertEqual(result.suggested.first?.stop.customerId, 5, "where he stands is on top, whatever its number")
        XCTAssertEqual(result.reason, .nearest)
        XCTAssertEqual(RouteNumbers.number(of: 5, in: Fixtures.route), 5, "and its card still says 5")
        // Every stop is on the list exactly once.
        XCTAssertEqual(Set(ids(result.all)), Set(1...6))
        XCTAssertEqual(result.all.count, 6)
    }

    func testWorkingTheWholeRouteBackToFront() {
        // At each stop in turn, back to front, with the ones behind him
        // filmed: the stop he is at is always the first card.
        var filmed = Set<Int>()
        for stop in Fixtures.route.filter({ $0.coordinate != nil }).reversed() {
            let result = StopRanker.rank(stops: Fixtures.route, fix: fix(at: stop), now: now, haveVideo: filmed)
            XCTAssertEqual(result.suggested.first?.stop.customerId, stop.customerId, "at stop \(stop.customerId)")
            XCTAssertFalse(result.suggested.contains { filmed.contains($0.stop.customerId) }, "a filmed stop is never suggested")
            filmed.insert(stop.customerId)
        }
        XCTAssertEqual(filmed, [1, 2, 3, 4, 5])
    }

    func testSkippingAStopAndComingBack() {
        // He films 1, skips 2, films 3, 4 and 5, then drives back to 2.
        let filmed: Set<Int> = [1, 3, 4, 5]
        // While he is away at stop 5, the skipped stop is still on the
        // list, still open, with its own number.
        let away = StopRanker.rank(stops: Fixtures.route, fix: fix(at: Fixtures.business), now: now, haveVideo: filmed)
        XCTAssertTrue(ids(away.all).contains(2))
        XCTAssertEqual(ids(away.suggested).filter { filmed.contains($0) }, [], "stops with a video are below")
        XCTAssertEqual(ids(away.suggested), [2, 6], "the only two left to film")
        XCTAssertEqual(ids(away.rest), [1, 3, 4, 5], "the filmed ones, in route order")
        // Back at stop 2: it is first. (Its neighbour, 11 metres away and
        // already filmed, is not offered in its place.)
        let back = StopRanker.rank(stops: Fixtures.route, fix: fix(at: Fixtures.quill), now: now, haveVideo: filmed)
        XCTAssertEqual(back.suggested.first?.stop.customerId, 2)
        XCTAssertEqual(back.reason, .nearest)
    }

    func testOutOfOrderWithNoLocationHisCheckInOrCompletionPutsTheStopFirst() {
        // No location. He is at stop 5, out of order, and has checked in there.
        var route = Fixtures.route
        route[4].checkIn = Fixtures.at(-3)
        var result = StopRanker.rank(stops: route, fix: nil, now: now)
        XCTAssertEqual(result.suggested.first?.stop.customerId, 5)
        XCTAssertEqual(result.reason, .checkedIn)

        // Or he completed it in FieldRoutes a few minutes ago and films now.
        route = Fixtures.route
        route[4].checkIn = Fixtures.at(-25)
        route[4].checkOut = Fixtures.at(-4)
        route[4].done = true
        result = StopRanker.rank(stops: route, fix: nil, now: now)
        XCTAssertEqual(result.suggested.first?.stop.customerId, 5)
        XCTAssertEqual(result.reason, .justFinished)

        // With neither, the phone cannot know: the planned order is all it
        // has, and the stop he is at is further down, never missing.
        result = StopRanker.rank(stops: Fixtures.route, fix: nil, now: now)
        XCTAssertEqual(result.reason, .routeOrder)
        XCTAssertEqual(ids(result.suggested), [1, 2, 3])
        XCTAssertTrue(ids(result.rest).contains(5))
    }

    func testGoingBackToAStopThatIsFinished() {
        // Stop 1 was completed three hours ago and never filmed; he is
        // standing at it again.
        var route = Fixtures.route
        route[0].done = true
        route[0].checkIn = Fixtures.at(-200)
        route[0].checkOut = Fixtures.at(-180)
        let there = StopRanker.rank(stops: route, fix: fix(at: Fixtures.maple), now: now)
        XCTAssertEqual(there.suggested.first?.stop.customerId, 1, "a finished stop with no video can still be filmed, and is first when he is there")
        // With no location it is at the bottom of the open stops, not gone.
        let blind = StopRanker.rank(stops: route, fix: nil, now: now)
        XCTAssertEqual(ids(blind.all).last, 1)
        // Once it has a video it is never suggested again, wherever he stands.
        let filmed = StopRanker.rank(stops: route, fix: fix(at: Fixtures.maple), now: now, haveVideo: [1])
        XCTAssertFalse(ids(filmed.suggested).contains(1))
        XCTAssertTrue(ids(filmed.rest).contains(1), "but its card is still there to open")
    }

    // MARK: Two stops at one address

    /// Two customers with the same map pin: a duplex, or a home and the
    /// HOA strip in front of it.
    private var upstairs: Stop { Fixtures.stop(71, "Dana Whitlock", last: "Whitlock", "60 Ferry Rd Unit A", lat: 38.62, lng: -121.32, order: 4) }
    private var downstairs: Stop { Fixtures.stop(72, "Lee Abernathy", last: "Abernathy", "60 Ferry Rd Unit B", lat: 38.62, lng: -121.32, order: 2) }

    func testTwoStopsAtOneAddressAreBothOfferedAndNeitherHidesTheOther() {
        let route = [Fixtures.maple, upstairs, downstairs]
        let here = fix(at: upstairs)
        var result = StopRanker.rank(stops: route, fix: here, now: now)
        // Both at 0 metres: the planned order breaks the tie, the same way every time.
        XCTAssertEqual(ids(result.suggested), [72, 71, 1])
        XCTAssertEqual(result.suggested[0].distanceMeters, result.suggested[1].distanceMeters)
        XCTAssertEqual(StopRanker.rank(stops: route.reversed(), fix: here, now: now).suggested.map { $0.stop.customerId }, [72, 71, 1])

        // One filmed: the other is first, and still open.
        result = StopRanker.rank(stops: route, fix: here, now: now, haveVideo: [72])
        XCTAssertEqual(result.suggested.first?.stop.customerId, 71)
        result = StopRanker.rank(stops: route, fix: here, now: now, haveVideo: [71])
        XCTAssertEqual(result.suggested.first?.stop.customerId, 72)
        // Two cards, two numbers.
        XCTAssertEqual(RouteNumbers.numbers(for: route), [1: 1, 72: 2, 71: 3])
    }

    func testAtOneAddressTheNameHeSaysTellsTheTwoApart() {
        let route = [Fixtures.maple, upstairs, downstairs]
        let here = fix(at: upstairs)
        // GPS cannot tell them apart: it matches both.
        XCTAssertEqual(TwoKeys.gpsSignal(fix: here, stop: upstairs, farMeters: 150), .match)
        XCTAssertEqual(TwoKeys.gpsSignal(fix: here, stop: downstairs, farMeters: 150), .match)

        // He picked Whitlock's card and says Whitlock: clean.
        var outcome = TwoKeys.signals(
            transcript: "Hi Dana, this is the Whitlock home, here is what I did", fix: here, recordedAt: now,
            stop: upstairs, all: route, farMeters: 150
        )
        XCTAssertEqual(outcome.signals.spoken, .match)
        XCTAssertTrue(TwoKeys.verdict(outcome.signals, stop: upstairs, spokenMiss: outcome.spokenMiss).clean)

        // He picked Whitlock's card and says Abernathy: never clean, though
        // the pin matches, and the app offers the other customer.
        outcome = TwoKeys.signals(
            transcript: "Hi Lee, this is the Abernathy home, here is what I did", fix: here, recordedAt: now,
            stop: upstairs, all: route, farMeters: 150
        )
        XCTAssertEqual(outcome.signals.gps, .match)
        XCTAssertEqual(outcome.signals.spoken, .mismatch)
        XCTAssertEqual(outcome.spokenMiss, .otherStop)
        XCTAssertEqual(outcome.better?.customerId, 72)
        XCTAssertFalse(TwoKeys.verdict(outcome.signals, stop: upstairs, spokenMiss: outcome.spokenMiss).clean)
    }
}
