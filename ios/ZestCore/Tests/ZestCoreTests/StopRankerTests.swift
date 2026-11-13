import XCTest
@testable import ZestCore

final class StopRankerTests: XCTestCase {
    /// The phone standing on a stop's map pin, with an ordinary 8 m fix.
    static func fix(at stop: Stop) -> VideoLocation? {
        stop.coordinate.map { VideoLocation(lat: $0.lat, lng: $0.lng, accuracyM: 8) }
    }

    func testGeoDistance() {
        let a = Coordinate(lat: 38.6000, lng: -121.3000)
        XCTAssertEqual(Geo.distanceMeters(a, a), 0, accuracy: 0.001)
        // One thousandth of a degree of latitude is about 111 m.
        XCTAssertEqual(Geo.distanceMeters(a, Coordinate(lat: 38.6010, lng: -121.3000)), 111.2, accuracy: 1)
        XCTAssertEqual(Geo.distanceText(meters: 30), "100 ft away")
        XCTAssertEqual(Geo.distanceText(meters: 2000), "1.2 miles away")
        XCTAssertEqual(Geo.distanceText(meters: 40_000), "25 miles away")
    }

    func testOpenCheckInWhereThePhoneIsGoesOnTop() {
        var route = Fixtures.route
        route[3].checkIn = Fixtures.at(-5)   // stop 4 is checked in, not out
        // The phone is on stop 4's lawn. Stop 1 comes first in route order.
        let result = StopRanker.rank(stops: route, fix: Self.fix(at: Fixtures.fifth), now: Fixtures.base)
        XCTAssertEqual(result.reason, .checkedIn)
        XCTAssertEqual(result.suggested.first?.id, 4)
    }

    func testCheckedOutStopIsNotPreselected() {
        var route = Fixtures.route
        route[3].checkIn = Fixtures.at(-30)
        route[3].checkOut = Fixtures.at(-10)
        let result = StopRanker.rank(stops: route, fix: Self.fix(at: Fixtures.maple), now: Fixtures.base)
        XCTAssertEqual(result.reason, .nearest)
        XCTAssertEqual(result.suggested.first?.id, 1)
    }

    func testThreeNearestByGPSNearestFirst() {
        let here = VideoLocation(lat: 38.61005, lng: -121.3100, accuracyM: 8)   // between stops 2 and 3
        let result = StopRanker.rank(stops: Fixtures.route, fix: here)
        XCTAssertEqual(result.reason, .nearest)
        XCTAssertEqual(result.suggested.count, 3)
        XCTAssertEqual(Set(result.suggested.prefix(2).map { $0.id }), [2, 3])
        XCTAssertEqual(result.suggested[2].id, 1)
        let distances = result.suggested.compactMap { $0.distanceMeters }
        XCTAssertEqual(distances, distances.sorted())
        // The rest keeps route order; the stop with no pin has no distance.
        XCTAssertEqual(result.rest.map { $0.id }, [4, 5, 6])
        XCTAssertNil(result.rest.last?.distanceMeters)
    }

    func testStopsWithAVideoAreNeverSuggested() {
        var route = Fixtures.route
        route[1].video = StopVideo(id: "v", state: .sent, byYou: true)     // stop 2, on the server
        let result = StopRanker.rank(stops: route, fix: Self.fix(at: Fixtures.quill), haveVideo: [3])   // stop 3, on this phone
        XCTAssertFalse(result.suggested.contains { $0.id == 2 || $0.id == 3 })
        // They sink to the bottom of the list.
        XCTAssertEqual(result.rest.suffix(2).map { $0.id }, [2, 3])
    }

    func testAPulledVideoNoLongerCounts() {
        var route = Fixtures.route
        route[1].video = StopVideo(id: "v", state: .pulled, byYou: true)
        let result = StopRanker.rank(stops: route, fix: Self.fix(at: Fixtures.quill))
        XCTAssertEqual(result.suggested.first?.id, 2)
    }

    func testNoLocationUsesRouteOrderWithNextUnfinishedOnTop() {
        var route = Fixtures.route
        route[0].done = true    // stop 1 already serviced, no check-out time known, no video yet
        let result = StopRanker.rank(stops: route, fix: nil, now: Fixtures.base)
        XCTAssertEqual(result.reason, .routeOrder)
        XCTAssertEqual(result.suggested.map { $0.id }, [2, 3, 4])
        XCTAssertTrue(result.suggested.allSatisfy { $0.distanceMeters == nil })
        // Still on the list, below the stops he has not done yet.
        XCTAssertEqual(result.rest.map { $0.id }, [5, 6, 1])
    }

    // MARK: No usable location: where is he standing?

    /// A route with no location to go by. Stops are numbered in route order.
    private func bare(_ id: Int, done: Bool = false, checkIn: Double? = nil, checkOut: Double? = nil, video: Bool = false) -> Stop {
        Fixtures.stop(
            id, "Customer \(id)", last: "Sample\(id)", "\(id) Test Rd", lat: 38.6 + Double(id) / 100, lng: -121.3, order: id,
            checkIn: checkIn.map { Fixtures.at(-$0) }, checkOut: checkOut.map { Fixtures.at(-$0) }, done: done,
            video: video ? StopVideo(id: "v\(id)", state: .sent, byYou: true) : nil
        )
    }

