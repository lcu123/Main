import XCTest
@testable import ZestCore

/// What the check-in is worth, from three real days of this office's
/// check-ins (106 stops, 4 techs; the figures are in `TwoKeys.swift` and
/// `StopRanker.swift`). Each test below is one of the shapes seen in those
/// days. The people, places and times here are made up.
final class CheckInSignalsTests: XCTestCase {
    private let now = Fixtures.base
    private let far = 150.0

    /// Two houses a long way apart, and two that are next door.
    private func stop(_ id: Int, lat: Double?, lng: Double?, order: Int, checkIn: Date? = nil, checkOut: Date? = nil) -> Stop {
        Fixtures.stop(id, "Customer \(id)", last: "Sample\(id)", "\(id) Test Rd", lat: lat, lng: lng, order: order, checkIn: checkIn, checkOut: checkOut)
    }

    private func here(_ lat: Double, _ lng: Double, accuracy: Double = 8) -> VideoLocation {
        VideoLocation(lat: lat, lng: lng, accuracyM: accuracy)
    }

    private func first(_ stops: [Stop], fix: VideoLocation?) -> (id: Int?, reason: StopRanker.Reason) {
        let result = StopRanker.rank(stops: stops, fix: fix, farMeters: far, now: now)
        return (result.suggested.first?.id, result.reason)
    }

    // MARK: StopRanker: an open check-in is believed only when it can be true now

    /// 6 of 106 stops were left checked in for over 90 minutes (104 to 347).
    /// The tech is kilometres away at another house: that house comes first.
    func testAStopLeftCheckedInForHoursIsNotOfferedFirstWhenThePhoneIsElsewhere() {
        for minutes in [104.0, 180, 347] {
            let leftOpen = stop(1, lat: 38.6000, lng: -121.3000, order: 1, checkIn: Fixtures.at(-minutes))
            let whereHeIs = stop(2, lat: 38.6500, lng: -121.3500, order: 2)          // about 7 km away
            let another = stop(3, lat: 38.7000, lng: -121.4000, order: 3)
            let result = StopRanker.rank(stops: [leftOpen, whereHeIs, another], fix: here(38.6500, -121.3500), farMeters: far, now: now)
            XCTAssertEqual(result.reason, .nearest, "left open \(Int(minutes)) minutes")
            XCTAssertEqual(result.suggested.first?.id, 2, "the house he is standing at comes first")
            XCTAssertNotEqual(result.suggested.first?.id, 1)
            XCTAssertEqual(result.all.map { $0.id }.sorted(), [1, 2, 3], "the stop left open is still on the list")
        }
    }

    /// The same, a few minutes after he drove off: with a fix the age does
    /// not matter. The phone is not there.
    func testWithAFixItIsWhereThePhoneIsNotHowOldTheCheckInIs() {
        let justLeft = stop(1, lat: 38.6000, lng: -121.3000, order: 1, checkIn: Fixtures.at(-12))
        let whereHeIs = stop(2, lat: 38.6500, lng: -121.3500, order: 2)
        XCTAssertEqual(first([justLeft, whereHeIs], fix: here(38.6500, -121.3500)).id, 2)
        XCTAssertEqual(first([justLeft, whereHeIs], fix: here(38.6500, -121.3500)).reason, .nearest)
        // Standing on the stop that is checked in, it is believed however old.
        let hoursAgo = stop(1, lat: 38.6000, lng: -121.3000, order: 1, checkIn: Fixtures.at(-300))
        XCTAssertEqual(first([whereHeIs, hoursAgo], fix: here(38.6000, -121.3000)).id, 1)
        XCTAssertEqual(first([whereHeIs, hoursAgo], fix: here(38.6000, -121.3000)).reason, .checkedIn)
    }

