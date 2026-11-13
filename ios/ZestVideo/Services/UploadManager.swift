import Foundation
import os
import UIKit
import ZestCore

/// Gets every saved video to the server, however long it takes.
///
/// For each video: register for a signed URL → upload the file straight to
/// the bucket in a **background** URLSession task (it carries on with the app
/// closed and survives a restart) → ask the server to confirm → only then
/// delete the file. The queue on disk is the truth; this class only moves
/// rows through `QueueMachine`. Call everything on the main thread.
@MainActor
final class UploadManager: NSObject, URLSessionTaskDelegate {
    static let sessionIdentifier = "com.zestlawnpest.video.uploads"

    private let api: APIClient
    private let store: QueueStore

    /// The server said this phone is signed out.
    var onUnauthorized: (() -> Void)?
    /// The server said the app is too old.
    var onUpdateRequired: (() -> Void)?
    /// Something the tech should read on the Today screen.
    var onNotice: ((String) -> Void)?
    /// A video reached the server (or left the queue): refresh nudges and lists.
    var onChange: (() -> Void)?
    /// An upload ended badly. The detail says why (the HTTP status, or the
    /// system's error domain, code and words), for the audit trail.
    var onUploadProblem: ((_ videoID: String, _ detail: [String: String]) -> Void)?

    /// Videos with a register/complete/delete call in the air.
    private var busy = Set<String>()
    /// The ones of those that are register calls: each is about to become
    /// an upload, so it counts towards the two that may run at once.
    private var registering = Set<String>()
    /// Videos this launch has already given an upload task, and which task.
    private var started: [String: Int] = [:]
    /// Videos whose task this launch started but the system no longer lists,
    /// and since when. See `advance`.
    private var missingSince: [String: Date] = [:]
    private var checkScheduled = false
    private static let log = Logger(subsystem: "com.zestlawnpest.video", category: "uploads")
    /// Last progress pushed to the screen, to avoid a redraw per packet.
    private var shownProgress: [String: Double] = [:]
    private var backgroundCompletion: (() -> Void)?
    private var retryScheduled = false

    /// "background" on a phone. "plain" only in a UI test (see `UITestMode`).
    private var sessionKind: String {
        #if targetEnvironment(simulator)
        if UITestMode.isOn && !UITestMode.backgroundUploads { return "plain" }
        #endif
        return "background"
    }

    private lazy var session: URLSession = {
        #if targetEnvironment(simulator)
        if UITestMode.isOn && !UITestMode.backgroundUploads {
            // UI tests: an ordinary session, so the queue, the retries and
            // the recovery after a kill are tested whatever the simulator's
            // background transfer service does. Every other line of this
            // class is the same either way.
            let plain = URLSessionConfiguration.ephemeral
            plain.timeoutIntervalForRequest = 60
            plain.timeoutIntervalForResource = 10 * 60
            plain.httpMaximumConnectionsPerHost = 2
            return URLSession(configuration: plain, delegate: self, delegateQueue: nil)
        }
        #endif
        let configuration = UploadManager.backgroundConfiguration(identifier: UploadManager.sessionIdentifier)
        #if targetEnvironment(simulator)
        // A UI test starts the app with launch arguments. Started by iOS
        // itself for a finished upload, it would come up WITHOUT them: the
        // ordinary app, pointed at the real server, in the middle of a test.
        // So in UI-test mode iOS is not asked to start it; the test does.
        if UITestMode.isOn {
            configuration.sessionSendsLaunchEvents = false
        }
        #endif
        return URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
    }()

    /// How videos leave a real phone: a background session that iOS carries
    /// on with while the app is closed. Every line here is a promise from
    /// the brief (the videos go by themselves, on cellular, without waiting
    /// for Wi-Fi), and `UploadSessionTests` holds each one in place.
    static func backgroundConfiguration(identifier: String) -> URLSessionConfiguration {
        let configuration = URLSessionConfiguration.background(withIdentifier: identifier)
        // Start now, not when iOS finds it convenient.
        configuration.isDiscretionary = false
        // Wake the app when an upload finishes so the server can be told.
        configuration.sessionSendsLaunchEvents = true
        // Cellular is allowed, Low Data Mode included: do not wait for Wi-Fi.
        configuration.allowsCellularAccess = true
        configuration.allowsExpensiveNetworkAccess = true
        configuration.allowsConstrainedNetworkAccess = true
        // No signal is not a failure: wait for it, for up to a day.
        configuration.waitsForConnectivity = true
        configuration.timeoutIntervalForResource = 24 * 60 * 60
        configuration.httpMaximumConnectionsPerHost = 2
        return configuration
    }