    private func order(_ stops: [Stop], fix: VideoLocation? = nil) -> [Int] {
        StopRanker.rank(stops: stops, fix: fix, now: Fixtures.base).all.map { $0.id }
    }

    /// What the UI test found. Two of the four techs tap check-in,
    /// check-out and "complete" within a minute at a fifth of their stops
    /// and then work there for about 20 minutes: when he films, the stop
    /// already reads done. With no location it must still come first.
    func testAStopHeJustFinishedAndHasNotFilmedComesFirst() {
        let filmedEarlier = bare(1, done: true, checkIn: 50, checkOut: 40, video: true)   // done, and has its video
        let justDone = bare(2, done: true, checkIn: 2, checkOut: 1)                       // tapped a minute ago, no video
        let route = [filmedEarlier, justDone, bare(3), bare(4), bare(5)]
        let result = StopRanker.rank(stops: route, fix: nil, now: Fixtures.base)
        XCTAssertEqual(result.all.map { $0.id }, [2, 3, 4, 5, 1])
        XCTAssertEqual(result.reason, .justFinished, "and its card does not say NEXT STOP")
        XCTAssertEqual(result.suggested.map { $0.id }, [2, 3, 4])
    }

    /// Every tier, in order: a believable open check-in; finished within
    /// the hour (latest check-out first); not done yet (route order);
    /// finished longer ago (route order); then the stops that have a video.
    func testTheOrderWithNoLocationTierByTier() {
        let hasVideo = bare(1, done: true, checkIn: 200, checkOut: 190, video: true)
        let finishedLongAgo = bare(2, done: true, checkIn: 180, checkOut: 170)
        let finishedLongAgoToo = bare(3, done: true, checkIn: 150, checkOut: 140)
        let finishedHalfAnHourAgo = bare(4, done: true, checkIn: 45, checkOut: 30)
        let finishedFiveMinutesAgo = bare(5, done: true, checkIn: 6, checkOut: 5)
        let checkedInNow = bare(6, checkIn: 3)
        let toDo = bare(7)
        let toDoLater = bare(8)
        let route = [hasVideo, finishedLongAgo, finishedLongAgoToo, finishedHalfAnHourAgo, finishedFiveMinutesAgo, checkedInNow, toDo, toDoLater]
        let result = StopRanker.rank(stops: route, fix: nil, now: Fixtures.base)
        XCTAssertEqual(result.all.map { $0.id }, [6, 5, 4, 7, 8, 2, 3, 1])
        XCTAssertEqual(result.reason, .checkedIn, "the open check-in is first, and says so")
        XCTAssertEqual(result.suggested.map { $0.id }, [6, 5, 4])
        // The same list whatever order the server sends it in.
        XCTAssertEqual(order(route.reversed()), [6, 5, 4, 7, 8, 2, 3, 1])
    }

    func testFinishedCountsAsJustFinishedForSixtyMinutes() {
        XCTAssertEqual(StopRanker.justFinishedSeconds, 60 * 60)
        func tier(_ minutesAgo: Double?) -> StopRanker.NoFixTier {
            StopRanker.noFixTier(of: bare(1, done: true, checkIn: (minutesAgo ?? 0) + 1, checkOut: minutesAgo), now: Fixtures.base)
        }
        XCTAssertEqual(tier(1), .justFinished)
        XCTAssertEqual(tier(59), .justFinished)
        XCTAssertEqual(tier(60), .justFinished, "the hour itself still counts")
        XCTAssertEqual(tier(61), .finishedEarlier)
        XCTAssertEqual(tier(240), .finishedEarlier)
        XCTAssertEqual(tier(-2), .justFinished, "a check-out a little ahead of the phone's clock is still just now")
        // Done, with no check-out time to go by: nothing says he is still there.
        XCTAssertEqual(StopRanker.noFixTier(of: bare(1, done: true), now: Fixtures.base), .finishedEarlier)
        XCTAssertEqual(StopRanker.noFixTier(of: bare(1, done: true, checkIn: 5), now: Fixtures.base), .finishedEarlier)
        // Not completed: still to do, whatever its check-in and check-out say.
        XCTAssertEqual(StopRanker.noFixTier(of: bare(1), now: Fixtures.base), .toDo)
        XCTAssertEqual(StopRanker.noFixTier(of: bare(1, checkIn: 10, checkOut: 5), now: Fixtures.base), .toDo)
        XCTAssertLessThan(StopRanker.NoFixTier.justFinished, .toDo)
        XCTAssertLessThan(StopRanker.NoFixTier.toDo, .finishedEarlier)
    }

