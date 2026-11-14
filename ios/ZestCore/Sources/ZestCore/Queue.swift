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
    /// How many times in a row the upload address was refused (403).
    /// `nil` in rows written before this was counted.
    public var refusedInARow: Int?
    /// True while the last try failed for want of an answer (no signal, a
    /// timeout, a server error) rather than a "no" from the server. The
    /// videos recorded after this one would fail the same way, so they wait
    /// behind it and go in order when the signal is back.
    public var waitingForSignal: Bool?
    /// True for a video the tech recorded for a customer on his list that
    /// the server would not take for that customer ("That stop is not on
    /// the list for that day. Use Not On My List."). It was sent to the
    /// office as "not on my list" instead. `customerId` and `customerName`
    /// still say who he picked.
    public var leftTheList: Bool?
    /// The server's words when it would not take the video at all, even
    /// for the office. The video stays on the phone; the row says so.
    public var refusedMessage: String?
    /// True while the server says it is holding this video for the office
    /// to decide (`withOffice` in `/v1/today`): the visit left the day's
    /// route after it was filmed. No email has gone to anyone, so the row
    /// says SENT TO OFFICE, never SENT ✓.
    public var withOffice: Bool?

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
        case .waiting: return isStuck ? VideoWords.stuckRow : "WAITING"
        case .sending(let percent): return "SENDING \(percent)%"
        case .sent: return goesToOffice ? VideoWords.sentToOffice : VideoWords.sent
        case .blocked: return "NOT SENT"
        }
    }

    /// The video goes to the office, not to a customer: it was filmed as
    /// "not on my list" and waits there to be matched. No email is on its
    /// way, so nothing on screen may say one is.
    ///
    /// The server's word once it has given one (`unmatched`, and after the
    /// office matches it `held` or `sent` like any other); until then, what
    /// the phone knows itself.
    ///
    /// Also true while the server is holding a customer's video for the
    /// office to decide (`withOffice`): it has not gone to the customer.
    public var goesToOffice: Bool {
        if withOffice == true { return true }
        if let serverState, serverState != .awaitingUpload, serverState != .unknown {
            return serverState == .unmatched
        }
        return notOnMyList
    }

    /// Still on the phone, and the server has said it will not take it.
    /// Waiting will not send it: someone at the office has to look.
    public var isStuck: Bool { isWaiting && refusedMessage != nil }
}

/// The words for where a video has gone. A video on the list goes to its
/// customer; one filmed as "not on my list" goes to the office to be matched.
public enum VideoWords {
    public static let sent = "SENT ✓"
    public static let sentToOffice = "SENT TO OFFICE"

    /// What follows "You can change it for 7 more minutes."
    public static func afterTheWindow(toOffice: Bool) -> String {
        toOffice ? "Then the office matches it to the customer." : "Then it goes to the customer."
    }

    /// Once the time to change it has passed.
    /// - Parameter deciding: the server is holding a customer's video for
    ///   the office to decide (`withOffice`). The customer is known, so
    ///   nothing is said about matching; the words are the server's own.
    public static func locked(toOffice: Bool, deciding: Bool = false) -> String {
        if deciding { return officeDeciding }
        return toOffice
            ? "This video went to the office. They will match it to the customer. Call the office to change it."
            : "This video already went to the customer. Call the office to change it."
    }

    /// A video the server is holding for the office to decide.
    public static let officeDeciding = "The office is looking at this video. Call the office to change it."

    // MARK: The office changed the route

    /// On the confirm screen, when the stop the tech has open is no longer
    /// on the list the server sends. True whatever the reason (cancelled,
    /// moved to another day, given to another tech), and he can still
    /// record: the work may be done already.
    public static let stopLeftList = "This stop is no longer on your list. You can still record it."

    /// On My Route, once a video for such a stop has gone to the office
    /// instead of the customer.
    public static func wentToOfficeInstead(name: String) -> String {
        let who = name.trimmingCharacters(in: .whitespacesAndNewlines)
        return "\(who.isEmpty ? "That stop" : who) is no longer on your list. The video goes to the office."
    }

    /// A video the server will not take at all. It stays on the phone.
    public static let stuckRow = "NOT SENT · CALL OFFICE"
    public static let stuck = "This video could not be sent. It is safe on this phone. Please call the office."
}