    init(api: APIClient, store: QueueStore) {
        self.api = api
        self.store = store
        super.init()
    }

    // MARK: Driving the queue

    /// Looks at every video and does the next thing for it. Cheap and safe to
    /// call often: at launch, on every return to the app, after every change.
    /// - Parameter wake: the tech just opened the app, so skip any backoff.
    func pump(wake: Bool = false) {
        guard api.token != nil else { return }
        if wake {
            for item in store.items where item.notBefore != nil {
                store.apply(.wake, to: item.id)
            }
        }
        // Ask the system which uploads are still alive (some may have been
        // started by an earlier launch of the app).
        session.getAllTasks { [weak self] tasks in
            let live = Set(tasks.filter { $0.state == .running || $0.state == .suspended }.compactMap { $0.taskDescription })
            Task { @MainActor in
                self?.advance(live: live)
            }
        }
    }

    private func advance(live: Set<String>) {
        guard api.token != nil else { return }
        let now = Date()

        // Which videos have an upload task alive.
        var uploading = Set<String>()
        for item in store.items where item.phase == .uploading {
            if live.contains(item.id) {
                missingSince[item.id] = nil
                uploading.insert(item.id)
                continue
            }
            guard let taskIdentifier = started[item.id] else { continue }
            // This launch started a task for it, and the system no longer
            // lists that task. Normally its result is on the way. If nothing
            // has come a good while later, the task is lost: start again
            // rather than wait for ever.
            if let since = missingSince[item.id], now.timeIntervalSince(since) >= UploadManager.lostTaskSeconds {
                started[item.id] = nil
                missingSince[item.id] = nil
                shownProgress[item.id] = nil
                trace("upload task \(taskIdentifier) for \(item.id) vanished with no result; starting again")
                onUploadProblem?(item.id, ["reason": "task vanished with no result", "session": sessionKind])
                continue
            }
            if missingSince[item.id] == nil {
                missingSince[item.id] = now
            }
            scheduleCheck(after: UploadManager.lostTaskSeconds + 1)
            uploading.insert(item.id)
        }

        // What to do next is decided in ZestCore, where it is tested: oldest
        // video first, one register call at a time, two uploads at once.
        let plan = UploadPlanner.plan(items: store.items, uploading: uploading, registering: registering, busy: busy, now: now)
        for step in plan.steps {
            switch step {
            case .register(let id):
                if let item = store.item(id) { register(item) }
            case .startUpload(let id):
                if let item = store.item(id) { startUpload(item) }
            case .complete(let id):
                if let item = store.item(id) { complete(item) }
            case .tellDeleted(let id):
                if let item = store.item(id) { tellServerDeleted(item) }
            }
        }

        // Orphan tasks: uploads for videos that no longer exist.
        let known = Set(store.items.filter { $0.phase == .uploading }.map { $0.id })
        if !live.subtracting(known).isEmpty {
            cancelTasks { !known.contains($0) }
        }

        if let soonest = plan.lookAgainAt { scheduleRetry(at: soonest) }
    }

