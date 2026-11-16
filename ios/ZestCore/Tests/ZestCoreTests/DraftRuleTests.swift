import XCTest
@testable import ZestCore

/// "The draft stays": an unsaved clip survives the day change, and is only
/// dropped by itself once it is too old for the server to take.
final class DraftRuleTests: XCTestCase {
    func testDaysBetween() {
        XCTAssertEqual(ZestDates.daysBetween("2026-11-13", "2026-11-13"), 0)
        XCTAssertEqual(ZestDates.daysBetween("2026-11-12", "2026-11-13"), 1)
        XCTAssertEqual(ZestDates.daysBetween("2026-11-13", "2026-11-12"), -1)
        XCTAssertEqual(ZestDates.daysBetween("2026-10-30", "2026-11-13"), 14)
        XCTAssertEqual(ZestDates.daysBetween("2025-12-31", "2026-01-01"), 1, "over a new year")
        XCTAssertEqual(ZestDates.daysBetween("2028-02-28", "2028-03-01"), 2, "over a leap day")
        // The clocks change on 2026-11-01 in the office's time zone: still one day.
        XCTAssertEqual(ZestDates.daysBetween("2026-10-31", "2026-11-01"), 1)
        XCTAssertEqual(ZestDates.daysBetween("2026-03-07", "2026-03-08"), 1)
        XCTAssertNil(ZestDates.daysBetween("", "2026-11-13"))
        XCTAssertNil(ZestDates.daysBetween("2026-11-13", "yesterday"))
        XCTAssertNil(ZestDates.daysBetween("2026-02-30", "2026-11-13"), "not a real date")
    }

    func testAClipFilmedTodayCanBeSavedOrRecordedAgain() {
        let standing = DraftRule.standing(stopDate: "2026-11-13", today: "2026-11-13")
        XCTAssertEqual(standing, .today)
        XCTAssertTrue(DraftRule.canRecordAgain(standing))
        XCTAssertFalse(DraftRule.canDelete(standing))
    }

    func testAClipFromAnEarlierDayStaysAndCanBeSavedOrDeleted() {
        for (day, age) in [("2026-11-12", 1), ("2026-11-06", 7), ("2026-10-30", 14)] {
            let standing = DraftRule.standing(stopDate: day, today: "2026-11-13")
            XCTAssertEqual(standing, .earlier(daysOld: age), "\(day): the draft stays")
            XCTAssertFalse(DraftRule.canRecordAgain(standing), "\(day): the tech is not at that stop any more")
            XCTAssertTrue(DraftRule.canDelete(standing), day)
        }
    }

    func testOnlyAClipTheServerCanNoLongerTakeIsDropped() {
        XCTAssertEqual(DraftRule.keepDays, 14, "the server takes a video up to 14 days after its stop")
        XCTAssertEqual(DraftRule.standing(stopDate: "2026-10-30", today: "2026-11-13"), .earlier(daysOld: 14), "day 14 is still taken")
        let expired = DraftRule.standing(stopDate: "2026-10-29", today: "2026-11-13")
        XCTAssertEqual(expired, .expired(daysOld: 15))
        XCTAssertFalse(DraftRule.canRecordAgain(expired))
        XCTAssertFalse(DraftRule.canDelete(expired), "nothing is asked: it goes by itself, and that is logged")
        XCTAssertEqual(DraftRule.standing(stopDate: "2025-11-13", today: "2026-11-13"), .expired(daysOld: 365))
    }

    func testAClockOrADateThatIsOffNeverThrowsAClipAway() {
        // The phone's calendar a day behind the day on the clip.
        XCTAssertEqual(DraftRule.standing(stopDate: "2026-11-14", today: "2026-11-13"), .today)
        // A date that cannot be read.
        XCTAssertEqual(DraftRule.standing(stopDate: "", today: "2026-11-13"), .today)
        XCTAssertEqual(DraftRule.standing(stopDate: "2026-11-12", today: "?"), .today)
    }

    func testTheQuestionSaysWhichDayItWasFilmed() {
        XCTAssertEqual(DraftRule.filmedText(stopDate: "2026-11-12"), "Filmed Thursday, Nov 12")
        XCTAssertEqual(DraftRule.filmedText(stopDate: "2026-10-30"), "Filmed Friday, Oct 30")
        XCTAssertNil(DraftRule.filmedText(stopDate: "not a date"), "no day rather than a wrong one")
    }

    // MARK: "Do not delete this app"

    func testNothingOnThePhoneNoWarning() {
        XCTAssertNil(KeepApp.warning(videosOnPhone: 0))
        XCTAssertNil(KeepApp.waitingBanner(waiting: 0))
        XCTAssertNil(KeepApp.warning(videosOnPhone: -1))
    }

    func testAnyVideoStillOnThePhoneSaysDoNotDeleteTheApp() {
        XCTAssertEqual(KeepApp.warning(videosOnPhone: 1), "Do not delete this app. 1 video is still on this phone.")
        XCTAssertEqual(KeepApp.warning(videosOnPhone: 12), "Do not delete this app. 12 videos are still on this phone.")
        XCTAssertEqual(KeepApp.waitingBanner(waiting: 1), "1 video waiting to send. Do not delete this app.")
        XCTAssertEqual(KeepApp.waitingBanner(waiting: 3), "3 videos waiting to send. Do not delete this app.")
        for count in 1...20 {
            XCTAssertTrue(KeepApp.warning(videosOnPhone: count)?.contains("Do not delete this app") ?? false)
            XCTAssertTrue(KeepApp.waitingBanner(waiting: count)?.contains("Do not delete this app") ?? false)
        }
    }
}
