import Foundation

/// One video waiting on (or just sent from) the phone.
///
/// The record is written to disk before any network call and holds
/// everything needed to finish the job after a crash, a restart or a day
/// with no signal. The `id` is made on the phone and never changes, so a
/// repeat can never make a second video or a second email.
public struct QueueItem: Codable, Equatable, Identifiable, Sendable {
    public enum Phase: String, Codable, Equatable, Sendable {
        /// On disk; needs `POST /v1/videos` for an upload URL.
        case queued
        /// Has a URL; a background upload task should be running.
        case uploading
        /// The bucket said 200; needs `POST /complete`.
        case uploaded
        /// The server confirmed it. The file is deleted; a tiny row remains for today.
        case done
        /// The server refused it for good (the stop is locked). File deleted.
        case blocked
        /// Deleted by the tech; the server still has to be told.
        case deleting
        /// Finished with. The store drops the row and the file.
        case removed
    }

    public var id: String
    public var stopDate: String
    public var customerId: Int?
    public var customerName: String
    public var appointmentIds: [Int]
    public var notOnMyList: Bool
    public var recordedAt: Date
    public var durationSec: Double
    public var sizeBytes: Int64
    public var location: VideoLocation?
    public var signals: Signals
    public var override: Bool
    public var recommend: Bool
    public var spokenText: String?
    /// File name inside the app's private Videos folder.
    public var fileName: String

    public var phase: Phase
    public var uploadURL: URL?
    public var uploadHeaders: [String: String]
    public var uploadExpiresAt: Date?
    /// 0...1 for the running upload. Not worth a disk write per tick.
    public var progress: Double
    public var attempts: Int
    /// Do not retry before this time (backoff after a failure).
    public var notBefore: Date?
    public var lastError: String?
    /// True once any call about this video may have reached the server.
    public var serverMayKnow: Bool
    public var serverState: VideoState?
    public var emailAt: Date?
    public var sentAt: Date?
    /// Why the server refused it, in the server's own plain words.
    public var blockedMessage: String?

    public init(
        id: String, stopDate: String, customerId: Int?, customerName: String, appointmentIds: [Int],
        notOnMyList: Bool, recordedAt: Date, durationSec: Double, sizeBytes: Int64, location: VideoLocation?,
        signals: Signals, override: Bool, recommend: Bool, spokenText: String?, fileName: String
    ) {
        self.id = id
        self.stopDate = stopDate
        self.customerId = customerId
        self.customerName = customerName
        self.appointmentIds = appointmentIds
        self.notOnMyList = notOnMyList
        self.recordedAt = recordedAt
        self.durationSec = durationSec
        self.sizeBytes = sizeBytes
        self.location = location
        self.signals = signals
        self.override = override
        self.recommend = recommend
        self.spokenText = spokenText
        self.fileName = fileName
        self.phase = .queued
        self.uploadURL = nil
        self.uploadHeaders = [:]
        self.uploadExpiresAt = nil
        self.progress = 0
        self.attempts = 0
        self.notBefore = nil
        self.lastError = nil
        self.serverMayKnow = false
        self.serverState = nil
        self.emailAt = nil
        self.sentAt = nil
        self.blockedMessage = nil
    }

    /// New lowercase UUID v4, as the contract asks.
    public static func newID() -> String {
        UUID().uuidString.lowercased()
    }

    public var registerRequest: RegisterVideoRequest {
        RegisterVideoRequest(
            id: id, stopDate: stopDate, customerId: notOnMyList ? nil : customerId,
            appointmentIds: notOnMyList ? [] : appointmentIds, notOnMyList: notOnMyList,
            recordedAt: recordedAt, durationSec: durationSec, sizeBytes: sizeBytes, location: location,
            signals: signals, override: override, recommend: recommend, spokenText: spokenText
        )
    }

    // MARK: Derived

    /// Still has work to do on this phone.
    public var isWaiting: Bool {
        phase == .queued || phase == .uploading || phase == .uploaded
    }

    /// The phone still needs the video file.
    public var needsFile: Bool { isWaiting }

    /// Counts as "this stop has a video" for the Today list.
    public var countsForStop: Bool { isWaiting || phase == .done }