    /// One timer for the next retry. Only fires while the app is running;
    /// when it is not, the next open or background wake calls `pump` anyway.
    private func scheduleRetry(at date: Date) {
        guard !retryScheduled else { return }
        retryScheduled = true
        let delay = max(1, date.timeIntervalSinceNow + 0.5)
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            self?.retryScheduled = false
            self?.pump()
        }
    }

    /// How long a started task may be missing from the system's list, with
    /// no result delivered, before it is counted as lost.
    static let lostTaskSeconds: TimeInterval = 20

    /// Looks at the queue again in a while (only while the app is running).
    private func scheduleCheck(after seconds: TimeInterval) {
        guard !checkScheduled else { return }
        checkScheduled = true
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            self?.checkScheduled = false
            self?.pump()
        }
    }

    /// One line about an upload, for whoever has to find out why a video did
    /// not go: the phone's own log, and in a UI test the test server.
    private func trace(_ text: String) {
        UploadManager.log.info("\(text, privacy: .public)")
        #if targetEnvironment(simulator)
        UITestMode.note(text)
        #endif
    }

    // MARK: Steps

    private func register(_ item: QueueItem) {
        guard Files.exists(store.fileURL(for: item)) else {
            store.apply(.fileLost, to: item.id)
            onNotice?("A video was no longer on the phone. Please record it again.")
            onChange?()
            // The next video in line must not wait behind this one.
            pump()
            return
        }
        busy.insert(item.id)
        registering.insert(item.id)
        store.apply(.registering, to: item.id)
        let request = item.registerRequest
        let background = beginBackgroundWork()
        Task { @MainActor in
            defer {
                busy.remove(item.id)
                registering.remove(item.id)
                endBackgroundWork(background)
                // Whatever the answer, the next video in line can be asked for.
                pump()
            }
            do {
                let answer = try await api.register(request)
                if let ticket = answer.upload {
                    if let updated = store.apply(.registered(ticket), to: item.id), updated.phase == .uploading {
                        startUpload(updated)
                    } else {
                        // Deleted by the tech while the call was in the air.
                        trace("register answered for \(item.id), but it is no longer waiting to upload")
                    }
                } else {
                    store.apply(.serverAlreadyHas(state: answer.state, emailAt: answer.emailAt), to: item.id)
                    onChange?()
                }
            } catch let error as APIError {
                if error.code == "visit_has_video" {
                    // The earlier video already went to the customer: the stop is locked.
                    store.apply(.stopLocked(message: error.message), to: item.id)
                    onNotice?(error.message)
                    onChange?()
                } else {
                    failed(item.id, error)
                }
            } catch {
                store.apply(.callFailed("Something went wrong.", retryAfter: nil), to: item.id)
            }
        }
    }

    private func startUpload(_ item: QueueItem) {
        guard let url = item.uploadURL else {
            trace("no upload started for \(item.id): it has no upload address")
            return
        }
        let fileURL = store.fileURL(for: item)
        guard Files.exists(fileURL) else {
            trace("no upload started for \(item.id): its file is not on the phone")
            store.apply(.fileLost, to: item.id)
            onChange?()
            pump()
            return
        }
        guard started[item.id] == nil else { return }

        var request = URLRequest(url: url)
        request.httpMethod = "PUT"
        for (header, value) in item.uploadHeaders {
            request.setValue(value, forHTTPHeaderField: header)
        }
        // From the file on disk: the video is never loaded into memory.
        let task = session.uploadTask(with: request, fromFile: fileURL)
        task.taskDescription = item.id
        started[item.id] = task.taskIdentifier
        missingSince[item.id] = nil
        task.resume()
        // The address itself is a secret (it is signed), so only where it points.
        trace(
            "upload task \(task.taskIdentifier) made and started for \(item.id): PUT \(url.scheme ?? "?")://\(url.host ?? "?")\(url.path), "
                + "\(Files.size(of: fileURL)) bytes on disk, \(sessionKind) session"
        )
        #if targetEnvironment(simulator)
        if UITestMode.isOn {
            reportLater(id: item.id, taskIdentifier: task.taskIdentifier)
        }
        #endif
    }

    #if targetEnvironment(simulator)
    /// UI tests only: if a task has still not ended after a while, say what
    /// the system thinks it is doing, so a stalled upload explains itself.
    private func reportLater(id: String, taskIdentifier: Int) {
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 20 * 1_000_000_000)
            guard let self, self.started[id] == taskIdentifier else { return }
            self.session.getAllTasks { [weak self] tasks in
                let text: String
                if let task = tasks.first(where: { $0.taskIdentifier == taskIdentifier }) {
                    let failure = task.error as NSError?
                    text = "upload task \(taskIdentifier) for \(id) has not ended after 20 s: state \(task.state.rawValue) "
                        + "(0 running, 1 suspended, 2 cancelling, 3 completed), sent \(task.countOfBytesSent) of "
                        + "\(task.countOfBytesExpectedToSend) bytes, error \(failure?.domain ?? "none") \(failure?.code ?? 0)"
                } else {
                    text = "upload task \(taskIdentifier) for \(id) has not ended after 20 s, and the system no longer lists it"
                }
                Task { @MainActor in
                    self?.trace(text)
                }
            }
        }
    }
    #endif

    private func complete(_ item: QueueItem) {
        busy.insert(item.id)
        let background = beginBackgroundWork()
        Task { @MainActor in
            defer {
                busy.remove(item.id)
                endBackgroundWork(background)
            }
            do {
                let answer = try await api.complete(id: item.id, sizeBytes: item.sizeBytes)
                // Only now is the file deleted (QueueStore does it on this event).
                store.apply(.completed(state: answer.state, emailAt: answer.emailAt), to: item.id)
                onChange?()
            } catch let error as APIError {
                // "Not uploaded", or a video the server does not know (404,
                // whatever word comes with it): start again from the top.
                // The file stays on the phone until that has worked.
                if let event = ServerSays.afterFailedComplete(status: error.status, code: error.code ?? "") {
                    store.apply(event, to: item.id)
                    pump()
                } else {
                    failed(item.id, error)
                }
            } catch {
                store.apply(.callFailed("Something went wrong.", retryAfter: nil), to: item.id)
            }
        }
    }

    private func tellServerDeleted(_ item: QueueItem) {
        busy.insert(item.id)
        let background = beginBackgroundWork()
        Task { @MainActor in
            defer {
                busy.remove(item.id)
                endBackgroundWork(background)
            }
            do {
                _ = try await api.deleteVideo(id: item.id)
                store.apply(.deleteConfirmed, to: item.id)
            } catch let error as APIError {
                switch ServerSays.afterFailedDelete(status: error.status, code: error.code ?? "") {
                case .tooLate:
                    // It reached the customer before the delete reached the server.
                    store.apply(.deleteConfirmed, to: item.id)
                    onNotice?(error.message.isEmpty ? UploadManager.lockedMessage : error.message)
                    onChange?()
                case .finished:
                    // The server never had it, or it is already gone.
                    store.apply(.deleteConfirmed, to: item.id)
                case .tryAgain:
                    failed(item.id, error)
                }
            } catch {
                store.apply(.callFailed("Something went wrong.", retryAfter: nil), to: item.id)
            }
        }
    }

    static let lockedMessage = VideoWords.locked(toOffice: false)

    private func failed(_ id: String, _ error: APIError) {
        switch error {
        case .unauthorized:
            // Keep the queue: it sends after the next sign-in. The wait
            // stops the engine asking again at once; signing in clears it.
            store.apply(.callFailed(error.message, retryAfter: 300), to: id)
            onUnauthorized?()
        case .updateRequired:
            // The answer will be the same until the app is updated.
            store.apply(.callFailed(error.message, retryAfter: 300), to: id)
            onUpdateRequired?()
        default:
            if error.isTransient {
                store.apply(.callFailed(error.message, retryAfter: error.retryAfter), to: id)
            } else {
                // A 4xx that will not get better by itself. Back off hard but
                // never drop the video.
                store.apply(.callFailed(error.message, retryAfter: 300), to: id)
            }
        }
    }

    // MARK: The tech's delete / record again

    /// Removes a video from the phone at once: cancels its upload, deletes its
    /// file, and (if the server may know about it) tells the server.
    func drop(_ id: String) {
        cancelTasks { $0 == id }
        started[id] = nil
        missingSince[id] = nil
        shownProgress[id] = nil
        store.apply(.deletedByTech, to: id)
        onChange?()
        pump()
    }

    private func cancelTasks(where shouldCancel: @escaping @Sendable (String) -> Bool) {
        session.getAllTasks { tasks in
            for task in tasks {
                if let id = task.taskDescription, shouldCancel(id) {
                    task.cancel()
                }
            }
        }
    }

    // MARK: Background wake-ups

    /// From the app delegate: iOS relaunched the app because uploads finished.
    func attachBackgroundCompletion(_ handler: @escaping () -> Void) {
        backgroundCompletion = handler
        _ = session   // make sure the session exists so its events are delivered
        // Never hold the system's handler for long.
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 25 * 1_000_000_000)
            self?.finishBackgroundEvents()
        }
    }

    /// Re-attach to the background session at launch.
    func resume() {
        _ = session
    }

    #if targetEnvironment(simulator)
    /// UI-test reset only: cancel transfers left over from an earlier test.
    func cancelEverything() {
        started.removeAll()
        registering.removeAll()
        missingSince.removeAll()
        shownProgress.removeAll()
        cancelTasks { _ in true }
    }
    #endif

    private func finishBackgroundEvents() {
        let handler = backgroundCompletion
        backgroundCompletion = nil
        handler?()
    }

    private func beginBackgroundWork() -> UIBackgroundTaskIdentifier {
        UIApplication.shared.beginBackgroundTask(withName: "zest.upload.step", expirationHandler: nil)
    }

    private func endBackgroundWork(_ identifier: UIBackgroundTaskIdentifier) {
        if identifier != .invalid {
            UIApplication.shared.endBackgroundTask(identifier)
        }
        if busy.isEmpty {
            finishBackgroundEvents()
        }
    }

    // MARK: Results from the upload tasks (main thread)

    private func uploadProgress(id: String, fraction: Double) {
        // Redraw in 2% steps, not on every packet.
        if fraction - (shownProgress[id] ?? 0) < 0.02 && fraction < 1 { return }
        shownProgress[id] = fraction
        store.apply(.progress(fraction), to: id)
    }

    private func uploadEnded(
        id: String, taskIdentifier: Int, status: Int?, cancelled: Bool,
        errorText: String?, errorDomain: String?, errorCode: Int?
    ) {
        // An old task reporting late (one that iOS cancelled when the app was
        // killed, say) must not be taken for the task that is running now.
        if let current = started[id], current != taskIdentifier {
            trace("upload task \(taskIdentifier) for \(id) reported late and was ignored (task \(current) is the live one)")
            return
        }
        started[id] = nil
        missingSince[id] = nil
        shownProgress[id] = nil

        // Say how it ended, every time; and when it ended badly, keep why.
        let good = status.map { (200..<300).contains($0) } ?? false
        var detail: [String: String] = ["session": sessionKind]
        if let status { detail["status"] = String(status) }
        if let errorDomain { detail["domain"] = errorDomain }
        if let errorCode { detail["code"] = String(errorCode) }
        if let errorText { detail["text"] = String(errorText.prefix(200)) }
        let how = detail.sorted { $0.key < $1.key }.map { "\($0.key)=\($0.value)" }.joined(separator: " ")
        trace("upload task \(taskIdentifier) for \(id) ended: \(good ? "ok" : "NOT ok") \(how)")

        guard let item = store.item(id) else { return }
        // (A cancel that follows the tech's own delete is not a problem.)
        if !good && !(cancelled && item.phase != .uploading) {
            onUploadProblem?(id, detail)
        }
        if cancelled {
            // Not the tech's doing (his own delete removes or parks the row
            // first): iOS dropped the transfer. Start it again; never leave
            // the video sitting with no task.
            if item.phase == .uploading { pump() }
            return
        }

        if let status {
            store.apply(.uploadFinished(status: status), to: id)
        } else {
            store.apply(.uploadFailed(errorText ?? "The upload stopped."), to: id)
        }
        // On to `complete`, or round again for a fresh URL.
        pump()
    }

    // MARK: URLSessionTaskDelegate (delegate queue → main)

    nonisolated func urlSession(
        _ session: URLSession, task: URLSessionTask, didSendBodyData bytesSent: Int64,
        totalBytesSent: Int64, totalBytesExpectedToSend: Int64
    ) {
        guard let id = task.taskDescription, totalBytesExpectedToSend > 0 else { return }
        let fraction = Double(totalBytesSent) / Double(totalBytesExpectedToSend)
        Task { @MainActor in
            self.uploadProgress(id: id, fraction: fraction)
        }
    }

    nonisolated func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let id = task.taskDescription else { return }
        // An HTTP status only counts when the transfer itself did not fail.
        let status = error == nil ? (task.response as? HTTPURLResponse)?.statusCode : nil
        let failure = error as NSError?
        let cancelled = failure?.code == NSURLErrorCancelled
        let errorText = error?.localizedDescription
        let errorDomain = failure?.domain
        let errorCode = failure?.code
        let taskIdentifier = task.taskIdentifier
        Task { @MainActor in
            self.uploadEnded(
                id: id, taskIdentifier: taskIdentifier, status: status, cancelled: cancelled,
                errorText: errorText, errorDomain: errorDomain, errorCode: errorCode
            )
        }
    }

    /// The whole session stopped working (it should never, for this app).
    nonisolated func urlSession(_ session: URLSession, didBecomeInvalidWithError error: Error?) {
        let failure = error as NSError?
        let text = "the upload session became invalid: \(failure?.domain ?? "no error") \(failure?.code ?? 0) \(error?.localizedDescription ?? "")"
        Task { @MainActor in
            self.trace(text)
        }
    }

    nonisolated func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        Task { @MainActor in
            // Give the follow-up calls (complete) a moment, then release iOS.
            if self.busy.isEmpty {
                try? await Task.sleep(nanoseconds: 1_000_000_000)
            }
            if self.busy.isEmpty {
                self.finishBackgroundEvents()
            }
        }
    }
}
