import XCTest
@testable import ZestCore

final class QueueTests: XCTestCase {
    private let now = Fixtures.base

    private func run(_ item: QueueItem, _ events: QueueEvent...) -> QueueItem {
        events.reduce(item) { QueueMachine.reduce($0, $1, now: now) }
    }

    func testNewItemStartsQueuedWithALowercaseUUID() {
        let item = Fixtures.item()
        XCTAssertEqual(item.phase, .queued)
        XCTAssertFalse(item.serverMayKnow)
        XCTAssertEqual(item.display, .waiting)
        XCTAssertEqual(item.displayText, "WAITING")
        let id = QueueItem.newID()
        XCTAssertEqual(id, id.lowercased())
        XCTAssertNotNil(UUID(uuidString: id))
        XCTAssertNotEqual(id, QueueItem.newID())
    }

    func testHappyPath() {
        var item = run(Fixtures.item(), .registering)
        XCTAssertTrue(item.serverMayKnow)
        XCTAssertEqual(item.phase, .queued)

        item = run(item, .registered(Fixtures.ticket))
        XCTAssertEqual(item.phase, .uploading)
        XCTAssertEqual(item.uploadURL, Fixtures.ticket.url)
        XCTAssertEqual(item.uploadHeaders, ["Content-Type": "video/mp4"])
        XCTAssertEqual(item.display, .waiting, "no bytes sent yet")
        XCTAssertTrue(item.needsFile)

        item = run(item, .progress(0.404))
        XCTAssertEqual(item.display, .sending(percent: 40))
        XCTAssertEqual(item.displayText, "SENDING 40%")

        item = run(item, .progress(0.2))
        XCTAssertEqual(item.display, .sending(percent: 40), "progress never goes backwards")

        item = run(item, .uploadFinished(status: 200))
        XCTAssertEqual(item.phase, .uploaded)
        XCTAssertEqual(item.display, .sending(percent: 99), "not Sent until the server confirms")
        XCTAssertTrue(item.needsFile, "the file stays until complete answers 200")

        item = run(item, .completed(state: .held, emailAt: Fixtures.at(10)))
        XCTAssertEqual(item.phase, .done)
        XCTAssertEqual(item.display, .sent)
        XCTAssertEqual(item.displayText, "SENT ✓")
        XCTAssertFalse(item.needsFile)
        XCTAssertEqual(item.emailAt, Fixtures.at(10))
        XCTAssertEqual(item.sentAt, now)
        XCTAssertNil(item.uploadURL)
        XCTAssertEqual(item.id, Fixtures.item().id, "the ID never changes")
    }

    func testARepeatRegisterForAVideoTheServerHasIsDone() {
        let item = run(Fixtures.item(), .registering, .serverAlreadyHas(state: .sent, emailAt: nil))
        XCTAssertEqual(item.phase, .done)
        XCTAssertEqual(item.serverState, .sent)
    }

    func testARepeatRegisterForARetiredVideoRemovesIt() {
        XCTAssertEqual(run(Fixtures.item(), .serverAlreadyHas(state: .replaced, emailAt: nil)).phase, .removed)
        XCTAssertEqual(run(Fixtures.item(), .serverAlreadyHas(state: .pulled, emailAt: nil)).phase, .removed)
    }

    func testNoUploadAddressIsRetriedNotTreatedAsSent() {
        let item = run(Fixtures.item(), .serverAlreadyHas(state: .awaitingUpload, emailAt: nil))
        XCTAssertEqual(item.phase, .queued)
        XCTAssertEqual(item.attempts, 1)
        XCTAssertNotNil(item.notBefore)
    }

    func testExpiredURLGetsAFreshOneWithoutCountingAsAFailure() {
        let item = run(Fixtures.item(), .registered(Fixtures.ticket), .progress(0.5), .uploadFinished(status: 403))
        XCTAssertEqual(item.phase, .queued)
        XCTAssertNil(item.uploadURL)
        XCTAssertEqual(item.attempts, 0)
        XCTAssertNil(item.notBefore)
        XCTAssertEqual(item.progress, 0)
    }

    func testRefusedUploadBacksOff() {
        let item = run(Fixtures.item(), .registered(Fixtures.ticket), .uploadFinished(status: 500))
        XCTAssertEqual(item.phase, .queued)
        XCTAssertEqual(item.attempts, 1)
        XCTAssertFalse(item.mayRetry(now: now))
        XCTAssertTrue(item.mayRetry(now: now.addingTimeInterval(6)))
    }