    /// The upload URL is missing or about to run out: register again first.
    public func needsFreshURL(now: Date) -> Bool {
        guard uploadURL != nil else { return true }
        guard let expires = uploadExpiresAt else { return false }
        return expires.timeIntervalSince(now) < 5 * 60
    }

    public func mayRetry(now: Date) -> Bool {
        guard let notBefore else { return true }
        return now >= notBefore
    }

    public enum Display: Equatable, Sendable {
        case waiting
        case sending(percent: Int)
        case sent
        case blocked
    }

    /// What the My videos screen says. It always tells the truth: "Sent" only
    /// after the server has confirmed the whole file.
    public var display: Display {
        switch phase {
        case .queued:
            return .waiting
        case .uploading:
            let percent = Int((progress * 100).rounded(.down))
            return percent > 0 ? .sending(percent: min(percent, 99)) : .waiting
        case .uploaded:
            return .sending(percent: 99)
        case .done:
            return .sent
        case .blocked, .deleting, .removed:
            return .blocked
        }
    }

    public var displayText: String {
        switch display {
        case .waiting: return "WAITING"
        case .sending(let percent): return "SENDING \(percent)%"
        case .sent: return "SENT ✓"
        case .blocked: return "NOT SENT"
        }
    }
}

/// What can happen to a queued video.
public enum QueueEvent: Equatable, Sendable {
    /// A register call is about to go out.
    case registering
    /// Register answered with an upload URL.
    case registered(UploadTicket)
    /// Register answered `upload: null`: the server already has this video.
    case serverAlreadyHas(state: VideoState, emailAt: Date?)
    /// Register answered `409 visit_has_video`: the stop is locked.
    case stopLocked(message: String)
    case progress(Double)
    /// The upload task finished with this HTTP status.
    case uploadFinished(status: Int)
    /// The upload task died without an HTTP answer.
    case uploadFailed(String)
    /// `complete` answered 200.
    case completed(state: VideoState, emailAt: Date?)
    /// `complete` answered `409 not_uploaded`.
    case notUploaded
    /// A register or complete call failed for now (no signal, 5xx, 429).
    case callFailed(String, retryAfter: TimeInterval?)
    /// The file is gone from the phone before the server confirmed it.
    case fileLost
    /// Fresh word from `/v1/today` about this video.
    case serverState(VideoState, emailAt: Date?)
    /// The tech deleted it, or recorded again for the same stop.
    case deletedByTech
    /// The server acknowledged the delete (or never knew the video).
    case deleteConfirmed
    /// The app came to the front: forget backoff and try now.
    case wake
}

/// The queue state machine. Pure, so every path is unit-tested.
public enum QueueMachine {
    public static func retryDelay(attempts: Int) -> TimeInterval {
        let capped = min(max(attempts, 1), 7)
        return min(300, 5 * pow(2, Double(capped - 1)))
    }

