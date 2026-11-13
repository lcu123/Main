import XCTest
@testable import ZestCore

final class TwoKeysTests: XCTestCase {
    private func verdict(_ spoken: SignalResult, _ gps: SignalResult, _ checkIn: SignalResult) -> TwoKeys.Verdict {
        TwoKeys.verdict(Signals(spoken: spoken, gps: gps, checkIn: checkIn))
    }

    func testTwoOrMoreMatchesSendClean() {
        XCTAssertTrue(verdict(.match, .match, .match).clean)
        XCTAssertTrue(verdict(.match, .match, .unknown).clean)
        XCTAssertTrue(verdict(.match, .unknown, .match).clean)
        XCTAssertTrue(verdict(.mismatch, .match, .match).clean, "two keys agree even though one does not")
    }

    func testFewerThanTwoMatchesWarn() {
        XCTAssertFalse(verdict(.match, .unknown, .unknown).clean)
        XCTAssertFalse(verdict(.match, .mismatch, .unknown).clean)
        XCTAssertFalse(verdict(.unknown, .unknown, .unknown).clean)
        XCTAssertFalse(verdict(.mismatch, .mismatch, .mismatch).clean)
    }

    func testVerdictNamesWhatDisagreed() {
        let v = verdict(.mismatch, .match, .unknown)
        XCTAssertEqual(v.matches, 1)
        XCTAssertEqual(v.mismatched, [.spoken])
        XCTAssertEqual(v.unknown, [.checkIn])
        XCTAssertEqual(v.reasons, ["I did not hear this customer's name."])
        XCTAssertEqual(verdict(.mismatch, .mismatch, .mismatch).reasons.count, 3)
        XCTAssertEqual(verdict(.unknown, .unknown, .unknown).reasons, ["I could not check this is the right stop."])
    }

    func testGPSSignal() {
        let stop = Fixtures.quill
        let onTheLawn = VideoLocation(lat: 38.6100, lng: -121.3100, accuracyM: 8)
        let downTheRoad = VideoLocation(lat: 38.6130, lng: -121.3100, accuracyM: 8)   // about 330 m
        XCTAssertEqual(TwoKeys.gpsSignal(fix: onTheLawn, stop: stop, farMeters: 150), .match)
        XCTAssertEqual(TwoKeys.gpsSignal(fix: downTheRoad, stop: stop, farMeters: 150), .mismatch)
        XCTAssertEqual(TwoKeys.gpsSignal(fix: nil, stop: stop, farMeters: 150), .unknown)
        XCTAssertEqual(TwoKeys.gpsSignal(fix: onTheLawn, stop: Fixtures.noPin, farMeters: 150), .unknown)
    }

    func testGPSAllowsForAFuzzyFixButNotAUselessOne() {
        let stop = Fixtures.quill
        // About 200 m out with a 60 m error circle: could well be at the stop.
        let fuzzy = VideoLocation(lat: 38.6118, lng: -121.3100, accuracyM: 60)
        XCTAssertEqual(TwoKeys.gpsSignal(fix: fuzzy, stop: stop, farMeters: 150), .match)
        let useless = VideoLocation(lat: 38.6100, lng: -121.3100, accuracyM: 2000)
        XCTAssertEqual(TwoKeys.gpsSignal(fix: useless, stop: stop, farMeters: 150), .unknown)
        let invalid = VideoLocation(lat: 38.6100, lng: -121.3100, accuracyM: -1)
        XCTAssertEqual(TwoKeys.gpsSignal(fix: invalid, stop: stop, farMeters: 150), .unknown)
    }

    func testIsFarForTheConfirmScreen() {
        let far = VideoLocation(lat: 38.70, lng: -121.31, accuracyM: 10)
        let near = VideoLocation(lat: 38.61, lng: -121.31, accuracyM: 10)
        XCTAssertEqual(TwoKeys.isFar(fix: far, stop: Fixtures.quill, farMeters: 150), true)
        XCTAssertEqual(TwoKeys.isFar(fix: near, stop: Fixtures.quill, farMeters: 150), false)
        XCTAssertNil(TwoKeys.isFar(fix: nil, stop: Fixtures.quill, farMeters: 150))
    }