    /// The same distance as the far-away warning: 150 m plus the fix's own error.
    func testBelievedInsideTheFarAwayDistanceAndNotBeyondIt() {
        let checkedIn = stop(1, lat: 38.6000, lng: -121.3000, order: 2, checkIn: Fixtures.at(-5))
        let other = stop(2, lat: 38.6500, lng: -121.3500, order: 1)
        // One thousandth of a degree of latitude is about 111 m.
        XCTAssertEqual(first([checkedIn, other], fix: here(38.6010, -121.3000)).reason, .checkedIn, "111 m away")
        XCTAssertEqual(first([checkedIn, other], fix: here(38.6020, -121.3000)).reason, .nearest, "222 m away with an 8 m fix")
        XCTAssertEqual(first([checkedIn, other], fix: here(38.6020, -121.3000)).id, 1, "still the nearest, so still first: as nearest")
        XCTAssertEqual(first([checkedIn, other], fix: here(38.6020, -121.3000, accuracy: 90)).reason, .checkedIn, "222 m with a 90 m error circle could be on the lawn")
        XCTAssertTrue(StopRanker.believesOpenCheckIn(of: checkedIn, fix: here(38.6010, -121.3000), farMeters: far, now: now))
        XCTAssertFalse(StopRanker.believesOpenCheckIn(of: checkedIn, fix: here(38.6020, -121.3000), farMeters: far, now: now))
        XCTAssertFalse(StopRanker.believesOpenCheckIn(of: other, fix: here(38.6500, -121.3500), farMeters: far, now: now), "not checked in at all")
    }

    /// 2 of 96 times a tech checked the next customer in while still
    /// standing at the previous house. The house he is at comes first.
    func testCheckingTheNextCustomerInEarlyDoesNotMoveTheStopHeIsAt() {
        let stillHere = stop(1, lat: 38.6000, lng: -121.3000, order: 1)
        let checkedInEarly = stop(2, lat: 38.6300, lng: -121.3300, order: 2, checkIn: Fixtures.at(-1))   // about 4 km on
        let result = StopRanker.rank(stops: [stillHere, checkedInEarly], fix: here(38.6000, -121.3000), farMeters: far, now: now)
        XCTAssertEqual(result.suggested.first?.id, 1)
        XCTAssertEqual(result.reason, .nearest)
    }

    /// At 6 check-ins another stop was still open. With a fix, the one the
    /// phone is at wins whatever the times say.
    func testWithTwoStopsOpenTheOneThePhoneIsAtWins() {
        let forgotten = stop(1, lat: 38.6000, lng: -121.3000, order: 1, checkIn: Fixtures.at(-3))    // checked in last
        let current = stop(2, lat: 38.6500, lng: -121.3500, order: 2, checkIn: Fixtures.at(-40))
        let result = StopRanker.rank(stops: [forgotten, current], fix: here(38.6500, -121.3500), farMeters: far, now: now)
        XCTAssertEqual(result.suggested.first?.id, 2, "the more recent check-in is 7 km away: not believed")
        XCTAssertEqual(result.reason, .checkedIn)
        // Both believable (next-door neighbours): the most recent wins.
        let a = stop(3, lat: 38.61000, lng: -121.3100, order: 1, checkIn: Fixtures.at(-30))
        let b = stop(4, lat: 38.61010, lng: -121.3100, order: 2, checkIn: Fixtures.at(-4))
        XCTAssertEqual(first([a, b], fix: here(38.61005, -121.3100)).id, 4)
    }

    /// No fix (location off, or no signal for it): only how fresh the
    /// check-in is can be judged. 90 minutes.
    func testWithNoFixAnOpenCheckInIsBelievedForNinetyMinutes() {
        XCTAssertEqual(TwoKeys.openCheckInTrustSeconds, 90 * 60)
        let next = stop(2, lat: 38.6500, lng: -121.3500, order: 1)
        for (minutes, believed) in [(1.0, true), (20, true), (89, true), (90, true), (91, false), (104, false), (347, false)] {
            let open = stop(1, lat: 38.6000, lng: -121.3000, order: 2, checkIn: Fixtures.at(-minutes))
            let result = first([next, open], fix: nil)
            XCTAssertEqual(result.reason, believed ? .checkedIn : .routeOrder, "\(Int(minutes)) minutes")
            XCTAssertEqual(result.id, believed ? 1 : 2, "\(Int(minutes)) minutes: \(believed ? "the open check-in" : "the next stop on the route")")
        }
    }