/// What the server's "no" means for a video, worked out from the status
/// and the error word alone, so it is the same whatever message comes with
/// it. (The server's word for a video it does not know is `bad_request`
/// with a 404; nothing here reads that word.)
public enum ServerSays {
    /// A failed `complete`. `nil` when it is not for this function to say
    /// (signed out, update needed, no answer: the caller's usual handling).
    public static func afterFailedComplete(status: Int?, code: String) -> QueueEvent? {
        // The whole file is not there: upload it again.
        if code == "not_uploaded" { return .notUploaded }
        // The server has no record of this video at all. Start again from
        // the top: register it afresh and send the file again. The video
        // stays on the phone until that has worked.
        if status == 404 { return .notUploaded }
        return nil
    }

    public enum Register: Equatable, Sendable {
        /// `409 visit_has_video`: this tech's own earlier video for the
        /// stop has already gone to the customer.
        case stopLocked
        /// `400`: a plain "no" to this video as it stands. Asking again
        /// with the same words gets the same answer.
        case refused
        /// Not for this function to say (signed out, update needed, no
        /// answer, a server error): the caller's usual handling.
        case other
    }

    /// A failed register.
    ///
    /// The one `400` a healthy phone can meet is "That stop is not on the
    /// list for that day. Use Not On My List." The server takes a video for
    /// a stop the office cancelled or handed on after the phone's list was
    /// fetched, so this is rare: a customer who was never a stop that day,
    /// or a server from before that rule. See `QueueEvent.refused` for what
    /// happens next.
    public static func afterFailedRegister(status: Int?, code: String) -> Register {
        if code == "visit_has_video" { return .stopLocked }
        if status == 400 { return .refused }
        return .other
    }

    public enum Delete: Equatable, Sendable {
        /// The video is gone from the server, or was never there: done.
        case finished
        /// It reached the customer (or the office) before the delete did.
        case tooLate
        /// No answer worth acting on: try again later.
        case tryAgain
    }

