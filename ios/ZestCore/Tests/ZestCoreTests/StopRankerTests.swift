import XCTest
@testable import ZestCore

final class StopRankerTests: XCTestCase {
    func testGeoDistance() {
        let a = Coordinate(lat: 38.6000, lng: -121.3000)
        XCTAssertEqual(Geo.distanceMeters(a, a), 0, accuracy: 0.001)
        // One thousandth of a degree of latitude is about 111 m.
        XCTAssertEqual(Geo.distanceMeters(a, Coordinate(lat: 38.6010, lng: -121.3000)), 111.2, accuracy: 1)
        XCTAssertEqual(Geo.distanceText(meters: 30), "100 ft away")
        XCTAssertEqual(Geo.distanceText(meters: 2000), "1.2 miles away")
        XCTAssertEqual(Geo.distanceText(meters: 40_000), "25 miles away")
    }

    func testOpenCheckInGoesOnTop() {
        var route = Fixtures.route
        route[3].checkIn = Fixtures.at(-5)   // stop 4 is checked in, not out
        // The phone is standing right on stop 1, but the check-in wins.
        let result = StopRanker.rank(stops: route, location: Fixtures.maple.coordinate)
        XCTAssertEqual(result.reason, .checkedIn)
        XCTAssertEqual(result.suggested.map { $0.id }, [4, 1, 2])
    }

    func testCheckedOutStopIsNotPreselected() {
        var route = Fixtures.route
        route[3].checkIn = Fixtures.at(-30)
        route[3].checkOut = Fixtures.at(-10)
        let result = StopRanker.rank(stops: route, location: Fixtures.maple.coordinate)
        XCTAssertEqual(result.reason, .nearest)
        XCTAssertEqual(result.suggested.first?.id, 1)
    }

    func testThreeNearestByGPSNearestFirst() {
        let here = Coordinate(lat: 38.61005, lng: -121.3100)   // between stops 2 and 3
        let result = StopRanker.rank(stops: Fixtures.route, location: here)
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
        let here = Fixtures.quill.coordinate
        let result = StopRanker.rank(stops: route, location: here, haveVideo: [3])   // stop 3, on this phone
        XCTAssertFalse(result.suggested.contains { $0.id == 2 || $0.id == 3 })
        // They sink to the bottom of the list.
        XCTAssertEqual(result.rest.suffix(2).map { $0.id }, [2, 3])
    }

    func testAPulledVideoNoLongerCounts() {
        var route = Fixtures.route
        route[1].video = StopVideo(id: "v", state: .pulled, byYou: true)
        let result = StopRanker.rank(stops: route, location: Fixtures.quill.coordinate)
        XCTAssertEqual(result.suggested.first?.id, 2)
    }

    func testNoLocationUsesRouteOrderWithNextUnfinishedOnTop() {
        var route = Fixtures.route
        route[0].done = true    // stop 1 already serviced (and no video yet)
        let result = StopRanker.rank(stops: route, location: nil)
        XCTAssertEqual(result.reason, .routeOrder)
        XCTAssertEqual(result.suggested.map { $0.id }, [2, 3, 4])
        XCTAssertTrue(result.suggested.allSatisfy { $0.distanceMeters == nil })
        XCTAssertEqual(result.rest.map { $0.id }, [1, 5, 6])
    }

    func testLocationButNoPinsFallsBackToRouteOrder() {
        let route = [Fixtures.noPin, Fixtures.stop(7, "Kai Ferngully", last: "Ferngully", "8 Thistle Rd", order: 1)]
        let result = StopRanker.rank(stops: route, location: Coordinate(lat: 38, lng: -121))
        XCTAssertEqual(result.reason, .routeOrder)
        XCTAssertEqual(result.suggested.map { $0.id }, [7, 6])
    }

    func testEveryStopAppearsExactlyOnce() {
        let result = StopRanker.rank(stops: Fixtures.route, location: Fixtures.maple.coordinate, haveVideo: [1, 5])
        XCTAssertEqual(result.all.map { $0.id }.sorted(), [1, 2, 3, 4, 5, 6])
        XCTAssertEqual(StopRanker.rank(stops: [], location: nil).all, [])
    }
}