    func testAFixTooFuzzyToUseAndAStopWithNoPinGoByFreshnessToo() {
        let next = stop(2, lat: 38.6500, lng: -121.3500, order: 1)
        let fresh = stop(1, lat: 38.6000, lng: -121.3000, order: 2, checkIn: Fixtures.at(-10))
        let stale = stop(1, lat: 38.6000, lng: -121.3000, order: 2, checkIn: Fixtures.at(-200))
        // A 2 km error circle says nothing about which house.
        let fuzzy = here(38.6500, -121.3500, accuracy: 2000)
        XCTAssertTrue(StopRanker.believesOpenCheckIn(of: fresh, fix: fuzzy, farMeters: far, now: now))
        XCTAssertFalse(StopRanker.believesOpenCheckIn(of: stale, fix: fuzzy, farMeters: far, now: now))
        XCTAssertEqual(first([next, fresh], fix: fuzzy).reason, .checkedIn)
        // No map pin: the phone's place cannot be compared with it.
        let noPinFresh = stop(3, lat: nil, lng: nil, order: 3, checkIn: Fixtures.at(-10))
        let noPinStale = stop(3, lat: nil, lng: nil, order: 3, checkIn: Fixtures.at(-200))
        XCTAssertEqual(first([next, noPinFresh], fix: here(38.6500, -121.3500)).id, 3)
        XCTAssertEqual(first([next, noPinStale], fix: here(38.6500, -121.3500)).id, 2)
    }

    /// Two techs tap check-in and check-out within a minute at about a
    /// fifth of their stops (22 of 106): there is never an open check-in
    /// during those visits, and the list goes by GPS.
    func testCheckedInAndOutWithinAMinuteLeavesTheListToGPS() {
        let tapped = stop(1, lat: 38.6000, lng: -121.3000, order: 1, checkIn: Fixtures.at(-2), checkOut: Fixtures.at(-1.5))
        let other = stop(2, lat: 38.6500, lng: -121.3500, order: 2)
        XCTAssertFalse(tapped.isCheckedInNow)
        XCTAssertEqual(first([other, tapped], fix: here(38.6000, -121.3000)).id, 1)
        XCTAssertEqual(first([other, tapped], fix: here(38.6000, -121.3000)).reason, .nearest)
        XCTAssertEqual(first([other, tapped], fix: nil).reason, .routeOrder)
    }

    /// A stop that has a video is never offered first, believable or not.
    func testAnOpenCheckInWithAVideoAlreadyIsNotOffered() {
        let open = stop(1, lat: 38.6000, lng: -121.3000, order: 1, checkIn: Fixtures.at(-5))
        let other = stop(2, lat: 38.6500, lng: -121.3500, order: 2)
        let result = StopRanker.rank(stops: [open, other], fix: here(38.6000, -121.3000), farMeters: far, now: now, haveVideo: [1])
        XCTAssertEqual(result.suggested.map { $0.id }, [2])
        XCTAssertEqual(result.rest.map { $0.id }, [1])
    }

    // MARK: The check-in key