    /// The answer to telling the server a video was deleted.
    public static func afterFailedDelete(status: Int?, code: String) -> Delete {
        if code == "too_late" { return .tooLate }
        // Signed out or an old build: the delete still has to reach the
        // server once that is put right.
        if status == 401 || status == 426 { return .tryAgain }
        // Any other plain "no" (404 above all: the server never had it, or
        // it is already gone) means there is nothing left to delete.
        if let status, (400..<500).contains(status), status != 429, status != 408 { return .finished }
        return .tryAgain
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
    /// Register answered `400`: the server will not take this video as it
    /// stands.
    ///
    /// - A video recorded for a customer is first sent exactly as it was
    ///   filmed, and normally taken, even when the stop has since left the
    ///   list. If the server still says no, it has no such stop for that
    ///   day. Its own words are "Use Not On My List", and that is what the
    ///   phone does, by itself: the same clip under the same ID is sent to
    ///   the office to be matched. Nothing is lost, and the tech is not
    ///   sent back to a house he has left to film it again.
    /// - For a video that is already going to the office there is no other
    ///   way to send it. It stays on the phone, the row says to call the
    ///   office, and it is tried again now and then (`refusedRetrySeconds`)
    ///   and each time the app is opened.
    case refused(message: String)
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
    /// Fresh word from `/v1/today` about this video. `withOffice`: the
    /// server is holding it for the office to decide.
    case serverState(VideoState, emailAt: Date?, withOffice: Bool = false)
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

    /// How long a video the server refused outright waits before it is
    /// offered again: an hour. (Opening the app tries at once.) It is never
    /// dropped, and it never asks every few seconds.
    public static let refusedRetrySeconds: TimeInterval = 60 * 60

    public static func reduce(_ item: QueueItem, _ event: QueueEvent, now: Date = Date()) -> QueueItem {
        var next = item

        func fail(_ message: String, retryAfter: TimeInterval? = nil, forSignal: Bool = false) {
            next.attempts += 1
            next.lastError = message
            next.notBefore = now.addingTimeInterval(retryAfter ?? retryDelay(attempts: next.attempts))
            next.waitingForSignal = forSignal ? true : nil
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
            next.waitingForSignal = nil
            next.refusedMessage = nil
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

        case (.blocked, .serverState(let state, _, _)) where state.isGone:
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
            next.waitingForSignal = nil
            next.refusedMessage = nil

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

        case (.queued, .refused(let message)), (.uploading, .refused(let message)):
            clearURL()
            next.phase = .queued
            if item.notOnMyList {
                // Not even for the office. Keep it, say so, ask again rarely.
                next.refusedMessage = message
                fail(message, retryAfter: refusedRetrySeconds)
            } else {
                // The server has no such stop: to the office instead, at once.
                next.notOnMyList = true
                next.leftTheList = true
                next.lastError = message
                next.notBefore = nil
                next.waitingForSignal = nil
            }

        case (.uploading, .progress(let fraction)):
            next.progress = max(item.progress, min(max(fraction, 0), 1))

        case (.uploading, .uploadFinished(let status)):
            if (200..<300).contains(status) {
                next.phase = .uploaded
                next.progress = 1
                next.lastError = nil
                next.notBefore = nil
                next.refusedInARow = nil
            } else if status == 403 {
                // The signed URL ran out. Not a failure: just get a new one.
                clearURL()
                next.phase = .queued
                // But a fresh address refused again and again is not "ran
                // out": something is wrong at the other end. From the third
                // time, wait between tries like any other failure, so the
                // phone never spins asking and uploading without a pause.
                let refusals = (item.refusedInARow ?? 0) + 1
                next.refusedInARow = refusals
                if refusals >= 3 {
                    fail("The upload was refused (403).")
                }
            } else {
                clearURL()
                next.phase = .queued
                fail("The upload was refused (\(status)).")
            }

        case (.uploading, .uploadFailed(let message)):
            // Keep the URL; the engine starts a new task when it may retry.
            next.progress = 0
            fail(message, forSignal: true)

        case (.uploaded, .completed(let state, let emailAt)):
            finish(state: state, emailAt: emailAt)

        case (.uploaded, .notUploaded):
            clearURL()
            next.phase = .queued
            fail("The server did not get the whole file.")

        case (.queued, .callFailed(let message, let retryAfter)),
             (.uploading, .callFailed(let message, let retryAfter)),
             (.uploaded, .callFailed(let message, let retryAfter)):
            // No wait given: there was no answer at all (no signal, a
            // timeout, a server error). A wait given: the server said no.
            fail(message, retryAfter: retryAfter, forSignal: retryAfter == nil)

        case (.queued, .fileLost), (.uploading, .fileLost):
            clearURL()
            next.phase = .blocked
            next.blockedMessage = "This video is no longer on the phone. Please record it again."

        case (.done, .serverState(let state, let emailAt, let withOffice)):
            next.serverState = state
            next.emailAt = emailAt ?? item.emailAt
            // Each answer says afresh whether the office is holding it: the
            // office may have sent it, or the stop may be back on the route.
            next.withOffice = withOffice ? true : nil
            if state.isGone { next.phase = .removed }

        case (.queued, .serverState(let state, let emailAt, let withOffice)),
             (.uploading, .serverState(let state, let emailAt, let withOffice)),
             (.uploaded, .serverState(let state, let emailAt, let withOffice)):
            // The server may have confirmed a file whose answer never reached us.
            if state.isGone || state.serverHasFile {
                finish(state: state, emailAt: emailAt)
                next.withOffice = withOffice ? true : nil
            }

        default:
            // An event that makes no sense in this phase is ignored, never fatal.
            return item
        }
        return next
    }
}

/// Decides what the upload engine does next. Pure, so a whole day of
/// videos can be played through in a test.
///
/// The rules, in order:
/// - **Oldest first.** After a day with no signal the first stop's video
///   goes first: its customer has waited longest.
/// - **One register call at a time, and at most two uploads.** An upload
///   address is only asked for when the video can start at once, so twelve
///   waiting videos do not hold twelve addresses that run out while they
///   queue behind each other on a weak signal.
/// - **No signal holds the line.** While the oldest waiting video cannot
///   reach the server, the ones recorded after it are not tried: they would
///   fail the same way, and trying them out of turn would send them out of
///   order when the signal comes back. Only that one video is retried.
/// - **A "no" from the server does not.** A video the server refused waits
///   its turn alone and never holds up the ones behind it.
public enum UploadPlanner {
    public enum Step: Equatable, Sendable {
        /// Ask the server for an upload address.
        case register(String)
        /// Start the upload task (the video has a usable address).
        case startUpload(String)
        /// The file is up: ask the server to confirm it.
        case complete(String)
        /// Tell the server the tech deleted it.
        case tellDeleted(String)
    }

