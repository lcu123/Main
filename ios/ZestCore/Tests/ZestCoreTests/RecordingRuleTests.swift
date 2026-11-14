import XCTest
@testable import ZestCore

/// A call (or anything else) interrupts a recording.
///
/// The owner's rules: nothing lost, never stuck, one question per screen,
/// plain words, red only while really recording. What the phone does to the
/// camera is in `CameraService`; what the app then does with the clip, and
/// what it says, is decided here.
final class RecordingRuleTests: XCTestCase {
    // MARK: Where the clip goes

    func testAClipThatRanToTheEndOrThatTheTechStoppedGoesToSaveThisVideoWithNothingSaid() {
        XCTAssertEqual(RecordingRule.next(seconds: 15, cut: nil), .review(note: nil))
        XCTAssertEqual(RecordingRule.next(seconds: 3.2, cut: nil), .review(note: nil), "He tapped the ring after three seconds.")
        // No rule about length applies to a clip nothing cut short.
        XCTAssertEqual(RecordingRule.next(seconds: 0.8, cut: nil), .review(note: nil))
    }

    func testACallNineSecondsInKeepsTheClipAndSaysWhy() {
        XCTAssertEqual(
            RecordingRule.next(seconds: 9, cut: .callOrAlarm),
            .review(note: "A call or alarm stopped this video early.")
        )
    }

    func testACallTwoSecondsInIsTooShortToSendAndSaysSo() {
        XCTAssertEqual(
            RecordingRule.next(seconds: 2, cut: .callOrAlarm),
            .recordAgain(message: "A call or alarm stopped the video too soon. Please record it again.")
        )
    }

    func testTheLineIsThreeSecondsTheSameAsForTheTechsOwnTap() {
        XCTAssertEqual(RecordingRule.shortestWorthKeeping, 3)
        XCTAssertEqual(RecordingRule.shortestWorthKeeping, Countdown.earliestStopSeconds, "A call can leave nothing shorter than he could have made on purpose.")
        guard case .recordAgain = RecordingRule.next(seconds: 2.99, cut: .callOrAlarm) else { return XCTFail("2.99 s is too short") }
        guard case .review = RecordingRule.next(seconds: 3.0, cut: .callOrAlarm) else { return XCTFail("3.0 s is kept") }
        guard case .recordAgain = RecordingRule.next(seconds: 0, cut: .appClosed) else { return XCTFail("nothing recorded") }
    }

    func testEveryKindOfInterruptionFollowsTheSameLine() {
        for cut in RecordingCut.allCases where cut != .appStopped {
            guard case .recordAgain(let message) = RecordingRule.next(seconds: 1.5, cut: cut) else {
                return XCTFail("\(cut): a second and a half is not worth sending")
            }
            XCTAssertEqual(message, RecordingRule.tooShort(cut))
            guard case .review(let note) = RecordingRule.next(seconds: 8, cut: cut) else {
                return XCTFail("\(cut): eight seconds is kept")
            }
            XCTAssertEqual(note, RecordingRule.reviewNote(cut))
        }
    }

    func testAClipFoundAfterTheAppWasStoppedIsKeptWhateverItsLength() {
        // Nobody is at the camera to be told to record again: he is asked
        // "Save this video?" at the next start, and can say RECORD AGAIN.
        XCTAssertEqual(
            RecordingRule.next(seconds: 1.2, cut: .appStopped),
            .review(note: "This video stopped early when the app closed.")
        )
        XCTAssertEqual(
            RecordingRule.next(seconds: 11, cut: .appStopped),
            .review(note: "This video stopped early when the app closed.")
        )
    }

    // MARK: The words

    func testTheWordsSayOnlyWhatThePhoneCanKnow() {
        // A call, FaceTime, Siri, an alarm and a timer all look the same to
        // the app, and it does not claim to know which it was.
        XCTAssertEqual(RecordingRule.reviewNote(.callOrAlarm), "A call or alarm stopped this video early.")
        XCTAssertEqual(RecordingRule.tooShort(.callOrAlarm), "A call or alarm stopped the video too soon. Please record it again.")
        // What the phone does name is said exactly.
        XCTAssertEqual(RecordingRule.reviewNote(.appClosed), "This video stopped early when the app closed.")
        XCTAssertEqual(RecordingRule.reviewNote(.cameraTaken), "Another app took the camera. This video stopped early.")
        XCTAssertEqual(RecordingRule.reviewNote(.phoneBusy), "The phone stopped this video early.")
        XCTAssertEqual(RecordingRule.reviewNote(.phoneFull), "The phone ran out of room. This video stopped early.")
        XCTAssertEqual(RecordingRule.reviewNote(.unknown), "This video stopped early.")

        XCTAssertEqual(RecordingRule.tooShort(.phoneBusy), "The video stopped too soon. Please record it again.")
        XCTAssertEqual(RecordingRule.tooShort(.appClosed), "The video stopped when the app closed. Please record it again.")
        XCTAssertEqual(RecordingRule.tooShort(.cameraTaken), "Another app took the camera. Please record it again.")
        XCTAssertEqual(RecordingRule.tooShort(.phoneFull), "The phone ran out of room. The video did not save.")

        XCTAssertEqual(RecordingRule.cameraAway(.callOrAlarm), "The camera is off during a call. It comes back by itself.")
        XCTAssertEqual(RecordingRule.cameraAway(.cameraTaken), "Another app has the camera. It comes back by itself.")
        XCTAssertEqual(RecordingRule.cameraAway(.unknown), "The camera is off for a moment. It comes back by itself.")
        XCTAssertEqual(RecordingRule.notSaved, "That video did not save. Please record it again.")
    }