    func testADeadUploadKeepsItsURLAndRetries() {
        let item = run(Fixtures.item(), .registered(Fixtures.ticket), .progress(0.7), .uploadFailed("The network connection was lost."))
        XCTAssertEqual(item.phase, .uploading)
        XCTAssertEqual(item.uploadURL, Fixtures.ticket.url)
        XCTAssertEqual(item.progress, 0)
        XCTAssertEqual(item.attempts, 1)
        XCTAssertEqual(item.lastError, "The network connection was lost.")
    }

    func testNotUploadedSendsItRoundAgain() {
        let item = run(Fixtures.item(), .registered(Fixtures.ticket), .uploadFinished(status: 200), .notUploaded)
        XCTAssertEqual(item.phase, .queued)
        XCTAssertNil(item.uploadURL)
        XCTAssertTrue(item.needsFile)
    }

    func testCallFailuresNeverDropTheVideo() {
        var item = Fixtures.item()
        for _ in 0..<12 {
            item = run(item, .callFailed("No signal", retryAfter: nil))
        }
        XCTAssertEqual(item.phase, .queued)
        XCTAssertEqual(item.attempts, 12)
        XCTAssertTrue(item.isWaiting)

        let uploaded = run(Fixtures.item(), .registered(Fixtures.ticket), .uploadFinished(status: 200), .callFailed("503", retryAfter: nil))
        XCTAssertEqual(uploaded.phase, .uploaded, "a failed complete is retried, the upload is not redone")
    }

    func testRetryAfterIsHonoured() {
        let item = run(Fixtures.item(), .callFailed("slow down", retryAfter: 90))
        XCTAssertEqual(item.notBefore, now.addingTimeInterval(90))
    }

    func testBackoffGrowsAndIsCapped() {
        XCTAssertEqual(QueueMachine.retryDelay(attempts: 1), 5)
        XCTAssertEqual(QueueMachine.retryDelay(attempts: 2), 10)
        XCTAssertEqual(QueueMachine.retryDelay(attempts: 3), 20)
        XCTAssertEqual(QueueMachine.retryDelay(attempts: 50), 300)
        XCTAssertEqual(QueueMachine.retryDelay(attempts: 0), 5)
    }

    func testWakeClearsBackoff() {
        let item = run(Fixtures.item(), .callFailed("No signal", retryAfter: nil), .wake)
        XCTAssertNil(item.notBefore)
        XCTAssertTrue(item.mayRetry(now: now))
        XCTAssertEqual(item.attempts, 1)
    }

    func testLockedStopBlocksTheVideoWithTheServersWords() {
        let item = run(Fixtures.item(), .registering, .stopLocked(message: "This video already went to the customer. Call the office to change it."))
        XCTAssertEqual(item.phase, .blocked)
        XCTAssertEqual(item.blockedMessage, "This video already went to the customer. Call the office to change it.")
        XCTAssertFalse(item.needsFile)
        XCTAssertFalse(item.countsForStop)
        XCTAssertEqual(item.displayText, "NOT SENT")
        // Nothing wakes a blocked video back up.
        XCTAssertEqual(run(item, .wake, .registered(Fixtures.ticket)).phase, .blocked)
    }

    func testLostFileIsBlockedWithAClearMessage() {
        let item = run(Fixtures.item(), .registered(Fixtures.ticket), .fileLost)
        XCTAssertEqual(item.phase, .blocked)
        XCTAssertNotNil(item.blockedMessage)
    }

    func testDeleteBeforeTheServerKnowsIsInstant() {
        XCTAssertEqual(run(Fixtures.item(), .deletedByTech).phase, .removed)
    }

    func testDeleteAfterTheServerMayKnowWaitsForTheServer() {
        var item = run(Fixtures.item(), .registering, .registered(Fixtures.ticket), .progress(0.5), .deletedByTech)
        XCTAssertEqual(item.phase, .deleting)
        XCTAssertNil(item.uploadURL)
        XCTAssertFalse(item.needsFile, "the file goes at once")
        XCTAssertFalse(item.countsForStop, "the stop is free to record again")

        // Late answers from the dead upload change nothing.
        XCTAssertEqual(run(item, .uploadFinished(status: 200), .completed(state: .held, emailAt: nil)).phase, .deleting)

        item = run(item, .callFailed("No signal", retryAfter: nil))
        XCTAssertEqual(item.phase, .deleting)
        XCTAssertEqual(item.attempts, 1)

        XCTAssertEqual(run(item, .deleteConfirmed).phase, .removed)
    }

