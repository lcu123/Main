import XCTest
@testable import ZestCore

/// The upload engine's "what next?", played through without a phone.
final class UploadPlannerTests: XCTestCase {
    private let now = Fixtures.base

    private func item(_ number: Int, minutesAgo: Double) -> QueueItem {
        Fixtures.item(id: String(format: "video-%02d", number), customerId: 100 + number, recordedAt: Fixtures.at(-minutesAgo))
    }

    private func plan(
        _ items: [QueueItem], uploading: Set<String> = [], registering: Set<String> = [], busy: Set<String> = [], at time: Date? = nil
    ) -> UploadPlanner.Plan {
        UploadPlanner.plan(items: items, uploading: uploading, registering: registering, busy: busy, now: time ?? now)
    }

    // MARK: The rules, one at a time

    func testTheOldestVideoIsRegisteredFirstAndOnlyOneAtATime() {
        // On disk in the order they were saved; the oldest is not first in the file.
        let items = [item(2, minutesAgo: 20), item(1, minutesAgo: 50), item(3, minutesAgo: 5)]
        XCTAssertEqual(plan(items).steps, [.register("video-01")], "one register, for the oldest")
        XCTAssertEqual(plan(items, registering: ["video-01"], busy: ["video-01"]).steps, [], "nothing more while that call is in the air")
    }

    func testAtMostTwoUploadsRunAtOnce() {
        var items = (1...4).map { item($0, minutesAgo: Double(60 - $0)) }
        items[0] = QueueMachine.reduce(items[0], .registered(Fixtures.ticket), now: now)
        // One is uploading: the next may be registered.
        XCTAssertEqual(plan(items, uploading: ["video-01"]).steps, [.register("video-02")])
        // Two are uploading: nothing new starts.
        items[1] = QueueMachine.reduce(items[1], .registered(Fixtures.ticket), now: now)
        XCTAssertEqual(plan(items, uploading: ["video-01", "video-02"]).steps, [])
        // One uploading and one registering also fills both places.
        XCTAssertEqual(
            plan(items, uploading: ["video-01"], registering: ["video-03"], busy: ["video-03"]).steps, [],
            "a video being registered is about to upload, so it counts"
        )
    }

    func testAVideoWithAnAddressAndNoTaskIsStartedAgain() {
        // After a kill: the row says "uploading", the address is still good, no task is alive.
        let uploading = QueueMachine.reduce(item(1, minutesAgo: 10), .registered(Fixtures.ticket), now: now)
        XCTAssertEqual(plan([uploading]).steps, [.startUpload("video-01")])
        XCTAssertEqual(plan([uploading], uploading: ["video-01"]).steps, [], "its task is alive: leave it be")
        // The address has run out: ask for a new one instead.
        XCTAssertEqual(plan([uploading], at: Fixtures.at(25 * 60)).steps, [.register("video-01")])
    }

    func testConfirmingAndDeletingNeverWaitForAPlace() {
        let done = QueueMachine.reduce(
            QueueMachine.reduce(item(1, minutesAgo: 30), .registered(Fixtures.ticket), now: now), .uploadFinished(status: 200), now: now
        )
        var deleted = QueueMachine.reduce(item(2, minutesAgo: 20), .registering, now: now)
        deleted = QueueMachine.reduce(deleted, .deletedByTech, now: now)
        let a = QueueMachine.reduce(item(3, minutesAgo: 10), .registered(Fixtures.ticket), now: now)
        let b = QueueMachine.reduce(item(4, minutesAgo: 5), .registered(Fixtures.ticket), now: now)
        let steps = plan([done, deleted, a, b], uploading: ["video-03", "video-04"]).steps
        XCTAssertEqual(steps, [.complete("video-01"), .tellDeleted("video-02")], "both places are taken, and these two still go")
    }

    func testAVideoTheServerRefusedDoesNotHoldUpTheOnesBehindIt() {
        // The server answered, and said wait: that is this one video's trouble.
        var refused = item(1, minutesAgo: 50)
        refused = QueueMachine.reduce(refused, .callFailed("Something is wrong with this video.", retryAfter: 300), now: now)
        XCTAssertNil(refused.waitingForSignal)
        let next = item(2, minutesAgo: 20)
        let result = plan([refused, next])
        XCTAssertEqual(result.steps, [.register("video-02")])
        XCTAssertEqual(result.lookAgainAt, refused.notBefore, "and the engine knows when to look at the first one again")
        // Once its wait is over it is first again.
        XCTAssertEqual(plan([refused, next], at: now.addingTimeInterval(301)).steps, [.register("video-01")])
    }

