import XCTest
@testable import ZestCore

final class StorageAndCountdownTests: XCTestCase {
    private let mb: Int64 = 1024 * 1024

    func testStorageLevels() {
        XCTAssertEqual(StorageGuard.level(freeBytes: 2000 * mb), .ok)
        XCTAssertEqual(StorageGuard.level(freeBytes: 500 * mb), .ok)
        XCTAssertEqual(StorageGuard.level(freeBytes: 499 * mb), .warn)
        XCTAssertEqual(StorageGuard.level(freeBytes: 200 * mb), .warn)
        XCTAssertEqual(StorageGuard.level(freeBytes: 199 * mb), .block)
        XCTAssertEqual(StorageGuard.level(freeBytes: 0), .block)
        XCTAssertEqual(StorageGuard.level(freeBytes: nil), .ok, "never block when the phone will not say")
    }

    func testSizeText() {
        XCTAssertEqual(StorageGuard.sizeText(0), "0 MB")
        XCTAssertEqual(StorageGuard.sizeText(300_000), "1 MB")
        XCTAssertEqual(StorageGuard.sizeText(5 * mb), "5 MB")
        XCTAssertEqual(StorageGuard.sizeText(60 * mb), "60 MB")
        XCTAssertEqual(StorageGuard.sizeText(1536 * mb), "1.5 GB")
        XCTAssertEqual(StorageGuard.sizeText(-5), "0 MB")
    }

    func testRingDrainsFromFullToEmpty() {
        XCTAssertEqual(Countdown.fractionLeft(elapsed: 0, maxSeconds: 15), 1)
        XCTAssertEqual(Countdown.fractionLeft(elapsed: 7.5, maxSeconds: 15), 0.5)
        XCTAssertEqual(Countdown.fractionLeft(elapsed: 15, maxSeconds: 15), 0)
        XCTAssertEqual(Countdown.fractionLeft(elapsed: 99, maxSeconds: 15), 0)
        XCTAssertEqual(Countdown.fractionLeft(elapsed: -1, maxSeconds: 15), 1)
        XCTAssertEqual(Countdown.fractionLeft(elapsed: 1, maxSeconds: 0), 0)
    }

    func testBigNumberReadsFifteenDownToOne() {
        XCTAssertEqual(Countdown.secondsLeft(elapsed: 0, maxSeconds: 15), 15)
        XCTAssertEqual(Countdown.secondsLeft(elapsed: 0.01, maxSeconds: 15), 15)
        XCTAssertEqual(Countdown.secondsLeft(elapsed: 1.0, maxSeconds: 15), 14)
        XCTAssertEqual(Countdown.secondsLeft(elapsed: 14.2, maxSeconds: 15), 1)
        XCTAssertEqual(Countdown.secondsLeft(elapsed: 15, maxSeconds: 15), 0)
        XCTAssertEqual(Countdown.secondsLeft(elapsed: 20, maxSeconds: 15), 0)
    }

    func testFinalStretchIsTheLastThreeSeconds() {
        XCTAssertFalse(Countdown.isFinalStretch(elapsed: 11.9, maxSeconds: 15))
        XCTAssertTrue(Countdown.isFinalStretch(elapsed: 12, maxSeconds: 15))
        XCTAssertTrue(Countdown.isFinalStretch(elapsed: 14.9, maxSeconds: 15))
        XCTAssertFalse(Countdown.isFinalStretch(elapsed: 15, maxSeconds: 15))
    }

    /// A tap on the ring in the first three seconds does nothing; after that
    /// it ends the clip. (Model-level: a UI test cannot time a tap this finely.)
    func testRingTapOnlyEndsTheClipAfterThreeSeconds() {
        XCTAssertFalse(Countdown.canStopEarly(elapsed: 0))
        XCTAssertFalse(Countdown.canStopEarly(elapsed: 0.4))
        XCTAssertFalse(Countdown.canStopEarly(elapsed: 2.99))
        XCTAssertTrue(Countdown.canStopEarly(elapsed: 3))
        XCTAssertTrue(Countdown.canStopEarly(elapsed: 14.9))
        XCTAssertFalse(Countdown.canStopEarly(elapsed: -5), "a clock that jumped back never ends a clip")
    }

    func testPromptsShareTheClipEvenly() {
        XCTAssertEqual(Countdown.promptIndex(elapsed: 0, maxSeconds: 15, count: 4), 0)
        XCTAssertEqual(Countdown.promptIndex(elapsed: 3.7, maxSeconds: 15, count: 4), 0)
        XCTAssertEqual(Countdown.promptIndex(elapsed: 3.8, maxSeconds: 15, count: 4), 1)
        XCTAssertEqual(Countdown.promptIndex(elapsed: 7.6, maxSeconds: 15, count: 4), 2)
        XCTAssertEqual(Countdown.promptIndex(elapsed: 11.3, maxSeconds: 15, count: 4), 3)
        XCTAssertEqual(Countdown.promptIndex(elapsed: 99, maxSeconds: 15, count: 4), 3)
        XCTAssertEqual(Countdown.promptIndex(elapsed: 5, maxSeconds: 15, count: 0), 0)
        XCTAssertEqual(Countdown.promptIndex(elapsed: -2, maxSeconds: 15, count: 4), 0)
    }
}