    func testDeleteOfASentVideoAlsoTellsTheServer() {
        let item = run(Fixtures.item(), .registering, .registered(Fixtures.ticket), .uploadFinished(status: 200),
                       .completed(state: .held, emailAt: Fixtures.at(10)), .deletedByTech)
        XCTAssertEqual(item.phase, .deleting)
    }

    func testRemovedIsFinal() {
        let item = run(Fixtures.item(), .deletedByTech, .registered(Fixtures.ticket), .wake, .completed(state: .held, emailAt: nil))
        XCTAssertEqual(item.phase, .removed)
    }

    func testServerWordFromTodayFinishesAVideoWhoseAnswerWasLost() {
        // complete() reached the server but the 200 never reached the phone.
        let item = run(Fixtures.item(), .registered(Fixtures.ticket), .uploadFinished(status: 200), .serverState(.held, emailAt: Fixtures.at(8)))
        XCTAssertEqual(item.phase, .done)
        XCTAssertEqual(item.emailAt, Fixtures.at(8))

        let stillUploading = run(Fixtures.item(), .registered(Fixtures.ticket), .serverState(.awaitingUpload, emailAt: nil))
        XCTAssertEqual(stillUploading.phase, .uploading, "awaiting_upload changes nothing")
    }

    func testAPulledVideoDisappearsFromTheDay() {
        let done = run(Fixtures.item(), .registered(Fixtures.ticket), .uploadFinished(status: 200), .completed(state: .held, emailAt: nil))
        XCTAssertEqual(run(done, .serverState(.sent, emailAt: nil)).serverState, .sent)
        XCTAssertEqual(run(done, .serverState(.pulled, emailAt: nil)).phase, .removed)
    }

    func testNonsenseEventsAreIgnored() {
        let item = Fixtures.item()
        XCTAssertEqual(run(item, .uploadFinished(status: 200)), item)
        XCTAssertEqual(run(item, .completed(state: .held, emailAt: nil)), item)
        XCTAssertEqual(run(item, .progress(0.5)), item)
        XCTAssertEqual(run(item, .deleteConfirmed), item)
    }

    func testFreshURLRule() {
        var item = Fixtures.item()
        XCTAssertTrue(item.needsFreshURL(now: now), "no URL yet")
        item = run(item, .registered(Fixtures.ticket))
        XCTAssertFalse(item.needsFreshURL(now: now))
        XCTAssertTrue(item.needsFreshURL(now: Fixtures.at(24 * 60 - 2)), "two minutes from running out")
        XCTAssertTrue(item.needsFreshURL(now: Fixtures.at(25 * 60)))
    }

    func testItemSurvivesADiskRoundTrip() throws {
        let item = run(Fixtures.item(), .registering, .registered(Fixtures.ticket), .progress(0.5))
        let data = try ZestJSON.encoder().encode([item])
        let back = try ZestJSON.decoder().decode([QueueItem].self, from: data)
        XCTAssertEqual(back, [item])
    }

    func testArrayHelpers() {
        let waiting = Fixtures.item(id: "a", customerId: 2, recordedAt: Fixtures.at(0))
        let newer = run(Fixtures.item(id: "b", customerId: 2, recordedAt: Fixtures.at(5)), .registered(Fixtures.ticket))
        let done = run(Fixtures.item(id: "c", customerId: 3), .serverAlreadyHas(state: .held, emailAt: nil))
        let blocked = run(Fixtures.item(id: "d", customerId: 4), .stopLocked(message: "locked"))
        let offList = Fixtures.item(id: "e", customerId: nil, notOnMyList: true)
        let items = [waiting, newer, done, blocked, offList]

        XCTAssertEqual(items.waitingCount, 3)
        XCTAssertEqual(items.liveItem(customerId: 2)?.id, "b", "the newest one for the stop")
        XCTAssertEqual(items.liveItem(customerId: 3)?.id, "c")
        XCTAssertNil(items.liveItem(customerId: 4), "a blocked video does not count for its stop")
        XCTAssertEqual(items.customersWithVideo, [2, 3])
    }
}