    func testCheckInSignal() {
        var stop = Fixtures.quill
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.base, stop: stop), .unknown, "no check-in known")

        stop.checkIn = Fixtures.at(-10)
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.base, stop: stop), .match, "still checked in")
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.at(-18), stop: stop), .match, "inside the 10 minute slack")
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.at(-25), stop: stop), .mismatch, "15 minutes before check-in: it matched when the slack was 20")
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.at(-45), stop: stop), .mismatch, "long before check-in")

        stop.checkOut = Fixtures.at(5)
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.at(13), stop: stop), .match, "8 minutes after check-out")
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.at(20), stop: stop), .mismatch, "15 minutes after check-out")
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.at(40), stop: stop), .mismatch, "35 minutes after check-out")
    }

    func testCheckInAndOutASecondApartStillGivesAUsableWindow() {
        // Seen in the real data: some stops are checked in and out a second apart.
        var stop = Fixtures.quill
        stop.checkIn = Fixtures.base
        stop.checkOut = Fixtures.base.addingTimeInterval(2)
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.at(-9), stop: stop), .match)
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.at(9), stop: stop), .match)
        XCTAssertEqual(TwoKeys.checkInSignal(recordedAt: Fixtures.at(12), stop: stop), .mismatch, "ten minutes each side, no more")
    }

    func testAllThreeSignalsTogether() {
        var stop = Fixtures.quill
        stop.checkIn = Fixtures.at(-10)
        let fix = VideoLocation(lat: 38.6100, lng: -121.3100, accuracyM: 8)
        let good = TwoKeys.signals(
            transcript: "this is the Quillfeather home", fix: fix, recordedAt: Fixtures.base,
            stop: stop, all: Fixtures.route, farMeters: 150
        )
        XCTAssertEqual(good.signals, Signals(spoken: .match, gps: .match, checkIn: .match))
        XCTAssertNil(good.better)

        // No signal all day: no check-in known, speech did not run, GPS only.
        let offline = TwoKeys.signals(
            transcript: nil, fix: fix, recordedAt: Fixtures.base,
            stop: Fixtures.quill, all: Fixtures.route, farMeters: 150
        )
        XCTAssertEqual(offline.signals, Signals(spoken: .unknown, gps: .match, checkIn: .unknown))
        XCTAssertFalse(TwoKeys.verdict(offline.signals).clean)

        // Wrong house picked: the words point next door.
        let wrong = TwoKeys.signals(
            transcript: "this is the Ostrander home", fix: fix, recordedAt: Fixtures.base,
            stop: Fixtures.quill, all: Fixtures.route, farMeters: 150
        )
        XCTAssertEqual(wrong.signals.spoken, .mismatch)
        XCTAssertEqual(wrong.better?.customerId, 3)
        XCTAssertFalse(TwoKeys.verdict(wrong.signals).clean)
    }

    func testAStopWithNoPinAndNoCheckInNeedsOnlyTheSpokenKey() {
        // The App Review account's stops are like this: nothing but the words to go on.
        let bare = Fixtures.noPin
        XCTAssertNil(bare.coordinate)
        XCTAssertNil(bare.checkIn)
        let said = Signals(spoken: .match, gps: .unknown, checkIn: .unknown)
        XCTAssertFalse(TwoKeys.verdict(said).clean, "the plain rule still wants two")
        XCTAssertTrue(TwoKeys.verdict(said, stop: bare).clean, "the only key there is, matched")

        // Not said, or speech did not run: still one warning.
        XCTAssertFalse(TwoKeys.verdict(Signals(spoken: .mismatch, gps: .unknown, checkIn: .unknown), stop: bare).clean)
        XCTAssertFalse(TwoKeys.verdict(.allUnknown, stop: bare).clean)
    }

    func testAStopWithAPinStillNeedsTwoKeys() {
        // A real stop has a map pin, so a spoken match alone is not enough,
        // even when the phone has no fix and nobody has checked in.
        let said = Signals(spoken: .match, gps: .unknown, checkIn: .unknown)
        XCTAssertFalse(TwoKeys.verdict(said, stop: Fixtures.quill).clean)
        XCTAssertTrue(TwoKeys.verdict(Signals(spoken: .match, gps: .match, checkIn: .unknown), stop: Fixtures.quill).clean)

        // No pin but checked in: two keys can be read, so two are needed.
        var checkedIn = Fixtures.noPin
        checkedIn.checkIn = Fixtures.at(-5)
        XCTAssertFalse(TwoKeys.verdict(said, stop: checkedIn).clean)
        XCTAssertTrue(TwoKeys.verdict(Signals(spoken: .match, gps: .unknown, checkIn: .match), stop: checkedIn).clean)

        // With all three readable the rule is unchanged.
        var full = Fixtures.quill
        full.checkIn = Fixtures.at(-5)
        XCTAssertTrue(TwoKeys.verdict(Signals(spoken: .mismatch, gps: .match, checkIn: .match), stop: full).clean)
        XCTAssertFalse(TwoKeys.verdict(Signals(spoken: .match, gps: .mismatch, checkIn: .mismatch), stop: full).clean)
    }

    // MARK: Hold window

    func testHoldWindowIsOpenForTenMinutesThenShutsForGood() {
        let recorded = Fixtures.base
        XCTAssertTrue(HoldWindow.isOpen(recordedAt: recorded, now: recorded, holdMinutes: 10))
        XCTAssertTrue(HoldWindow.isOpen(recordedAt: recorded, now: Fixtures.at(9.9), holdMinutes: 10))
        XCTAssertFalse(HoldWindow.isOpen(recordedAt: recorded, now: Fixtures.at(10), holdMinutes: 10))
        XCTAssertFalse(HoldWindow.isOpen(recordedAt: recorded, now: Fixtures.at(600), holdMinutes: 10))
        XCTAssertFalse(HoldWindow.isOpen(recordedAt: recorded, now: recorded, holdMinutes: 0))
    }

    func testHoldWindowDoesNotReopenWhenThePhoneClockGoesBack() {
        XCTAssertFalse(HoldWindow.isOpen(recordedAt: Fixtures.base, now: Fixtures.at(-120), holdMinutes: 10))
        XCTAssertTrue(HoldWindow.isOpen(recordedAt: Fixtures.base, now: Fixtures.at(-0.5), holdMinutes: 10), "small clock jitter is fine")
    }

    func testMinutesLeftInPlainWords() {
        let recorded = Fixtures.base
        XCTAssertEqual(HoldWindow.secondsLeft(recordedAt: recorded, now: recorded, holdMinutes: 10), 600)
        XCTAssertEqual(HoldWindow.secondsLeft(recordedAt: recorded, now: Fixtures.at(11), holdMinutes: 10), 0)
        XCTAssertEqual(HoldWindow.minutesLeft(secondsLeft: 600), 10)
        XCTAssertEqual(HoldWindow.minutesLeft(secondsLeft: 541), 10)
        XCTAssertEqual(HoldWindow.minutesLeft(secondsLeft: 540), 9)
        XCTAssertEqual(HoldWindow.minutesLeft(secondsLeft: 1), 1)
        XCTAssertEqual(HoldWindow.minutesLeft(secondsLeft: 0), 0)
        XCTAssertEqual(HoldWindow.text(secondsLeft: 420), "You can change it for 7 more minutes.")
        XCTAssertEqual(HoldWindow.text(secondsLeft: 30), "You can change it for 1 more minute.")
        XCTAssertEqual(HoldWindow.text(secondsLeft: 0), "It has gone to the customer.")
    }

    func testHoldWindowFromTheServersClosingTime() {
        XCTAssertTrue(HoldWindow.isOpen(closesAt: Fixtures.at(5), now: Fixtures.base))
        XCTAssertFalse(HoldWindow.isOpen(closesAt: Fixtures.at(-5), now: Fixtures.base))
        XCTAssertFalse(HoldWindow.isOpen(closesAt: nil, now: Fixtures.base))
    }
}