    func testNoSignalOnTheOldestHoldsTheLineSoTheyGoInOrder() {
        // No answer at all: the ones behind it would fail the same way.
        var noSignal = item(1, minutesAgo: 50)
        noSignal = QueueMachine.reduce(noSignal, .callFailed("No signal. Please try again.", retryAfter: nil), now: now)
        XCTAssertEqual(noSignal.waitingForSignal, true)
        let next = item(2, minutesAgo: 20)
        let waiting = plan([noSignal, next])
        XCTAssertEqual(waiting.steps, [], "the younger video is not tried out of turn")
        XCTAssertEqual(waiting.lookAgainAt, noSignal.notBefore)
        // Its wait is over (or the tech opened the app): the oldest goes first.
        XCTAssertEqual(plan([noSignal, next], at: now.addingTimeInterval(6)).steps, [.register("video-01")])
        let woken = QueueMachine.reduce(noSignal, .wake, now: now)
        XCTAssertEqual(plan([woken, next]).steps, [.register("video-01")])
        // Through: the mark is gone, and the next one follows.
        let registered = QueueMachine.reduce(woken, .registered(Fixtures.ticket), now: now)
        XCTAssertNil(registered.waitingForSignal)
        XCTAssertEqual(plan([registered, next], uploading: ["video-01"]).steps, [.register("video-02")])
        // Confirming a finished upload is never held back by this.
        let uploaded = QueueMachine.reduce(
            QueueMachine.reduce(item(3, minutesAgo: 10), .registered(Fixtures.ticket), now: now), .uploadFinished(status: 200), now: now
        )
        XCTAssertEqual(plan([noSignal, next, uploaded]).steps, [.complete("video-03")])
    }

    func testFinishedBlockedAndBusyVideosAreLeftAlone() {
        let done = QueueMachine.reduce(item(1, minutesAgo: 30), .serverAlreadyHas(state: .held, emailAt: nil), now: now)
        let blocked = QueueMachine.reduce(item(2, minutesAgo: 20), .stopLocked(message: "locked"), now: now)
        let busy = item(3, minutesAgo: 10)
        XCTAssertEqual(plan([done, blocked, busy], busy: ["video-03"]).steps, [])
    }

    // MARK: A long day with no signal

    /// A tiny stand-in for the phone's engine and the server, so a whole day
    /// can be played: it carries out each step the planner gives and feeds the
    /// result back through the real state machine.
    private final class Day {
        var items: [QueueItem]
        var clock: Date
        var online = false
        var uploading = Set<String>()
        // What the "server" saw.
        var registerOrder: [String] = []
        var registerCalls: [String: Int] = [:]
        var uploadsFinished: [String: Int] = [:]
        var completes: [String: Int] = [:]
        var mostUploadsAtOnce = 0
        var mostRegistersAtOnce = 0

        init(items: [QueueItem], clock: Date) {
            self.items = items
            self.clock = clock
        }

        func apply(_ event: QueueEvent, to id: String) {
            guard let index = items.firstIndex(where: { $0.id == id }) else { return }
            items[index] = QueueMachine.reduce(items[index], event, now: clock)
        }

        /// One turn of the engine: plan, do every step, let the uploads that
        /// were already running finish.
        func turn() {
            let finishing = uploading
            let plan = UploadPlanner.plan(items: items, uploading: uploading, registering: [], busy: [], now: clock)
            var registers = 0
            for step in plan.steps {
                switch step {
                case .register(let id):
                    registers += 1
                    apply(.registering, to: id)
                    guard online else {
                        apply(.callFailed("No signal. Please try again.", retryAfter: nil), to: id)
                        continue
                    }
                    registerCalls[id, default: 0] += 1
                    if !registerOrder.contains(id) { registerOrder.append(id) }
                    apply(.registered(UploadTicket(url: Fixtures.ticket.url, headers: [:], expiresAt: clock.addingTimeInterval(3600))), to: id)
                    uploading.insert(id)
                case .startUpload(let id):
                    uploading.insert(id)
                case .complete(let id):
                    guard online else {
                        apply(.callFailed("No signal. Please try again.", retryAfter: nil), to: id)
                        continue
                    }
                    completes[id, default: 0] += 1
                    apply(.completed(state: .held, emailAt: nil), to: id)
                case .tellDeleted(let id):
                    apply(.deleteConfirmed, to: id)
                }
            }
            mostRegistersAtOnce = max(mostRegistersAtOnce, registers)
            mostUploadsAtOnce = max(mostUploadsAtOnce, uploading.count)
            for id in finishing {
                uploading.remove(id)
                uploadsFinished[id, default: 0] += 1
                apply(.uploadFinished(status: 200), to: id)
            }
            clock = clock.addingTimeInterval(2)
        }

