import XCTest
@testable import ZestCore

/// The black record screen.
///
/// The first build on a real phone opened the record screen black, with TAP
/// TO RECORD under the button, and answered every tap with "The video
/// stopped too soon. Please record it again." The capture session had
/// stopped; the app had remembered "ready" from the moment it started and
/// never asked again. Going BACK and tapping the stop again was the only
/// way out.
///
/// What the screen may say, and what the app does by itself to bring the
/// camera back, is now decided here from what the session says at that
/// moment (`CameraRule.settle`). `CameraService` asks the session and
/// carries the answer out; these tests are the rule.
final class CameraRuleTests: XCTestCase {
    /// Every combination of the facts, with every number of tries that matters.
    private var everything: [CameraFacts] {
        var all: [CameraFacts] = []
        let both = [false, true]
        for wanted in both { for running in both { for interrupted in both { for recording in both {
            for starting in both { for failed in both { for inFront in both {
                for tries in 0...(CameraRule.mostTries + 1) {
                    all.append(CameraFacts(
                        wanted: wanted, running: running, interrupted: interrupted, recording: recording,
                        starting: starting, failed: failed, triesLately: tries, inFront: inFront
                    ))
                }
            } } }
        } } } }
        return all
    }

    // MARK: The screen cannot lie

    func testTapToRecordIsOfferedOnlyWhileTheSessionSaysItIsRunning() {
        XCTAssertEqual(everything.count, 640)
        for facts in everything {
            let verdict = CameraRule.settle(facts)
            if verdict.standing == .ready {
                XCTAssertTrue(facts.running, "ready over a session that is not running: \(facts)")
                XCTAssertTrue(facts.wanted, "ready with no screen wanting the camera: \(facts)")
            }
            if facts.wanted && !facts.running && !facts.recording {
                XCTAssertTrue(
                    [.starting, .away, .failed].contains(verdict.standing),
                    "a stopped camera must read STARTING CAMERA, CAMERA OFF or could-not-start, never \(verdict.standing): \(facts)"
                )
            }
        }
    }

    func testTheOwnersPhone() {
        // A screen wants the camera; the session has stopped; nothing was
        // said about it and nothing is on its way.
        let stopped = CameraFacts(wanted: true, running: false, interrupted: false)
        XCTAssertEqual(CameraRule.settle(stopped), CameraRule.Verdict(.starting, .start), "STARTING CAMERA, and it is started with no touch")
        // Once it runs again, and not before, the button is offered.
        XCTAssertEqual(CameraRule.settle(CameraFacts(wanted: true, running: true, interrupted: false)), CameraRule.Verdict(.ready))
    }

    func testAnInterruptedSessionIsWaitedForAndNotFought() {
        // A call, another app, the app off the screen: the phone gives the
        // camera back by itself.
        for tries in 0...4 {
            let facts = CameraFacts(wanted: true, running: false, interrupted: true, triesLately: tries)
            XCTAssertEqual(CameraRule.settle(facts), CameraRule.Verdict(.away), "\(facts)")
        }
        // Running beats everything it might have said before.
        XCTAssertEqual(CameraRule.settle(CameraFacts(wanted: true, running: true, interrupted: true)), CameraRule.Verdict(.ready))
    }

    func testAStartThatIsOnItsWayIsNotAskedForTwice() {
        let facts = CameraFacts(wanted: true, running: false, interrupted: false, starting: true)
        XCTAssertEqual(CameraRule.settle(facts), CameraRule.Verdict(.starting))
    }

    func testARecordingIsLeftAloneUntilItIsHandedOver() {
        for facts in everything where facts.recording {
            XCTAssertEqual(CameraRule.settle(facts), CameraRule.Verdict(.asIs), "\(facts)")
        }
    }

    // MARK: Never stuck, and never for ever

    func testItTriesThreeTimesByItselfAndThenSaysSo() {
        XCTAssertEqual(CameraRule.mostTries, 3)
        for tries in 0..<3 {
            let facts = CameraFacts(wanted: true, running: false, interrupted: false, triesLately: tries)
            XCTAssertEqual(CameraRule.settle(facts), CameraRule.Verdict(.starting, .start), "try \(tries + 1)")
        }
        let spent = CameraFacts(wanted: true, running: false, interrupted: false, triesLately: 3)
        XCTAssertEqual(CameraRule.settle(spent), CameraRule.Verdict(.failed), "TRY AGAIN is offered instead")
        // Having said so, it stays said: the tries growing old does not
        // set it off again behind a page that says the camera could not start.
        let later = CameraFacts(wanted: true, running: false, interrupted: false, failed: true, triesLately: 0)
        XCTAssertEqual(CameraRule.settle(later), CameraRule.Verdict(.failed))
        // If the camera does come up after all, the screen shows it.
        XCTAssertEqual(CameraRule.settle(CameraFacts(wanted: true, running: true, interrupted: false, failed: true)), CameraRule.Verdict(.ready))
    }