    func testTheSlackIsTenMinutes() {
        XCTAssertEqual(TwoKeys.checkInSlackSeconds, 10 * 60)
        var visit = stop(1, lat: 38.6, lng: -121.3, order: 1, checkIn: Fixtures.at(0), checkOut: Fixtures.at(30))
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.at(-10), stop: visit), .match, "10 minutes before check-in")
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.at(-10.5), stop: visit), .mismatch)
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.at(15), stop: visit), .match, "during the visit")
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.at(40), stop: visit), .match, "10 minutes after check-out")
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.at(40.5), stop: visit), .mismatch)
        visit.checkIn = nil
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.at(15), stop: visit), .unknown, "no check-in time: nothing to go by")
    }

    /// The median drive between stops is 16.6 minutes. With 20 minutes of
    /// slack a video at the right stop also matched the next stop's window;
    /// with 10 it does not.
    func testAVideoAtTheRightStopNoLongerMatchesTheNextStopsWindow() {
        // He films at the end of this visit, drives 16.6 minutes, checks in next door.
        let filmed = Fixtures.at(0)
        let nextStop = stop(2, lat: 38.61, lng: -121.31, order: 2, checkIn: Fixtures.at(16.6), checkOut: Fixtures.at(40))
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: filmed, stop: nextStop), .mismatch)
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: filmed, stop: nextStop, slack: 20 * 60), .match, "what the old 20 minutes did")
        // A short hop of 8 minutes still falls inside: a match says little.
        let closeBy = stop(3, lat: 38.61, lng: -121.31, order: 3, checkIn: Fixtures.at(8), checkOut: Fixtures.at(30))
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: filmed, stop: closeBy), .match)
    }

    /// Checked in and out within the same minute (22 of 106 stops).
    func testCheckedInAndOutWithinAMinuteGivesAWindowOfTenMinutesEachSide() {
        let tapped = stop(1, lat: 38.6, lng: -121.3, order: 1, checkIn: Fixtures.at(0), checkOut: Fixtures.at(0.5))
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.at(-9), stop: tapped), .match)
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.at(10), stop: tapped), .match)
        // Tapped on arrival, filmed at the end of a 25-minute visit.
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.at(25), stop: tapped), .mismatch)
    }

    /// Left checked in for hours (6 of 106): after 90 minutes the open
    /// check-in says nothing, for or against.
    func testACheckInLeftOpenForHoursSaysNothing() {
        let open = stop(1, lat: 38.6, lng: -121.3, order: 1, checkIn: Fixtures.at(0))
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.at(30), stop: open), .match, "still there, half an hour in")
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.at(90), stop: open), .match)
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.at(104), stop: open), .unknown)
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.at(347), stop: open), .unknown)
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.at(-30), stop: open), .mismatch, "filmed well before he checked in")
    }

    // MARK: Two keys: every combination

    /// How the spoken key came out. A mismatch is one of two things, and
    /// they are kept apart: the words named another stop on today's list,
    /// or no customer's name was heard in them at all.
    private enum Said: String, CaseIterable {
        case match = "✓"
        case otherStop = "✗ another stop"
        case noName = "✗ no name"
        case unknown = "?"

        var result: SignalResult {
            switch self {
            case .match: return .match
            case .otherStop, .noName: return .mismatch
            case .unknown: return .unknown
            }
        }

        var miss: SpokenMiss? {
            switch self {
            case .otherStop: return .otherStop
            case .noName: return .noName
            case .match, .unknown: return nil
            }
        }
    }

    private typealias Row = (said: Said, gps: SignalResult, checkIn: SignalResult, clean: Bool, what: String)

    /// Every combination of the three keys for a stop where all three can
    /// be read (it has a map pin and a check-in time). ✓ match, ✗ mismatch,
    /// ? unknown. `clean` is "the video goes with no question asked".
    ///
    /// The rules:
    /// 1. At least two keys match, and one of them is the spoken name or
    ///    the GPS. The check-in can be the second key; it cannot send a
    ///    video by itself.
    /// 2. If the words named ANOTHER stop on today's list, the video never
    ///    goes without asking, whatever the other two keys say.
    private let table: [Row] = [
        // The name was said.
        (.match, .match, .match, true, "everything agrees"),
        (.match, .match, .mismatch, true, "name and place agree; checked in and out on arrival, filmed at the end"),
        (.match, .match, .unknown, true, "name and place agree; not checked in yet"),
        (.match, .mismatch, .match, true, "name and check-in agree; the map pin is off, or he filmed from the truck"),
        (.match, .mismatch, .mismatch, false, "only the name: far from the pin and outside the check-in"),
        (.match, .mismatch, .unknown, false, "the name says yes, the place says no"),
        (.match, .unknown, .match, true, "location off: the name, with the check-in in support"),
        (.match, .unknown, .mismatch, false, "location off, and the time does not fit the check-in"),
        (.match, .unknown, .unknown, false, "location off and no check-in yet: one key only"),
        // He said ANOTHER customer's name. Never clean.
        (.otherStop, .match, .match, false, "THE HOUSE NEXT DOOR: the place and the time fit, the words name the neighbour"),
        (.otherStop, .match, .mismatch, false, "another customer's name"),
        (.otherStop, .match, .unknown, false, "another customer's name"),
        (.otherStop, .mismatch, .match, false, "another customer's name"),
        (.otherStop, .mismatch, .mismatch, false, "another customer's name, and nothing else agrees"),
        (.otherStop, .mismatch, .unknown, false, "another customer's name"),
        (.otherStop, .unknown, .match, false, "another customer's name"),
        (.otherStop, .unknown, .mismatch, false, "another customer's name"),
        (.otherStop, .unknown, .unknown, false, "another customer's name"),
        // Words were heard, but no customer's name: he forgot it, or it was misheard.
        (.noName, .match, .match, true, "forgot the name at the right house: on the pin, inside his check-in. Not nagged"),
        (.noName, .match, .mismatch, false, "only the place"),
        (.noName, .match, .unknown, false, "only the place: no name and not checked in"),
        (.noName, .mismatch, .match, false, "only the check-in: never enough"),
        (.noName, .mismatch, .mismatch, false, "nothing agrees"),
        (.noName, .mismatch, .unknown, false, "nothing agrees"),
        (.noName, .unknown, .match, false, "only the check-in: never enough"),
        (.noName, .unknown, .mismatch, false, "nothing agrees"),
        (.noName, .unknown, .unknown, false, "nothing agrees"),
        // Speech-to-text did not run.
        (.unknown, .match, .match, true, "speech off: the place, with the check-in in support"),
        (.unknown, .match, .mismatch, false, "speech off: only the place"),
        (.unknown, .match, .unknown, false, "speech off and no check-in yet: only the place"),
        (.unknown, .mismatch, .match, false, "only the check-in: never enough"),
        (.unknown, .mismatch, .mismatch, false, "nothing agrees"),
        (.unknown, .mismatch, .unknown, false, "nothing agrees"),
        (.unknown, .unknown, .match, false, "speech off and location off: only the check-in, never enough"),
        (.unknown, .unknown, .mismatch, false, "nothing to go by, and the time does not fit"),
        (.unknown, .unknown, .unknown, false, "nothing to go by"),
    ]

    private func mark(_ result: SignalResult) -> String {
        switch result {
        case .match: return "✓"
        case .mismatch: return "✗"
        case .unknown: return "?"
        }
    }

    private func name(_ row: Row) -> String {
        "spoken \(row.said.rawValue), gps \(mark(row.gps)), check-in \(mark(row.checkIn)): \(row.what)"
    }

    private func verdict(_ row: Row, at stop: Stop) -> TwoKeys.Verdict {
        TwoKeys.verdict(Signals(spoken: row.said.result, gps: row.gps, checkIn: row.checkIn), stop: stop, spokenMiss: row.said.miss)
    }

    func testTheTableCoversEveryCombinationOnce() {
        XCTAssertEqual(table.count, 4 * 3 * 3)
        XCTAssertEqual(Set(table.map { "\($0.said.rawValue)|\(mark($0.gps))|\(mark($0.checkIn))" }).count, 36)
        XCTAssertEqual(table.filter { $0.clean }.count, 7, "seven ways to send with no question; twenty-nine that ask")
    }

    func testEveryCombinationAtAStopWithAPinAndACheckIn() {
        let full = stop(1, lat: 38.6, lng: -121.3, order: 1, checkIn: Fixtures.at(-5))
        for row in table {
            XCTAssertEqual(verdict(row, at: full).clean, row.clean, name(row))
            let plain = TwoKeys.verdict(Signals(spoken: row.said.result, gps: row.gps, checkIn: row.checkIn), spokenMiss: row.said.miss)
            XCTAssertEqual(plain.clean, row.clean, "the plain rule: \(name(row))")
        }
    }

    /// The two rows the rule turns on, side by side.
    func testAnotherCustomersNameAsksAndAForgottenNameDoesNot() {
        let full = stop(1, lat: 38.6, lng: -121.3, order: 1, checkIn: Fixtures.at(-5))
        let agree = Signals(spoken: .mismatch, gps: .match, checkIn: .match)
        let nextDoor = TwoKeys.verdict(agree, stop: full, spokenMiss: .otherStop)
        XCTAssertFalse(nextDoor.clean, "he said the neighbour's name: asked, although the place and the time agree")
        XCTAssertEqual(nextDoor.reasons, ["I did not hear this customer's name."])
        XCTAssertEqual(nextDoor.matches, 2, "two keys did match: that is exactly why this case needs its own rule")
        let forgot = TwoKeys.verdict(agree, stop: full, spokenMiss: .noName)
        XCTAssertTrue(forgot.clean, "no name at all, on the pin, inside his check-in: sent, not nagged")
        // With nothing said about which kind it was, the old reading stands.
        XCTAssertTrue(TwoKeys.verdict(agree, stop: full).clean)
    }

    /// A stop with a pin and no check-in time: the check-in is always "?".
    /// The same table, its twelve rows with the check-in unknown.
    func testEveryCombinationAtAStopWithAPinAndNoCheckIn() {
        let pinOnly = stop(1, lat: 38.6, lng: -121.3, order: 1)
        let rows = table.filter { $0.checkIn == .unknown }
        XCTAssertEqual(rows.count, 12)
        for row in rows {
            XCTAssertEqual(verdict(row, at: pinOnly).clean, row.clean, name(row))
        }
        XCTAssertEqual(rows.filter { $0.clean }.count, 1, "only the name and the place together")
    }

    /// A stop with a check-in time and no map pin: the GPS is always "?".
    func testEveryCombinationAtAStopWithACheckInAndNoPin() {
        let noPin = stop(1, lat: nil, lng: nil, order: 1, checkIn: Fixtures.at(-5))
        let rows = table.filter { $0.gps == .unknown }
        XCTAssertEqual(rows.count, 12)
        for row in rows {
            XCTAssertEqual(verdict(row, at: noPin).clean, row.clean, name(row))
        }
        XCTAssertEqual(rows.filter { $0.clean }.count, 1, "only the name with the check-in in support")
    }

    /// A stop with no pin and no check-in: only the spoken key can be read,
    /// and it is enough by itself (the App Review account's stops are like
    /// this). This is the one place a single key sends a video, and that
    /// key is never the check-in.
    func testAStopWithNoPinAndNoCheckInNeedsOnlyTheSpokenKey() {
        let bare = stop(1, lat: nil, lng: nil, order: 1)
        func clean(_ said: Said) -> Bool {
            TwoKeys.verdict(Signals(spoken: said.result, gps: .unknown, checkIn: .unknown), stop: bare, spokenMiss: said.miss).clean
        }
        XCTAssertTrue(clean(.match))
        XCTAssertFalse(clean(.otherStop))
        XCTAssertFalse(clean(.noName))
        XCTAssertFalse(clean(.unknown))
    }

    /// He said another customer's name: never clean, on any kind of stop
    /// (pin or not, check-in or not) and whatever the other two keys say.
    func testAnotherCustomersNameIsNeverCleanOnAnyKindOfStop() {
        let stops = [
            stop(1, lat: 38.6, lng: -121.3, order: 1, checkIn: Fixtures.at(-5)),
            stop(2, lat: 38.6, lng: -121.3, order: 2),
            stop(3, lat: nil, lng: nil, order: 3, checkIn: Fixtures.at(-5)),
            stop(4, lat: nil, lng: nil, order: 4),
        ]
        let all: [SignalResult] = [.match, .mismatch, .unknown]
        for stop in stops {
            for gps in all {
                for checkIn in all {
                    let signals = Signals(spoken: .mismatch, gps: gps, checkIn: checkIn)
                    XCTAssertFalse(
                        TwoKeys.verdict(signals, stop: stop, spokenMiss: .otherStop).clean,
                        "stop \(stop.customerId): gps \(mark(gps)) check-in \(mark(checkIn))"
                    )
                    XCTAssertFalse(TwoKeys.verdict(signals, spokenMiss: .otherStop).clean)
                }
            }
        }
    }

    /// Whatever the stop, and however few keys can be read for it, a
    /// check-in match with no spoken or GPS match beside it never sends.
    func testTheCheckInNeverSendsAVideoByItself() {
        let stops = [
            stop(1, lat: 38.6, lng: -121.3, order: 1, checkIn: Fixtures.at(-5)),
            stop(2, lat: 38.6, lng: -121.3, order: 2),
            stop(3, lat: nil, lng: nil, order: 3, checkIn: Fixtures.at(-5)),
            stop(4, lat: nil, lng: nil, order: 4),
        ]
        for stop in stops {
            for said in [Said.otherStop, .noName, .unknown] {
                for gps in [SignalResult.mismatch, .unknown] {
                    let signals = Signals(spoken: said.result, gps: gps, checkIn: .match)
                    let verdict = TwoKeys.verdict(signals, stop: stop, spokenMiss: said.miss)
                    XCTAssertFalse(verdict.clean, "stop \(stop.customerId): spoken \(said.rawValue) gps \(mark(gps)) check-in ✓")
                    XCTAssertFalse(verdict.reasons.isEmpty, "and the tech is told why he is asked")
                }
            }
        }
        // With nothing else to go by, what he sees is the plain line.
        XCTAssertEqual(
            TwoKeys.verdict(Signals(spoken: .unknown, gps: .unknown, checkIn: .match), stop: stops[0]).reasons,
            ["I could not check this is the right stop."]
        )
    }

    // MARK: The two kinds of spoken mismatch, from real words

    private let jordan = Fixtures.stop(2, "Jordan Quillfeather", last: "Quillfeather", "123 Harbor View Dr", lat: 38.6100, lng: -121.3100, order: 2, checkIn: Fixtures.at(-2))
    private let riley = Fixtures.stop(3, "Riley Ostrander", last: "Ostrander", "125 Harbor View Dr", lat: 38.6101, lng: -121.3100, order: 1, checkIn: Fixtures.at(-40), checkOut: Fixtures.at(-4))
    private let script = "Here's what I did. Here's what I recommend. I closed the backyard gate."

    /// The whole way through, with real times: the wrong house next door.
    /// 123 and 125 on the same street, 11 m apart. He did 125 first, checked
    /// out 4 minutes ago, and is now filming at 123 but tapped 125.
    func testTheHouseNextDoorIsAskedAboutAlthoughPlaceAndTimeFit() {
        let outcome = TwoKeys.signals(
            transcript: "Hi Jordan. \(script)", fix: here(38.6100, -121.3100), recordedAt: now, stop: riley, all: [jordan, riley], farMeters: far
        )
        XCTAssertEqual(outcome.signals, Signals(spoken: .mismatch, gps: .match, checkIn: .match), "the place and the time both fit the wrong house")
        XCTAssertEqual(outcome.spokenMiss, .otherStop)
        XCTAssertEqual(outcome.better?.customerId, 2, "the words point to the right one: \"It sounded like Quillfeather, Jordan.\"")
        XCTAssertFalse(TwoKeys.verdict(outcome.signals, stop: riley, spokenMiss: outcome.spokenMiss).clean, "so he is asked")
        // At the right house the same words go straight through.
        let right = TwoKeys.signals(
            transcript: "Hi Jordan. \(script)", fix: here(38.6100, -121.3100), recordedAt: now, stop: jordan, all: [jordan, riley], farMeters: far
        )
        XCTAssertNil(right.spokenMiss)
        XCTAssertTrue(TwoKeys.verdict(right.signals, stop: jordan, spokenMiss: right.spokenMiss).clean)
    }

    /// He forgot to say the name, at the right house, on the pin, inside
    /// his check-in: a key is missing, nothing is contradicted. Not nagged.
    func testAForgottenNameAtTheRightHouseStillGoes() {
        let outcome = TwoKeys.signals(
            transcript: script, fix: here(38.6100, -121.3100), recordedAt: now, stop: jordan, all: [jordan, riley], farMeters: far
        )
        XCTAssertEqual(outcome.signals, Signals(spoken: .mismatch, gps: .match, checkIn: .match))
        XCTAssertEqual(outcome.spokenMiss, .noName)
        XCTAssertNil(outcome.better)
        XCTAssertTrue(TwoKeys.verdict(outcome.signals, stop: jordan, spokenMiss: outcome.spokenMiss).clean)
        // Speech that did not run at all is not a mismatch of either kind.
        let silent = TwoKeys.signals(transcript: nil, fix: here(38.6100, -121.3100), recordedAt: now, stop: jordan, all: [jordan, riley], farMeters: far)
        XCTAssertEqual(silent.signals.spoken, .unknown)
        XCTAssertNil(silent.spokenMiss)
    }

    func testWhatCountsAsSayingAnotherCustomer() {
        let avery = Fixtures.stop(1, "Avery Maple", last: "Maple", "410 Birchwood Ln", lat: 38.6000, lng: -121.3000, order: 1)
        let otherAvery = Fixtures.stop(7, "Avery Thistlewood", last: "Thistlewood", "9 Fern Ct", lat: 38.6200, lng: -121.3200, order: 7)
        let onGateRoad = Fixtures.stop(8, "Morgan Pellham", last: "Pellham", "77 Gate Rd", lat: 38.6300, lng: -121.3300, order: 8)
        let everyone = [avery, otherAvery, jordan, riley, onGateRoad]
        func miss(_ words: String, at target: Stop, tech: String? = nil) -> SpokenMiss? {
            SpokenMatcher.evaluate(transcript: words, target: target, all: everyone, techName: tech).miss
        }
        // A first name, a last name, or a house number of another stop.
        XCTAssertEqual(miss("Hi Jordan. \(script)", at: riley), .otherStop)
        XCTAssertEqual(miss("this is the Quillfeather home", at: riley), .otherStop)
        XCTAssertEqual(miss("I am at four ten Birchwood Lane", at: riley), .otherStop, "another stop's house number and street")
        // Two other customers share the name he said: it still was not this
        // one's. Nobody can be suggested, but he is asked.
        let twoAverys = SpokenMatcher.evaluate(transcript: "Hi Avery. \(script)", target: riley, all: everyone)
        XCTAssertEqual(twoAverys.miss, .otherStop)
        XCTAssertNil(twoAverys.better, "which Avery is not known")
        // No name at all.
        XCTAssertEqual(miss(script, at: avery), .noName)
        XCTAssertEqual(miss("Hi there. \(script)", at: jordan), .noName)
        // A word of his script that happens to be a street on the route is
        // not "another customer": every clip says "the backyard gate".
        let gate = SpokenMatcher.evaluate(transcript: script, target: riley, all: everyone)
        XCTAssertEqual(gate.result, .mismatch)
        XCTAssertEqual(gate.miss, .noName, "Gate Road is on the route; nobody named its customer")
        // His own first name, when a customer shares it, is not a customer.
        XCTAssertEqual(miss("Hi there, this is Avery with Zest. \(script)", at: riley, tech: "Avery"), .noName)
        // A match, or speech that did not run, is no kind of miss.
        XCTAssertNil(miss("Hi Riley. \(script)", at: riley))
        XCTAssertNil(SpokenMatcher.evaluate(transcript: nil, target: riley, all: everyone).miss)
        // Saying both names: the words fit this stop at least as well. A match.
        XCTAssertNil(miss("Hi Riley, I just left Jordan next door. \(script)", at: riley))
    }

    // MARK: The audit trail tells the two apart

    func testTheOverrideEventSaysWhichKindOfMismatchItWas() {
        let agree = Signals(spoken: .mismatch, gps: .match, checkIn: .match)
        XCTAssertEqual(
            TwoKeys.auditDetail(agree, spokenMiss: .otherStop, better: jordan),
            ["spoken": "mismatch", "gps": "match", "checkIn": "match", "spokenMiss": "other_stop", "soundedLike": "2"]
        )
        XCTAssertEqual(
            TwoKeys.auditDetail(agree, spokenMiss: .otherStop, better: nil),
            ["spoken": "mismatch", "gps": "match", "checkIn": "match", "spokenMiss": "other_stop"],
            "two customers fitted equally: no one to name"
        )
        XCTAssertEqual(
            TwoKeys.auditDetail(Signals(spoken: .mismatch, gps: .match, checkIn: .unknown), spokenMiss: .noName, better: nil),
            ["spoken": "mismatch", "gps": "match", "checkIn": "unknown", "spokenMiss": "no_name"]
        )
        // No spoken mismatch, nothing extra.
        XCTAssertEqual(
            TwoKeys.auditDetail(Signals(spoken: .match, gps: .mismatch, checkIn: .unknown), spokenMiss: nil, better: nil),
            ["spoken": "match", "gps": "mismatch", "checkIn": "unknown"]
        )
        XCTAssertEqual(SpokenMiss.otherStop.rawValue, "other_stop")
        XCTAssertEqual(SpokenMiss.noName.rawValue, "no_name")
    }
}