        var waiting: Int { items.waitingCount }
    }

    /// The brief, phase 2: "Done when 20 videos in a row arrive after
    /// airplane-mode tests, with none lost."
    ///
    /// Twenty stops with no signal all day, then the signal comes back.
    /// Every video goes exactly once, oldest first, never more than two at a
    /// time, and the screen never says SENT for one the server does not have.
    func testTwentyVideosSavedWithNoSignalAllArriveOnceInOrder() {
        // Saved half an hour apart, on disk in the order they were saved.
        let saved = (1...20).map { item($0, minutesAgo: Double((21 - $0) * 30)) }
        XCTAssertEqual(saved.count, 20)
        let day = Day(items: saved, clock: now)

        // All day: no signal. The engine keeps trying; nothing is lost and
        // nothing is called sent.
        for _ in 0..<200 {
            day.turn()
            XCTAssertEqual(day.waiting, 20)
            XCTAssertTrue(day.items.allSatisfy { $0.displayText == "WAITING" }, "My Videos says WAITING for every one")
            XCTAssertTrue(day.items.allSatisfy { $0.needsFile }, "every file is still on the phone")
        }
        XCTAssertTrue(day.registerOrder.isEmpty)
        // Only the oldest was ever tried: one call per try, not twenty.
        XCTAssertGreaterThan(day.items[0].attempts, 0)
        XCTAssertTrue(day.items.dropFirst().allSatisfy { $0.attempts == 0 }, "the nineteen behind it were not tried out of turn")

        // The signal is back and the tech opens the app.
        day.online = true
        for id in day.items.map({ $0.id }) {
            day.apply(.wake, to: id)
        }
        var turns = 0
        while day.waiting > 0 && turns < 500 {
            day.turn()
            turns += 1
            // The truth the whole way: SENT only once the server confirmed it.
            for item in day.items where item.displayText == "SENT ✓" {
                XCTAssertEqual(day.completes[item.id], 1, "\(item.id) says SENT, so the server must have confirmed it")
                XCTAssertFalse(item.needsFile, "and its file is off the phone")
            }
        }

        let ids = saved.map { $0.id }
        XCTAssertEqual(day.waiting, 0, "all twenty left the phone (took \(turns) turns)")
        XCTAssertEqual(Set(ids).count, 20)
        XCTAssertEqual(day.registerOrder, ids, "the server heard of them in the order they were recorded")
        for id in ids {
            XCTAssertEqual(day.registerCalls[id], 1, "\(id) was registered once")
            XCTAssertEqual(day.uploadsFinished[id], 1, "\(id) was uploaded once")
            XCTAssertEqual(day.completes[id], 1, "\(id) was confirmed once")
        }
        XCTAssertLessThanOrEqual(day.mostUploadsAtOnce, UploadPlanner.maxUploads, "never more than two uploads at once")
        XCTAssertLessThanOrEqual(day.mostRegistersAtOnce, 1, "never more than one register call at once")
        XCTAssertTrue(day.items.allSatisfy { $0.phase == .done && !$0.needsFile }, "the phone ends holding no files")
        XCTAssertTrue(day.items.allSatisfy { $0.displayText == "SENT ✓" })
    }

    /// The signal comes and goes in the middle of sending. Still exactly
    /// once each, still in order.
    func testASignalThatComesAndGoesStillSendsEachVideoOnce() {
        let saved = (1...20).map { item($0, minutesAgo: Double((21 - $0) * 30)) }
        let day = Day(items: saved, clock: now)
        var turns = 0
        while day.waiting > 0 && turns < 4000 {
            // Three turns with signal, then four without, over and over.
            day.online = turns % 7 < 3
            if turns % 7 == 0 {
                for id in day.items.map({ $0.id }) { day.apply(.wake, to: id) }
            }
            day.turn()
            turns += 1
        }
        XCTAssertEqual(day.waiting, 0, "all twenty left the phone (took \(turns) turns)")
        XCTAssertEqual(day.registerOrder, saved.map { $0.id })
        for id in saved.map({ $0.id }) {
            XCTAssertEqual(day.completes[id], 1, "\(id) was confirmed exactly once")
        }
    }
}