    public static func reduce(_ item: QueueItem, _ event: QueueEvent, now: Date = Date()) -> QueueItem {
        var next = item

        func fail(_ message: String, retryAfter: TimeInterval? = nil) {
            next.attempts += 1
            next.lastError = message
            next.notBefore = now.addingTimeInterval(retryAfter ?? retryDelay(attempts: next.attempts))
        }
        func clearURL() {
            next.uploadURL = nil
            next.uploadHeaders = [:]
            next.uploadExpiresAt = nil
            next.progress = 0
        }
        func finish(state: VideoState, emailAt: Date?) {
            if state.isGone {
                next.phase = .removed
            } else {
                next.phase = .done
                next.sentAt = next.sentAt ?? now
                next.progress = 1
            }
            next.serverState = state
            next.emailAt = emailAt ?? next.emailAt
            next.lastError = nil
            next.notBefore = nil
            clearURL()
            if next.phase == .done { next.progress = 1 }
        }

        switch (item.phase, event) {
        // Removed is final.
        case (.removed, _):
            return item

        // The tech's delete wins from any live state.
        case (.deleting, .deletedByTech):
            return item
        case (_, .deletedByTech):
            clearURL()
            next.phase = item.serverMayKnow ? .deleting : .removed
            next.notBefore = nil
            next.attempts = 0
        case (.deleting, .deleteConfirmed):
            next.phase = .removed
        case (.deleting, .callFailed(let message, let retryAfter)):
            fail(message, retryAfter: retryAfter)
        case (.deleting, .wake):
            next.notBefore = nil
        case (.deleting, _):
            return item

        case (.blocked, .serverState(let state, _)) where state.isGone:
            next.phase = .removed
        case (.blocked, _):
            return item

        case (_, .wake):
            next.notBefore = nil

        case (.queued, .registering), (.uploading, .registering):
            next.serverMayKnow = true

        case (.queued, .registered(let ticket)), (.uploading, .registered(let ticket)):
            next.phase = .uploading
            next.uploadURL = ticket.url
            next.uploadHeaders = ticket.headers
            next.uploadExpiresAt = ticket.expiresAt
            next.progress = 0
            next.serverMayKnow = true
            next.serverState = .awaitingUpload
            next.lastError = nil
            next.notBefore = nil

        case (.queued, .serverAlreadyHas(let state, let emailAt)),
             (.uploading, .serverAlreadyHas(let state, let emailAt)),
             (.uploaded, .serverAlreadyHas(let state, let emailAt)):
            next.serverMayKnow = true
            if state == .awaitingUpload {
                // No URL and no file on the server: ask again later.
                fail("The server gave no upload address.")
            } else {
                finish(state: state, emailAt: emailAt)
            }

        case (.queued, .stopLocked(let message)), (.uploading, .stopLocked(let message)):
            clearURL()
            next.phase = .blocked
            next.blockedMessage = message
            next.notBefore = nil

        case (.uploading, .progress(let fraction)):
            next.progress = max(item.progress, min(max(fraction, 0), 1))

        case (.uploading, .uploadFinished(let status)):
            if (200..<300).contains(status) {
                next.phase = .uploaded
                next.progress = 1
                next.lastError = nil
                next.notBefore = nil
            } else if status == 403 {
                // The signed URL ran out. Not a failure: just get a new one.
                clearURL()
                next.phase = .queued
            } else {
                clearURL()
                next.phase = .queued
                fail("The upload was refused (\(status)).")
            }

        case (.uploading, .uploadFailed(let message)):
            // Keep the URL; the engine starts a new task when it may retry.
            next.progress = 0
            fail(message)

        case (.uploaded, .completed(let state, let emailAt)):
            finish(state: state, emailAt: emailAt)

        case (.uploaded, .notUploaded):
            clearURL()
            next.phase = .queued
            fail("The server did not get the whole file.")

        case (.queued, .callFailed(let message, let retryAfter)),
             (.uploading, .callFailed(let message, let retryAfter)),
             (.uploaded, .callFailed(let message, let retryAfter)):
            fail(message, retryAfter: retryAfter)

        case (.queued, .fileLost), (.uploading, .fileLost):
            clearURL()
            next.phase = .blocked
            next.blockedMessage = "This video is no longer on the phone. Please record it again."

        case (.done, .serverState(let state, let emailAt)):
            next.serverState = state
            next.emailAt = emailAt ?? item.emailAt
            if state.isGone { next.phase = .removed }

        case (.queued, .serverState(let state, let emailAt)),
             (.uploading, .serverState(let state, let emailAt)),
             (.uploaded, .serverState(let state, let emailAt)):
            // The server may have confirmed a file whose answer never reached us.
            if state.isGone || state.serverHasFile {
                finish(state: state, emailAt: emailAt)
            }

        default:
            // An event that makes no sense in this phase is ignored, never fatal.
            return item
        }
        return next
    }
}

public extension Array where Element == QueueItem {
    /// How many videos still have to leave the phone.
    var waitingCount: Int { filter { $0.isWaiting }.count }

    /// The newest video that counts for a customer today.
    func liveItem(customerId: Int) -> QueueItem? {
        filter { $0.customerId == customerId && !$0.notOnMyList && $0.countsForStop }
            .max { $0.recordedAt < $1.recordedAt }
    }

    /// Customers that already have a video on this phone.
    var customersWithVideo: Set<Int> {
        Set(filter { $0.countsForStop && !$0.notOnMyList }.compactMap { $0.customerId })
    }
}