    func testTheBoundaryOnTheList() {
        // 59 minutes ago: first. 61 minutes ago: below the stops still to do.
        XCTAssertEqual(order([bare(1, done: true, checkIn: 70, checkOut: 59), bare(2), bare(3)]), [1, 2, 3])
        XCTAssertEqual(order([bare(1, done: true, checkIn: 70, checkOut: 61), bare(2), bare(3)]), [2, 3, 1])
        let late = StopRanker.rank(stops: [bare(1, done: true, checkIn: 70, checkOut: 61), bare(2)], fix: nil, now: Fixtures.base)
        XCTAssertEqual(late.reason, .routeOrder, "the first card is the next stop on the route")
        // Done with no check-out time: below the stops still to do.
        XCTAssertEqual(order([bare(1, done: true), bare(2), bare(3)]), [2, 3, 1])
        // Two finished within the hour: the one he left last comes first.
        XCTAssertEqual(order([bare(1, done: true, checkOut: 5), bare(2, done: true, checkOut: 40), bare(3)]), [1, 2, 3])
        XCTAssertEqual(order([bare(1, done: true, checkOut: 40), bare(2, done: true, checkOut: 5), bare(3)]), [2, 1, 3])
    }

    /// With a usable GPS fix nothing changes: the nearest stops come first,
    /// and a stop finished a minute ago kilometres away is not put on top.
    func testWithALocationTheNearestStillComesFirst() {
        let justDoneFarAway = bare(1, done: true, checkIn: 2, checkOut: 1)     // at 38.61
        let here = bare(5)                                                    // at 38.65
        let route = [justDoneFarAway, bare(2), bare(3), bare(4), here]
        let fix = VideoLocation(lat: 38.65, lng: -121.3, accuracyM: 8)
        let result = StopRanker.rank(stops: route, fix: fix, now: Fixtures.base)
        XCTAssertEqual(result.reason, .nearest)
        XCTAssertEqual(result.suggested.map { $0.id }, [5, 4, 3])
        XCTAssertEqual(result.rest.map { $0.id }, [1, 2], "the rest in route order, as before")
        // And standing on the stop he just finished, GPS puts it first anyway.
        let onIt = StopRanker.rank(stops: route, fix: VideoLocation(lat: 38.61, lng: -121.3, accuracyM: 8), now: Fixtures.base)
        XCTAssertEqual(onIt.suggested.first?.id, 1)
        XCTAssertEqual(onIt.reason, .nearest)
    }

    /// A fix with a two-kilometre error circle is no location at all.
    func testAFixTooFuzzyToUseCountsAsNoLocation() {
        let route = [bare(1, done: true, checkIn: 2, checkOut: 1), bare(2), bare(3)]
        let fuzzy = VideoLocation(lat: 38.63, lng: -121.3, accuracyM: 2000)   // "nearest" to stop 3
        let result = StopRanker.rank(stops: route, fix: fuzzy, now: Fixtures.base)
        XCTAssertEqual(result.reason, .justFinished)
        XCTAssertEqual(result.all.map { $0.id }, [1, 2, 3])
        XCTAssertTrue(result.all.allSatisfy { $0.distanceMeters == nil }, "and no distances are shown from it")
        XCTAssertNil(TwoKeys.usable(fuzzy))
        XCTAssertNil(TwoKeys.usable(VideoLocation(lat: 38.63, lng: -121.3, accuracyM: -1)))
        XCTAssertNotNil(TwoKeys.usable(VideoLocation(lat: 38.63, lng: -121.3, accuracyM: 500)))
        XCTAssertNil(TwoKeys.usable(nil))
    }

    /// The app's practice server's usual day, as the phone gets it with
    /// location refused: 205 done and filmed, 201 checked out a minute ago
    /// and not filmed, 202 to 204 still to do.
    func testThePracticeServersDayWithNoLocation() {
        let route = [
            bare(205, done: true, checkIn: 60, checkOut: 50, video: true),
            bare(201, done: true, checkIn: 10, checkOut: 1),
            bare(202), bare(203), bare(204),
        ].enumerated().map { index, stop -> Stop in
            var copy = stop
            copy.order = index + 1
            return copy
        }
        XCTAssertEqual(order(route), [201, 202, 203, 204, 205])
    }

    func testLocationButNoPinsFallsBackToRouteOrder() {
        let route = [Fixtures.noPin, Fixtures.stop(7, "Kai Ferngully", last: "Ferngully", "8 Thistle Rd", order: 1)]
        let result = StopRanker.rank(stops: route, fix: VideoLocation(lat: 38, lng: -121, accuracyM: 8))
        XCTAssertEqual(result.reason, .routeOrder)
        XCTAssertEqual(result.suggested.map { $0.id }, [7, 6])
    }

    func testEveryStopAppearsExactlyOnce() {
        let result = StopRanker.rank(stops: Fixtures.route, fix: Self.fix(at: Fixtures.maple), haveVideo: [1, 5])
        XCTAssertEqual(result.all.map { $0.id }.sorted(), [1, 2, 3, 4, 5, 6])
        XCTAssertEqual(StopRanker.rank(stops: [], fix: nil).all, [])
    }
}
