import XCTest
import ZestCore
@testable import ZestVideo

/// The on-disk queue: the part that must never lose a video.
final class QueueStoreTests: XCTestCase {
    private func makeFolders() throws -> (root: URL, videos: URL) {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("zest-tests-\(UUID().uuidString)")
        let videos = root.appendingPathComponent("Videos")
        try FileManager.default.createDirectory(at: videos, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        return (root, videos)
    }

    private func makeItem(id: String = QueueItem.newID(), customerId: Int = 2, stopDate: String = "2026-11-13") -> QueueItem {
        QueueItem(
            id: id, stopDate: stopDate, customerId: customerId, customerName: "Jordan Quillfeather",
            appointmentIds: [20], notOnMyList: false, recordedAt: Date(), durationSec: 14.6, sizeBytes: 11,
            location: nil, signals: .allUnknown, override: false, recommend: false, spokenText: nil,
            fileName: "\(id).mp4"
        )
    }

    private let ticket = UploadTicket(
        url: URL(string: "https://bucket.example.com/v.mp4?sig=1")!,
        headers: ["Content-Type": "video/mp4"], expiresAt: Date().addingTimeInterval(3600)
    )

    @MainActor
    func testQueueSurvivesARelaunch() throws {
        let folders = try makeFolders()
        let item = makeItem()
        try Data("hello video".utf8).write(to: folders.videos.appendingPathComponent(item.fileName))

        let store = QueueStore(directory: folders.root, videosDirectory: folders.videos)
        store.add(item)
        store.apply(.registering, to: item.id)
        store.apply(.registered(ticket), to: item.id)
        store.apply(.progress(0.5), to: item.id)

        // A fresh store reads the same file, as after the app is killed.
        let relaunched = QueueStore(directory: folders.root, videosDirectory: folders.videos)
        XCTAssertEqual(relaunched.items.count, 1)
        let restored = try XCTUnwrap(relaunched.item(item.id))
        XCTAssertEqual(restored.phase, .uploading)
        XCTAssertEqual(restored.uploadURL, ticket.url)
        XCTAssertTrue(restored.serverMayKnow)
        XCTAssertEqual(restored.progress, 0, "progress starts over; the task reports it again")
        XCTAssertTrue(FileManager.default.fileExists(atPath: relaunched.fileURL(for: restored).path))
    }

    @MainActor
    func testFileIsDeletedOnlyAfterTheServerConfirms() throws {
        let folders = try makeFolders()
        let item = makeItem()
        let file = folders.videos.appendingPathComponent(item.fileName)
        try Data("hello video".utf8).write(to: file)
        let store = QueueStore(directory: folders.root, videosDirectory: folders.videos)
        store.add(item)

        store.apply(.registered(ticket), to: item.id)
        store.apply(.uploadFinished(status: 200), to: item.id)
        XCTAssertTrue(FileManager.default.fileExists(atPath: file.path), "the bucket has it, but the server has not confirmed")

        store.apply(.completed(state: .held, emailAt: nil), to: item.id)
        XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
        XCTAssertEqual(store.item(item.id)?.phase, .done)
        XCTAssertEqual(store.heldBytes, 0)
    }

    @MainActor
    func testDeleteRemovesTheFileAtOnce() throws {
        let folders = try makeFolders()
        let item = makeItem()
        let file = folders.videos.appendingPathComponent(item.fileName)
        try Data("hello video".utf8).write(to: file)
        let store = QueueStore(directory: folders.root, videosDirectory: folders.videos)
        store.add(item)

        // Never reached the server: gone completely.
        store.apply(.deletedByTech, to: item.id)
        XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
        XCTAssertNil(store.item(item.id))
    }

    @MainActor
    func testDeleteAfterRegisterKeepsARowUntilTheServerIsTold() throws {
        let folders = try makeFolders()
        let item = makeItem()
        let file = folders.videos.appendingPathComponent(item.fileName)
        try Data("hello video".utf8).write(to: file)
        let store = QueueStore(directory: folders.root, videosDirectory: folders.videos)
        store.add(item)
        store.apply(.registering, to: item.id)
        store.apply(.registered(ticket), to: item.id)

        store.apply(.deletedByTech, to: item.id)
        XCTAssertFalse(FileManager.default.fileExists(atPath: file.path))
        XCTAssertEqual(store.item(item.id)?.phase, .deleting)

        store.apply(.deleteConfirmed, to: item.id)
        XCTAssertNil(store.item(item.id))
    }

    @MainActor
    func testOldFinishedRowsArePrunedButWaitingVideosNeverAre() throws {
        let folders = try makeFolders()
        let store = QueueStore(directory: folders.root, videosDirectory: folders.videos)
        let yesterdayDone = makeItem(customerId: 1, stopDate: "2026-11-12")
        let yesterdayWaiting = makeItem(customerId: 2, stopDate: "2026-11-12")
        let todayDone = makeItem(customerId: 3, stopDate: "2026-11-13")
        for item in [yesterdayDone, yesterdayWaiting, todayDone] {
            try Data("x".utf8).write(to: folders.videos.appendingPathComponent(item.fileName))
            store.add(item)
        }
        store.apply(.serverAlreadyHas(state: .sent, emailAt: nil), to: yesterdayDone.id)
        store.apply(.serverAlreadyHas(state: .held, emailAt: nil), to: todayDone.id)

        store.pruneOldDays(today: "2026-11-13")
        XCTAssertEqual(Set(store.items.map { $0.id }), [yesterdayWaiting.id, todayDone.id])
    }

    @MainActor
    func testOrphanFilesAreCleanedUp() throws {
        let folders = try makeFolders()
        let store = QueueStore(directory: folders.root, videosDirectory: folders.videos)
        let item = makeItem()
        try Data("x".utf8).write(to: folders.videos.appendingPathComponent(item.fileName))
        try Data("x".utf8).write(to: folders.videos.appendingPathComponent("stray.mp4"))
        try Data("x".utf8).write(to: folders.videos.appendingPathComponent("draft.mp4"))
        store.add(item)

        store.removeOrphanFiles(keeping: ["draft.mp4"])
        let left = Set(try FileManager.default.contentsOfDirectory(atPath: folders.videos.path))
        XCTAssertEqual(left, [item.fileName, "draft.mp4"])
    }

    func testDraftStoreKeepsADraftOnlyWhileItsFileExists() throws {
        let folders = try makeFolders()
        let store = DraftStore(directory: folders.root, videosDirectory: folders.videos)
        XCTAssertNil(store.loadDraft())

        let stop = Stop(customerId: 2, name: "Jordan Quillfeather", lastName: "Quillfeather", address: "123 Harbor View Dr")
        let draft = Draft(
            id: "d1", target: RecordTarget(stop: stop), fileName: "d1.mp4", stopDate: "2026-11-13",
            recordedAt: Date(timeIntervalSince1970: 1_800_000_000), durationSec: 12, sizeBytes: 5,
            location: VideoLocation(lat: 38.61, lng: -121.31, accuracyM: 8),
            createdAt: Date(timeIntervalSince1970: 1_800_000_015)
        )
        store.saveDraft(draft)
        XCTAssertNil(store.loadDraft(), "no video file, no draft")

        try Data("x".utf8).write(to: folders.videos.appendingPathComponent("d1.mp4"))
        store.saveDraft(draft)
        XCTAssertEqual(store.loadDraft(), draft)

        store.clearDraft()
        XCTAssertNil(store.loadDraft())
    }

    func testPendingRecordingRoundTrip() throws {
        let folders = try makeFolders()
        let store = DraftStore(directory: folders.root, videosDirectory: folders.videos)
        let pending = PendingRecording(
            id: "p1", target: .notOnMyList, stopDate: "2026-11-13", location: nil,
            startedAt: Date(timeIntervalSince1970: 1_800_000_000)
        )
        store.savePending(pending)
        XCTAssertEqual(store.loadPending(), pending)
        XCTAssertEqual(pending.movieFileName, "recording-p1.mov")
        XCTAssertTrue(pending.target.notOnMyList)
        XCTAssertNil(pending.target.customerId)
        store.clearPending()
        XCTAssertNil(store.loadPending())
    }
}