    public struct Plan: Equatable, Sendable {
        public var steps: [Step]
        /// When the next video waiting out a failure may be tried again.
        public var lookAgainAt: Date?

        public init(steps: [Step] = [], lookAgainAt: Date? = nil) {
            self.steps = steps
            self.lookAgainAt = lookAgainAt
        }
    }

    /// Uploads running at once. The session allows two connections; more
    /// tasks than that would only sit and wait.
    public static let maxUploads = 2

    /// - Parameters:
    ///   - items: the queue as it is on disk.
    ///   - uploading: videos whose upload task is alive right now.
    ///   - registering: videos with a register call in the air.
    ///   - busy: videos with any call in the air (register, complete, delete).
    public static func plan(
        items: [QueueItem], uploading: Set<String>, registering: Set<String>, busy: Set<String>, now: Date
    ) -> Plan {
        var plan = Plan()
        var slots = max(0, maxUploads - uploading.count - registering.count)
        var mayRegister = registering.isEmpty

        // Oldest first; videos recorded in the same second keep the queue's order.
        let ordered = items.enumerated()
            .sorted { $0.element.recordedAt != $1.element.recordedAt ? $0.element.recordedAt < $1.element.recordedAt : $0.offset < $1.offset }
            .map { $0.element }

        for item in ordered {
            if busy.contains(item.id) || registering.contains(item.id) { continue }
            if !item.mayRetry(now: now) {
                if let wait = item.notBefore {
                    plan.lookAgainAt = min(plan.lookAgainAt ?? wait, wait)
                }
                // An older video with no signal: nothing younger is asked for.
                if item.waitingForSignal == true, item.phase == .queued || item.phase == .uploading {
                    mayRegister = false
                }
                continue
            }
            switch item.phase {
            case .queued:
                if mayRegister && slots > 0 {
                    plan.steps.append(.register(item.id))
                    mayRegister = false
                    slots -= 1
                }
            case .uploading:
                if uploading.contains(item.id) { continue }
                if item.needsFreshURL(now: now) {
                    if mayRegister && slots > 0 {
                        plan.steps.append(.register(item.id))
                        mayRegister = false
                        slots -= 1
                    }
                } else if slots > 0 {
                    plan.steps.append(.startUpload(item.id))
                    slots -= 1
                }
            case .uploaded:
                plan.steps.append(.complete(item.id))
            case .deleting:
                plan.steps.append(.tellDeleted(item.id))
            case .done, .blocked, .removed:
                break
            }
        }
        return plan
    }
}

public extension Array where Element == QueueItem {
    /// How many videos still have to leave the phone.
    var waitingCount: Int { filter { $0.isWaiting }.count }

    /// Videos filmed for `day`'s route. A video from an earlier day that is
    /// still on the phone (saved late, or waiting for signal overnight)
    /// belongs to that day's visit: it must not make today's stop for the
    /// same customer look done, and a new video today must not replace it.
    func forDay(_ day: String) -> [QueueItem] {
        filter { item in
            guard let age = ZestDates.daysBetween(item.stopDate, day) else { return true }
            return age <= 0
        }
    }

    /// The newest video that counts for a customer on `day`.
    func liveItem(customerId: Int, day: String) -> QueueItem? {
        forDay(day)
            .filter { $0.customerId == customerId && !$0.notOnMyList && $0.countsForStop }
            .max { $0.recordedAt < $1.recordedAt }
    }

    /// Customers that already have a video on this phone for `day`.
    func customersWithVideo(day: String) -> Set<Int> {
        Set(forDay(day).filter { $0.countsForStop && !$0.notOnMyList }.compactMap { $0.customerId })
    }
}