    func testACameraThatNeverStartsCostsThreeStartsAndNoMore() {
        // The rule, turned round and round by a camera that will not start.
        var tries = 0
        var failed = false
        var starts = 0
        for _ in 0..<50 {
            let verdict = CameraRule.settle(CameraFacts(
                wanted: true, running: false, interrupted: false, failed: failed, triesLately: tries
            ))
            if verdict.step == .start {
                starts += 1
                tries += 1
            }
            failed = verdict.standing == .failed
        }
        XCTAssertEqual(starts, 3)
        XCTAssertTrue(failed)
    }

    func testNothingIsStartedByItselfWhenItShouldNotBe() {
        for facts in everything {
            let verdict = CameraRule.settle(facts)
            if verdict.step == .start {
                XCTAssertTrue(facts.wanted && !facts.running && !facts.interrupted, "\(facts)")
                XCTAssertTrue(!facts.starting && !facts.failed && !facts.recording, "\(facts)")
                XCTAssertTrue(facts.inFront, "started with the app off the screen: \(facts)")
                XCTAssertLessThan(facts.triesLately, CameraRule.mostTries, "\(facts)")
            }
            if verdict.step == .stop {
                XCTAssertTrue(!facts.wanted && facts.running && !facts.recording, "\(facts)")
                XCTAssertLessThan(facts.triesLately, CameraRule.mostTries, "\(facts)")
            }
        }
    }

    func testWithTheAppOffTheScreenItWaitsAndStartsWhenTheAppIsBack() {
        let away = CameraFacts(wanted: true, running: false, interrupted: false, inFront: false)
        XCTAssertEqual(CameraRule.settle(away), CameraRule.Verdict(.starting), "it says STARTING CAMERA and waits")
        var back = away
        back.inFront = true
        XCTAssertEqual(CameraRule.settle(back), CameraRule.Verdict(.starting, .start))
    }

    func testACameraNobodyWantsIsStopped() {
        // The phone starts an interrupted session again by itself when the
        // interruption ends. With no screen wanting it, that would leave
        // the camera and the microphone on under My Route.
        XCTAssertEqual(CameraRule.settle(CameraFacts(wanted: false, running: true, interrupted: false)), CameraRule.Verdict(.off, .stop))
        XCTAssertEqual(CameraRule.settle(CameraFacts(wanted: false, running: false, interrupted: false)), CameraRule.Verdict(.off))
        XCTAssertEqual(CameraRule.settle(CameraFacts(wanted: false, running: false, interrupted: true)), CameraRule.Verdict(.off))
        // Not for ever, should a session ever refuse to stop.
        XCTAssertEqual(CameraRule.settle(CameraFacts(wanted: false, running: true, interrupted: false, triesLately: 3)), CameraRule.Verdict(.off))
    }

    func testTheNumbers() {
        XCTAssertEqual(CameraRule.triesWindow, 10)
        XCTAssertEqual(CameraRule.secondsBetweenTries, 0.7, accuracy: 0.001)
        XCTAssertEqual(CameraRule.watchSeconds, 1, "the screen is never wrong for longer than about this")
        // Three tries with their pauses fit well inside the window that counts them.
        XCTAssertLessThan(Double(CameraRule.mostTries) * CameraRule.secondsBetweenTries * 2, CameraRule.triesWindow)
    }

    // MARK: The words

