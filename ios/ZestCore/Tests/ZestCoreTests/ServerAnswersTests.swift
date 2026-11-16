import XCTest
@testable import ZestCore

/// The two places where the app's practice server had drifted from the real
/// one: a video "not on my list" (the real server answers `unmatched`, with
/// no email time), and a video the server does not know (a 404 whose error
/// word is `bad_request`, not `not_found`).
final class ServerAnswersTests: XCTestCase {
    private func run(_ item: QueueItem, _ events: QueueEvent..., at now: Date = Fixtures.base) -> QueueItem {
        events.reduce(item) { QueueMachine.reduce($0, $1, now: now) }
    }

    private func offList() -> QueueItem {
        Fixtures.item(id: "off", customerId: nil, notOnMyList: true)
    }

    // MARK: Not on my list

    func testTheRealAnswersDecodeWithNoEmailTime() throws {
        let complete = #"{"id": "off", "state": "unmatched", "emailAt": null}"#
        let answer = try ZestJSON.decoder().decode(VideoStatusResponse.self, from: Data(complete.utf8))
        XCTAssertEqual(answer.state, .unmatched)
        XCTAssertNil(answer.emailAt)
        // And with the key left out altogether.
        let bare = try ZestJSON.decoder().decode(VideoStatusResponse.self, from: Data(#"{"id": "off", "state": "unmatched"}"#.utf8))
        XCTAssertNil(bare.emailAt)

        let listed = #"{"id": "off", "customerId": null, "state": "unmatched", "emailAt": null}"#
        let video = try ZestJSON.decoder().decode(TodayVideo.self, from: Data(listed.utf8))
        XCTAssertEqual(video, TodayVideo(id: "off", customerId: nil, state: .unmatched, emailAt: nil))

        let again = #"{"id": "off", "state": "unmatched", "upload": null}"#
        let repeatRegister = try ZestJSON.decoder().decode(RegisterVideoResponse.self, from: Data(again.utf8))
        XCTAssertEqual(repeatRegister.state, .unmatched)
        XCTAssertNil(repeatRegister.upload)
        XCTAssertNil(repeatRegister.emailAt)
    }

    func testAVideoNotOnTheListIsSentToTheOfficeNotToACustomer() {
        var item = offList()
        XCTAssertTrue(item.goesToOffice, "before the server has said anything, the phone knows it is off the list")
        XCTAssertEqual(item.displayText, "WAITING", "and it is not sent yet")

        item = run(item, .registering, .registered(Fixtures.ticket), .uploadFinished(status: 200))
        XCTAssertEqual(item.displayText, "SENDING 99%")
        XCTAssertTrue(item.goesToOffice)

        item = run(item, .completed(state: .unmatched, emailAt: nil))
        XCTAssertEqual(item.phase, .done, "the server has the whole file: the phone's copy can go")
        XCTAssertEqual(item.serverState, .unmatched)
        XCTAssertNil(item.emailAt, "no email is on its way")
        XCTAssertEqual(item.displayText, "SENT TO OFFICE")
        XCTAssertEqual(item.display, .sent, "it still counts as sent: nothing is left to do on the phone")
        XCTAssertFalse(item.isWaiting)
        XCTAssertFalse(item.needsFile)

        // The day's list repeats it, still with no email time: no change.
        let later = run(item, .serverState(.unmatched, emailAt: nil))
        XCTAssertEqual(later.displayText, "SENT TO OFFICE")
        XCTAssertNil(later.emailAt)
        XCTAssertEqual(later.phase, .done)
    }

    func testOnceTheOfficeMatchesItItReadsLikeAnyOtherVideo() {
        let item = run(offList(), .registering, .registered(Fixtures.ticket), .uploadFinished(status: 200), .completed(state: .unmatched, emailAt: nil))
        let matched = run(item, .serverState(.held, emailAt: Fixtures.at(10)))
        XCTAssertFalse(matched.goesToOffice, "it now has a customer and an email on its way")
        XCTAssertEqual(matched.displayText, "SENT ✓")
        XCTAssertEqual(matched.emailAt, Fixtures.at(10))
        XCTAssertEqual(run(matched, .serverState(.sent, emailAt: nil)).displayText, "SENT ✓")
        // Pulled by the office: gone from the phone's list.
        XCTAssertEqual(run(item, .serverState(.pulled, emailAt: nil)).phase, .removed)
    }

    func testAVideoOnTheListNeverSaysOffice() {
        var item = Fixtures.item()
        XCTAssertFalse(item.goesToOffice)
        item = run(item, .registering, .registered(Fixtures.ticket), .uploadFinished(status: 200), .completed(state: .held, emailAt: Fixtures.at(10)))
        XCTAssertEqual(item.displayText, "SENT ✓")
        XCTAssertFalse(item.goesToOffice)
        // A state this build does not know is not taken for "unmatched".
        XCTAssertFalse(run(Fixtures.item(), .serverAlreadyHas(state: .unknown, emailAt: nil)).goesToOffice)
        XCTAssertTrue(run(offList(), .serverAlreadyHas(state: .unknown, emailAt: nil)).goesToOffice)
    }

    func testTheWordsNeverPromiseAnEmailForAVideoThatGoesToTheOffice() {
        let office = [
            VideoWords.sentToOffice, VideoWords.afterTheWindow(toOffice: true), VideoWords.locked(toOffice: true),
            HoldWindow.text(secondsLeft: 0, toOffice: true), HoldWindow.text(secondsLeft: 400, toOffice: true),
        ]
        for words in office {
            XCTAssertFalse(words.lowercased().contains("email"), words)
            XCTAssertFalse(words.contains("goes to the customer"), words)
            XCTAssertFalse(words.contains("went to the customer"), words)
            XCTAssertFalse(words.contains("gone to the customer"), words)
        }
        XCTAssertEqual(VideoWords.afterTheWindow(toOffice: true), "Then the office matches it to the customer.")
        XCTAssertEqual(VideoWords.afterTheWindow(toOffice: false), "Then it goes to the customer.")
        XCTAssertEqual(VideoWords.locked(toOffice: false), "This video already went to the customer. Call the office to change it.")
        XCTAssertTrue(VideoWords.locked(toOffice: true).contains("Call the office"))
        XCTAssertEqual(HoldWindow.text(secondsLeft: 0, toOffice: true), "It has gone to the office.")
        XCTAssertEqual(HoldWindow.text(secondsLeft: 0), "It has gone to the customer.")
        // The minutes left to delete it are the same either way.
        XCTAssertEqual(HoldWindow.text(secondsLeft: 400, toOffice: true), HoldWindow.text(secondsLeft: 400))
    }

    func testTheTenMinutesToDeleteItRunFromWhenItWasFilmed() {
        // No email time comes from the server, so the phone counts from the clip.
        let item = run(offList(), .registering, .registered(Fixtures.ticket), .uploadFinished(status: 200), .completed(state: .unmatched, emailAt: nil))
        XCTAssertTrue(HoldWindow.isOpen(recordedAt: item.recordedAt, now: Fixtures.at(9.9), holdMinutes: 10))
        XCTAssertFalse(HoldWindow.isOpen(recordedAt: item.recordedAt, now: Fixtures.at(10.1), holdMinutes: 10))
        // Deleted inside the window: the server is told, then it is gone.
        let deleting = run(item, .deletedByTech)
        XCTAssertEqual(deleting.phase, .deleting)
        XCTAssertEqual(run(deleting, .deleteConfirmed).phase, .removed)
    }

    // MARK: A video the server does not know

    func testNothingReadsTheWordThatComesWithA404() {
        for word in ["bad_request", "not_found", "", "anything_else"] {
            XCTAssertEqual(ServerSays.afterFailedComplete(status: 404, code: word), .notUploaded, word)
            XCTAssertEqual(ServerSays.afterFailedDelete(status: 404, code: word), .finished, word)
        }
    }

    func testCompleteForAVideoTheServerDoesNotKnowStartsAgainAndThenFinishes() {
        var item = run(Fixtures.item(), .registering, .registered(Fixtures.ticket), .uploadFinished(status: 200))
        XCTAssertEqual(item.phase, .uploaded)

        // 404 {"error": "bad_request", "message": "We do not know this video."}
        guard let event = ServerSays.afterFailedComplete(status: 404, code: "bad_request") else {
            return XCTFail("a 404 on complete must be acted on")
        }
        item = run(item, event)
        XCTAssertEqual(item.phase, .queued, "back to the start: it will be registered again")
        XCTAssertNil(item.uploadURL, "with a fresh upload address")
        XCTAssertTrue(item.needsFile, "the file stays on the phone")
        XCTAssertNotNil(item.notBefore, "after a wait, not at once")
        XCTAssertEqual(item.attempts, 1)

        // It is not asked for again before the wait is up...
        let waitingPlan = UploadPlanner.plan(items: [item], uploading: [], registering: [], busy: [], now: Fixtures.base)
        XCTAssertTrue(waitingPlan.steps.isEmpty, "no call while it waits: \(waitingPlan.steps)")
        XCTAssertNotNil(waitingPlan.lookAgainAt)
        // ...and then goes through the whole way again, once.
        let later = Fixtures.at(10)
        XCTAssertEqual(UploadPlanner.plan(items: [item], uploading: [], registering: [], busy: [], now: later).steps, [.register("0b0e7c1e-7c35-4f0b-9c50-0f7f6f0f3f11")])
        item = run(item, .registering, .registered(Fixtures.ticket), .uploadFinished(status: 200), .completed(state: .held, emailAt: Fixtures.at(20)), at: later)
        XCTAssertEqual(item.phase, .done)
        XCTAssertEqual(item.displayText, "SENT ✓")
    }

    func testAServerThatKeeps404ingIsAskedLessAndLessOften() {
        var item = Fixtures.item()
        var waits: [TimeInterval] = []
        var now = Fixtures.base
        for _ in 0..<8 {
            item = QueueMachine.reduce(item, .registering, now: now)
            item = QueueMachine.reduce(item, .registered(Fixtures.ticket), now: now)
            item = QueueMachine.reduce(item, .uploadFinished(status: 200), now: now)
            item = QueueMachine.reduce(item, .notUploaded, now: now)
            let wait = (item.notBefore ?? now).timeIntervalSince(now)
            waits.append(wait)
            now = now.addingTimeInterval(wait + 1)
        }
        XCTAssertEqual(waits, waits.sorted(), "each wait is at least as long as the one before: \(waits)")
        XCTAssertGreaterThanOrEqual(waits[0], 5)
        XCTAssertEqual(waits.last, 300, "never more than five minutes apart, and never a tight loop")
        XCTAssertTrue(item.needsFile, "and the video is still on the phone the whole time")
        XCTAssertEqual(item.phase, .queued)
    }

    func testOtherFailuresOfCompleteAreLeftToTheUsualHandling() {
        XCTAssertEqual(ServerSays.afterFailedComplete(status: 409, code: "not_uploaded"), .notUploaded)
        XCTAssertNil(ServerSays.afterFailedComplete(status: 500, code: "server_error"))
        XCTAssertNil(ServerSays.afterFailedComplete(status: 400, code: "bad_request"), "a 400 is not a 404")
        XCTAssertNil(ServerSays.afterFailedComplete(status: nil, code: ""), "no answer at all")
        XCTAssertNil(ServerSays.afterFailedComplete(status: 429, code: "slow_down"))
    }

    func testDeleteOfAVideoTheServerDoesNotKnowIsFinished() {
        // The tech deleted a video the server had heard of; the server then
        // answers 404 "bad_request" (it is gone, or was never there).
        var item = run(Fixtures.item(), .registering, .registered(Fixtures.ticket), .deletedByTech)
        XCTAssertEqual(item.phase, .deleting)
        XCTAssertEqual(ServerSays.afterFailedDelete(status: 404, code: "bad_request"), .finished)
        item = run(item, .deleteConfirmed)
        XCTAssertEqual(item.phase, .removed, "dropped: it is not asked about again")
        XCTAssertEqual(run(item, .wake, .callFailed("x", retryAfter: nil)).phase, .removed, "removed is final")
    }

    func testTooLateAndPassingTroubleOnDelete() {
        XCTAssertEqual(ServerSays.afterFailedDelete(status: 409, code: "too_late"), .tooLate)
        XCTAssertEqual(ServerSays.afterFailedDelete(status: 400, code: "bad_request"), .finished)
        for status in [500, 502, 503, 429, 408] {
            XCTAssertEqual(ServerSays.afterFailedDelete(status: status, code: "server_error"), .tryAgain, "\(status)")
        }
        XCTAssertEqual(ServerSays.afterFailedDelete(status: nil, code: ""), .tryAgain, "no answer: the delete still has to reach the server")
        XCTAssertEqual(ServerSays.afterFailedDelete(status: 401, code: "unauthorized"), .tryAgain)
        XCTAssertEqual(ServerSays.afterFailedDelete(status: 426, code: "update_required"), .tryAgain)
        // While it waits to be told, the delete is retried, with a pause.
        var item = run(Fixtures.item(), .registering, .registered(Fixtures.ticket), .deletedByTech)
        item = run(item, .callFailed("No signal.", retryAfter: nil))
        XCTAssertEqual(item.phase, .deleting)
        XCTAssertNotNil(item.notBefore)
    }
}
