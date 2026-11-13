import Foundation
import ZestCore

/// The saved copy of `GET /v1/today`. It is the offline source of truth and
/// is wiped as soon as its date is no longer today.
final class TodayStore {
    private let url: URL

    init(directory: URL = Files.root) {
        url = directory.appendingPathComponent("today.json")
    }

    func load(now: Date = Date()) -> TodayResponse? {
        guard let cached = Files.read(TodayResponse.self, from: url) else { return nil }
        guard ZestDates.isToday(cached.date, now: now) else {
            wipe()
            return nil
        }
        return cached
    }

    func save(_ today: TodayResponse) {
        Files.write(today, to: url)
    }

    func wipe() {
        Files.remove(url)
    }
}

/// Which stop a recording is for.
struct RecordTarget: Codable, Equatable {
    var customerId: Int?
    var name: String
    var address: String
    var cityLine: String
    var appointmentIds: [Int]
    var notOnMyList: Bool

    init(stop: Stop) {
        customerId = stop.customerId
        name = stop.name
        address = stop.address
        cityLine = stop.cityLine
        appointmentIds = stop.appointmentIds
        notOnMyList = false
    }

    private init() {
        customerId = nil
        name = "Not on my list"
        address = "The office will match this video."
        cityLine = ""
        appointmentIds = []
        notOnMyList = true
    }

    static let notOnMyList = RecordTarget()
}

/// A recording in progress. Saved before the camera rolls so that a crash or
/// a phone call mid-clip still ends in "keep or record again?".
struct PendingRecording: Codable, Equatable {
    var id: String
    var target: RecordTarget
    var stopDate: String
    var location: VideoLocation?
    var startedAt: Date

    var movieFileName: String { "recording-\(id).mov" }
}

/// A finished clip the tech has not saved yet.
struct Draft: Codable, Equatable {
    var id: String
    var target: RecordTarget
    var fileName: String
    var stopDate: String
    var recordedAt: Date
    var durationSec: Double
    var sizeBytes: Int64
    var location: VideoLocation?
    var createdAt: Date
}

final class DraftStore {
    private let draftURL: URL
    private let pendingURL: URL
    private let videosDirectory: URL

    init(directory: URL = Files.root, videosDirectory: URL = Files.videos) {
        draftURL = directory.appendingPathComponent("draft.json")
        pendingURL = directory.appendingPathComponent("recording.json")
        self.videosDirectory = videosDirectory
    }

    /// The saved draft, if its video file is still there.
    func loadDraft() -> Draft? {
        guard let draft = Files.read(Draft.self, from: draftURL) else { return nil }
        guard Files.exists(videosDirectory.appendingPathComponent(draft.fileName)) else {
            Files.remove(draftURL)
            return nil
        }
        return draft
    }

    func saveDraft(_ draft: Draft) {
        Files.write(draft, to: draftURL)
    }

    /// Forgets the draft. The caller decides what happens to the video file.
    func clearDraft() {
        Files.remove(draftURL)
    }

    func loadPending() -> PendingRecording? {
        Files.read(PendingRecording.self, from: pendingURL)
    }

    func savePending(_ pending: PendingRecording) {
        Files.write(pending, to: pendingURL)
    }

    func clearPending() {
        Files.remove(pendingURL)
    }
}

/// The upload queue on disk. Every change is written before anything else
/// happens, so a kill, a crash or a dead battery never loses a video.
@MainActor
final class QueueStore: ObservableObject {
    @Published private(set) var items: [QueueItem] = []

    private let url: URL
    let videosDirectory: URL

    init(directory: URL = Files.root, videosDirectory: URL = Files.videos) {
        url = directory.appendingPathComponent("queue.json")
        self.videosDirectory = videosDirectory
        items = Files.read([QueueItem].self, from: url) ?? []
        // Progress is only meaningful while a task is alive in this launch.
        for index in items.indices where items[index].phase == .uploading {
            items[index].progress = 0
        }
    }

    func fileURL(for item: QueueItem) -> URL {
        videosDirectory.appendingPathComponent(item.fileName)
    }

    func item(_ id: String) -> QueueItem? {
        items.first { $0.id == id }
    }

    /// Adds a new video. Written to disk before this returns.
    func add(_ item: QueueItem) {
        guard self.item(item.id) == nil else { return }
        Files.protect(fileURL(for: item))
        items.append(item)
        save()
    }

    /// Runs one event through the state machine and stores the result.
    /// The video file is deleted the moment the phone no longer needs it.
    @discardableResult
    func apply(_ event: QueueEvent, to id: String, now: Date = Date()) -> QueueItem? {
        guard let index = items.firstIndex(where: { $0.id == id }) else { return nil }
        let before = items[index]
        let after = QueueMachine.reduce(before, event, now: now)
        guard after != before else { return before }

        if !after.needsFile {
            Files.remove(fileURL(for: after))
        }
        if after.phase == .removed {
            items.remove(at: index)
        } else {
            items[index] = after
        }
        // A progress tick is not worth a disk write; everything else is.
        if case .progress = event {} else { save() }
        return after
    }

    /// Drops finished rows from earlier days. Videos still waiting are never dropped.
    func pruneOldDays(today: String) {
        let kept = items.filter { item in
            if item.isWaiting { return true }
            return item.stopDate == today
        }
        if kept.count != items.count {
            items = kept
            save()
        }
    }

    /// Bytes of video the app is holding on the phone right now.
    var heldBytes: Int64 {
        Files.totalBytes(in: videosDirectory)
    }

    /// Deletes any video file that no queue row (or the draft) points at.
    func removeOrphanFiles(keeping extra: Set<String>) {
        let wanted = Set(items.filter { $0.needsFile }.map { $0.fileName }).union(extra)
        for name in Files.fileNames(in: videosDirectory) where !wanted.contains(name) {
            Files.remove(videosDirectory.appendingPathComponent(name))
        }
    }

    private func save() {
        Files.write(items, to: url)
    }
}

#if targetEnvironment(simulator)
extension QueueStore {
    /// Screenshot mode only: show these rows. Nothing is written to disk.
    func loadScreenshotItems(_ fixture: [QueueItem]) {
        items = fixture
    }
}
#endif