    func testTheWordsUnderTheButton() {
        XCTAssertEqual(CameraRule.label(ready: true, armed: false, away: false), "TAP TO RECORD")
        XCTAssertEqual(CameraRule.label(ready: false, armed: false, away: false), "STARTING CAMERA")
        XCTAssertEqual(CameraRule.label(ready: false, armed: false, away: true), "CAMERA OFF")
        // Between the tap and the first frame, whatever the camera is doing.
        for ready in [false, true] {
            for away in [false, true] {
                XCTAssertEqual(CameraRule.label(ready: ready, armed: true, away: away), "GET READY")
            }
        }
        // TAP TO RECORD is said in exactly one case.
        for ready in [false, true] {
            for armed in [false, true] {
                for away in [false, true] {
                    let label = CameraRule.label(ready: ready, armed: armed, away: away)
                    XCTAssertEqual(label == CameraRule.readyLabel, ready && !armed)
                    // And the button can be tapped in exactly that case.
                    XCTAssertEqual(CameraRule.offersRecord(ready: ready, armed: armed), label == CameraRule.readyLabel)
                    // In capitals, short enough for one line under the button.
                    XCTAssertEqual(label, label.uppercased())
                    XCTAssertLessThanOrEqual(label.count, 16, label)
                }
            }
        }
    }

    func testATapThatStartedNothingIsNotAVideoThatStoppedTooSoon() {
        XCTAssertEqual(CameraRule.didNotStart, "The camera did not start. Tap again.")
        // It is never one of the lines for a clip that was cut short, and
        // says nothing about a video: there was none.
        for cut in RecordingCut.allCases {
            XCTAssertNotEqual(CameraRule.didNotStart, RecordingRule.tooShort(cut))
            XCTAssertNotEqual(CameraRule.didNotStart, RecordingRule.reviewNote(cut))
        }
        XCTAssertNotEqual(CameraRule.didNotStart, RecordingRule.notSaved)
        for word in ["video", "stopped", "too soon", "record it again"] {
            XCTAssertFalse(CameraRule.didNotStart.lowercased().contains(word), "\"\(word)\" belongs to a clip that was cut short")
        }
        // While a call has the camera the screen already says so, and that
        // line is the one to read ("It comes back by itself"), not "tap".
        XCTAssertEqual(CameraRule.didNotStartLine(away: nil), CameraRule.didNotStart)
        for cut in RecordingCut.allCases {
            XCTAssertNil(CameraRule.didNotStartLine(away: cut), "\(cut)")
        }
    }

    func testTheNewLinesAreShortPlainAndAskNothing() {
        // The same rules as every other line on the camera screen
        // (`RecordingRuleTests.testEveryLineIsShortPlainAndAsksNothing`).
        for line in [CameraRule.didNotStart, CameraRule.couldNotStart] {
            XCTAssertLessThanOrEqual(line.split(separator: " ").count, 13, "Too long to read at a glance: \(line)")
            XCTAssertFalse(line.contains("?"), "One question per screen, and this is not it: \(line)")
            XCTAssertTrue(line.hasSuffix("."), line)
            for word in ["interrupt", "session", "error", "failed", "unable", "AVFoundation", "microphone"] {
                XCTAssertFalse(line.lowercased().contains(word.lowercased()), "Not a plain word (\(word)): \(line)")
            }
        }
        XCTAssertEqual(CameraRule.couldNotStart, "The camera could not start.")
    }

    // MARK: The audit trail

    func testTheAuditTrailCallsItNotStartedAndSaysWhatTheCameraSaid() {
        XCTAssertEqual(
            CameraRule.notStartedDetail(why: "stopped; error -11819 41s ago"),
            ["seconds": "0.0", "endedBy": "not_started", "kept": "no", "camera": "stopped; error -11819 41s ago"]
        )
        // Not one of the causes a real clip can be cut short by: those
        // names are fixed (`testTheNamesOfTheCausesAreFixed`), and a record
        // saved with this word would read as "unknown".
        XCTAssertNil(RecordingCut(rawValue: CameraRule.notStarted))
    }

    func testARealClipsRecordStopCarriesTheCamerasWordOnlyWhenThereIsOne() {
        XCTAssertEqual(RecordingRule.auditDetail(seconds: 14.96, cut: nil, kept: true, camera: nil), ["seconds": "15.0"])
        XCTAssertEqual(RecordingRule.auditDetail(seconds: 14.96, cut: nil, kept: true, camera: ""), ["seconds": "15.0"])
        XCTAssertEqual(
            RecordingRule.auditDetail(seconds: 15, cut: nil, kept: true, camera: "started at the tap"),
            ["seconds": "15.0", "camera": "started at the tap"]
        )
        XCTAssertEqual(
            RecordingRule.auditDetail(seconds: 1.2, cut: .unknown, kept: false, camera: "error -11818"),
            ["seconds": "1.2", "endedBy": "unknown", "kept": "no", "camera": "error -11818"]
        )
    }
}
