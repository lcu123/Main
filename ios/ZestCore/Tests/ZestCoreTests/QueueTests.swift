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

    /// A server that refuses every fresh address must not make the phone
    /// spin: ask, upload, 403, ask, upload, 403, with no pause.
    func testAnAddressRefusedAgainAndAgainStartsToWait() {
        var item = run(Fixtures.item(), .registered(Fixtures.ticket), .uploadFinished(status: 403))
        XCTAssertNil(item.notBefore, "the first refusal is just an address that ran out")
        item = run(item, .registered(Fixtures.ticket), .uploadFinished(status: 403))
        XCTAssertNil(item.notBefore, "and the second")
        item = run(item, .registered(Fixtures.ticket), .uploadFinished(status: 403))
        XCTAssertNotNil(item.notBefore, "the third time it waits before asking again")
        XCTAssertEqual(item.phase, .queued, "and the video is still there to send")
        XCTAssertTrue(item.needsFile)

        // Once an upload goes through, the count starts again.
        var fine = run(Fixtures.item(), .registered(Fixtures.ticket), .uploadFinished(status: 403), .registered(Fixtures.ticket), .uploadFinished(status: 403))
        fine = run(fine, .registered(Fixtures.ticket), .uploadFinished(status: 200))
        XCTAssertEqual(fine.phase, .uploaded)
        XCTAssertNil(fine.refusedInARow)
    }

    /// A queue file written before `refusedInARow` existed still loads.
    func testAnOlderQueueFileStillLoads() throws {
        let item = Fixtures.item()
        let data = try ZestJSON.encoder().encode([item])
        var text = String(decoding: data, as: UTF8.self)
        XCTAssertFalse(text.contains("refusedInARow"), "a nil count is simply not written")
        text = text.replacingOccurrences(of: "\"refusedInARow\":null,", with: "")
        let back = try ZestJSON.decoder().decode([QueueItem].self, from: Data(text.utf8))
        XCTAssertEqual(back, [item])
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

    // MARK: Killed at every point

    /// What the app does with a row when it starts again: read it back from
    /// disk, then take the next step for the phase it is in.
    private func relaunch(_ item: QueueItem) throws -> QueueItem {
        let data = try ZestJSON.encoder().encode([item])
        return try XCTUnwrap(try ZestJSON.decoder().decode([QueueItem].self, from: data).first)
    }

    /// The app is killed after each step in turn. Every time, the row read
    /// back from disk still gets to "sent" with the same ID, so the server
    /// can never end up with two videos for one recording.
    func testAKillAtAnyPointStillEndsWithOneSentVideo() throws {
        let steps: [QueueEvent] = [
            .registering,
            .registered(Fixtures.ticket),
            .progress(0.5),
            .uploadFinished(status: 200),
            .completed(state: .held, emailAt: Fixtures.at(10)),
        ]
        for killAfter in 0...steps.count {
            var item = Fixtures.item()
            for step in steps.prefix(killAfter) {
                item = run(item, step)
            }
            item = try relaunch(item)
            XCTAssertEqual(item.id, Fixtures.item().id, "the ID never changes (kill after step \(killAfter))")

            // The relaunch: wake, then whatever that phase needs.
            item = run(item, .wake)
            switch item.phase {
            case .queued:
                item = run(item, .registering, .registered(Fixtures.ticket), .uploadFinished(status: 200),
                           .completed(state: .held, emailAt: Fixtures.at(10)))
            case .uploading:
                // The background upload finished while the app was gone, or is started again.
                XCTAssertNotNil(item.uploadURL, "the upload address was kept on disk")
                item = run(item, .uploadFinished(status: 200), .completed(state: .held, emailAt: Fixtures.at(10)))
            case .uploaded:
                item = run(item, .completed(state: .held, emailAt: Fixtures.at(10)))
            case .done:
                break
            default:
                XCTFail("unexpected phase \(item.phase) after a kill at step \(killAfter)")
            }
            XCTAssertEqual(item.phase, .done, "kill after step \(killAfter)")
            XCTAssertEqual(item.displayText, "SENT ✓")
            XCTAssertFalse(item.needsFile, "the phone's copy can go")
        }
    }

    /// Killed while "complete" was in the air: the server did confirm, the
    /// phone never heard. Either way of finding out ends in one sent video.
    func testAKillWhileConfirmingIsSettledByEitherAnswer() throws {
        let uploaded = try relaunch(run(Fixtures.item(), .registering, .registered(Fixtures.ticket), .uploadFinished(status: 200)))
        XCTAssertEqual(uploaded.phase, .uploaded)
        XCTAssertTrue(uploaded.needsFile, "the file is kept until the server says it has it")

        let askedAgain = run(uploaded, .completed(state: .held, emailAt: nil))
        let toldByToday = run(uploaded, .serverState(.held, emailAt: nil))
        XCTAssertEqual(askedAgain.phase, .done)
        XCTAssertEqual(toldByToday.phase, .done)
        // A late second answer changes nothing.
        XCTAssertEqual(run(toldByToday, .completed(state: .held, emailAt: nil)).phase, .done)
    }

    /// Killed right after SAVE, before register answered: the row says the
    /// server may know, and asking again with the same ID is safe.
    func testAKillBeforeRegisterAnsweredAsksAgainWithTheSameID() throws {
        let item = try relaunch(run(Fixtures.item(), .registering))
        XCTAssertEqual(item.phase, .queued)
        XCTAssertTrue(item.serverMayKnow)
        XCTAssertEqual(item.registerRequest.id, Fixtures.item().id)
        // The server had finished the first one after all: it just says so.
        XCTAssertEqual(run(item, .serverAlreadyHas(state: .held, emailAt: nil)).phase, .done)
        // Or it had not: a fresh address, and on it goes.
        XCTAssertEqual(run(item, .registered(Fixtures.ticket)).phase, .uploading)
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
        XCTAssertEqual(items.liveItem(customerId: 2, day: "2026-11-13")?.id, "b", "the newest one for the stop")
        XCTAssertEqual(items.liveItem(customerId: 3, day: "2026-11-13")?.id, "c")
        XCTAssertNil(items.liveItem(customerId: 4, day: "2026-11-13"), "a blocked video does not count for its stop")
        XCTAssertEqual(items.customersWithVideo(day: "2026-11-13"), [2, 3])
    }

    /// A video from an earlier day that is still on the phone (saved late,
    /// or waiting for signal overnight) belongs to that day's visit.
    func testAVideoFromAnEarlierDayDoesNotCountForTodaysStop() {
        var yesterday = Fixtures.item(id: "y", customerId: 2, recordedAt: Fixtures.at(-24 * 60))
        yesterday.stopDate = "2026-11-12"
        let today = Fixtures.item(id: "t", customerId: 3, recordedAt: Fixtures.at(0))
        let items = [yesterday, today]

        XCTAssertEqual(items.waitingCount, 2, "both still have to leave the phone")
        XCTAssertNil(items.liveItem(customerId: 2, day: "2026-11-13"), "yesterday's video is not today's video for the same customer")
        XCTAssertEqual(items.customersWithVideo(day: "2026-11-13"), [3], "so today's stop for that customer still asks for one")
        XCTAssertEqual(items.liveItem(customerId: 2, day: "2026-11-12")?.id, "y", "it is still that day's video")
        XCTAssertEqual(items.forDay("2026-11-13").map { $0.id }, ["t"])

        // A phone whose calendar is a day ahead of the office's files a video
        // under tomorrow's date: it is still today's.
        var ahead = Fixtures.item(id: "a", customerId: 4)
        ahead.stopDate = "2026-11-14"
        XCTAssertEqual([ahead].liveItem(customerId: 4, day: "2026-11-13")?.id, "a")
        // A date that cannot be read never hides a video.
        var odd = Fixtures.item(id: "o", customerId: 5)
        odd.stopDate = "not a date"
        XCTAssertEqual([odd].customersWithVideo(day: "2026-11-13"), [5])
    }
}