    func testEveryLineIsShortPlainAndAsksNothing() {
        var lines: [String] = [RecordingRule.notSaved]
        for cut in RecordingCut.allCases {
            lines += [RecordingRule.reviewNote(cut), RecordingRule.tooShort(cut), RecordingRule.cameraAway(cut)]
        }
        for line in lines {
            XCTAssertLessThanOrEqual(line.split(separator: " ").count, 13, "Too long to read at a glance: \(line)")
            XCTAssertFalse(line.contains("?"), "One question per screen, and this is not it: \(line)")
            XCTAssertTrue(line.hasSuffix("."), line)
            for word in ["interrupt", "session", "error", "failed", "unable", "AVFoundation", "microphone"] {
                XCTAssertFalse(line.lowercased().contains(word.lowercased()), "Not a plain word (\(word)): \(line)")
            }
        }
        // The label under the big button while the camera is away.
        XCTAssertEqual(RecordingRule.cameraAwayLabel, "CAMERA OFF")
    }

    // MARK: Never stuck

    func testACameraThatNeverReportsIsGivenFourSecondsPastTheLimit() {
        XCTAssertEqual(RecordingRule.overdueSeconds, 4)
        XCTAssertFalse(RecordingRule.isOverdue(elapsed: 15, maxSeconds: 15), "It may still be writing the file.")
        XCTAssertFalse(RecordingRule.isOverdue(elapsed: 18.9, maxSeconds: 15))
        XCTAssertTrue(RecordingRule.isOverdue(elapsed: 19, maxSeconds: 15))
        XCTAssertTrue(RecordingRule.isOverdue(elapsed: 7, maxSeconds: 3))
    }

    // MARK: Nothing lost

    func testAFileThatWillNotSaveIsKeptAndTriedThreeTimes() {
        XCTAssertEqual(RecordingRule.saveTries, 3)
        XCTAssertTrue(RecordingRule.keepForAnotherTry(triesSoFar: 1), "The phone was locked in the middle of it: try when it is opened.")
        XCTAssertTrue(RecordingRule.keepForAnotherTry(triesSoFar: 2))
        XCTAssertFalse(RecordingRule.keepForAnotherTry(triesSoFar: 3), "Three times is a file that will never save.")
    }

    // MARK: The audit trail

    func testTheRecordStopEventSaysHowItEnded() {
        XCTAssertEqual(RecordingRule.auditDetail(seconds: 14.96, cut: nil, kept: true), ["seconds": "15.0"])
        XCTAssertEqual(
            RecordingRule.auditDetail(seconds: 9.04, cut: .callOrAlarm, kept: true),
            ["seconds": "9.0", "endedBy": "call_or_alarm", "kept": "yes"]
        )
        XCTAssertEqual(
            RecordingRule.auditDetail(seconds: 2.1, cut: .appClosed, kept: false),
            ["seconds": "2.1", "endedBy": "app_closed", "kept": "no"]
        )
        XCTAssertEqual(RecordingRule.auditDetail(seconds: -1, cut: .unknown, kept: false)["seconds"], "0.0")
    }

    func testTheNamesOfTheCausesAreFixed() {
        // They are written into saved records and the audit trail.
        XCTAssertEqual(
            RecordingCut.allCases.map { $0.rawValue },
            ["call_or_alarm", "app_closed", "camera_taken", "phone_busy", "phone_full", "app_stopped", "unknown"]
        )
        let saved = try? JSONDecoder().decode([RecordingCut].self, from: Data(#"["call_or_alarm","app_closed"]"#.utf8))
        XCTAssertEqual(saved, [.callOrAlarm, .appClosed])
    }

    func testANameThisBuildDoesNotKnowReadsAsUnknownAndLosesNothing() throws {
        // A record on disk written with a name that is no longer used (or
        // not yet known) still loads: the clip it belongs to is kept.
        let saved = try JSONDecoder().decode([RecordingCut].self, from: Data(#"["call","other_sound","something_new"]"#.utf8))
        XCTAssertEqual(saved, [.unknown, .unknown, .unknown])
        XCTAssertEqual(String(decoding: try JSONEncoder().encode([RecordingCut.callOrAlarm]), as: UTF8.self), #"["call_or_alarm"]"#)
    }

    func testTheAppNeverClaimsToKnowItWasACall() {
        // It cannot know: telling a call from an alarm needs CallKit, which
        // the app does not link (see `AppSourceRulesTests`). So no line
        // says "a call" alone about a clip.
        for cut in RecordingCut.allCases {
            for line in [RecordingRule.reviewNote(cut), RecordingRule.tooShort(cut)] where line.lowercased().contains("call") {
                XCTAssertTrue(line.contains("A call or alarm"), line)
            }
        }
    }
}
