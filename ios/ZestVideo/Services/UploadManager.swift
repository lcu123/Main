import Foundation
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

    /// Videos with a register/complete/delete call in the air.
    private var busy = Set<String>()
    /// Videos this launch has already given an upload task, and which task.
    private var started: [String: Int] = [:]
    /// Last progress pushed to the screen, to avoid a redraw per packet.
    private var shownProgress: [String: Double] = [:]
    private var backgroundCompletion: (() -> Void)?
    private var retryScheduled = false

    private lazy var session: URLSession = {
        let configuration = URLSessionConfiguration.background(withIdentifier: UploadManager.sessionIdentifier)
        // Start now, not when iOS finds it convenient.
        configuration.isDiscretionary = false
        // Wake the app when an upload finishes so the server can be told.
        configuration.sessionSendsLaunchEvents = true
        #if targetEnvironment(simulator)
        // A UI test starts the app with launch arguments. Started by iOS
        // itself for a finished upload, it would come up WITHOUT them: the
        // ordinary app, pointed at the real server, in the middle of a test.
        // So in UI-test mode iOS is not asked to start it; the test does.
        if UITestMode.isOn {
            configuration.sessionSendsLaunchEvents = false
        }
        #endif
        // Cellular is allowed, Low Data Mode included: do not wait for Wi-Fi.
        configuration.allowsCellularAccess = true
        configuration.allowsExpensiveNetworkAccess = true
        configuration.allowsConstrainedNetworkAccess = true
        configuration.waitsForConnectivity = true
        configuration.timeoutIntervalForResource = 24 * 60 * 60
        configuration.httpMaximumConnectionsPerHost = 2
        return URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
    }()

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
        var soonest: Date?

        for item in store.items {
            if busy.contains(item.id) { continue }
            if !item.mayRetry(now: now) {
                if let wait = item.notBefore { soonest = min(soonest ?? wait, wait) }
                continue
            }
            switch item.phase {
            case .queued:
                register(item)
            case .uploading:
                if live.contains(item.id) || started[item.id] != nil { continue }
                if item.needsFreshURL(now: now) {
                    register(item)
                } else {
                    startUpload(item)
                }
            case .uploaded:
                complete(item)
            case .deleting:
                tellServerDeleted(item)
            case .done, .blocked, .removed:
                break
            }
        }

        // Orphan tasks: uploads for videos that no longer exist.
        let known = Set(store.items.filter { $0.phase == .uploading }.map { $0.id })
        if !live.subtracting(known).isEmpty {
            cancelTasks { !known.contains($0) }
        }

        if let soonest { scheduleRetry(at: soonest) }
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

    // MARK: Steps

    private func register(_ item: QueueItem) {
        guard Files.exists(store.fileURL(for: item)) else {
            store.apply(.fileLost, to: item.id)
            onNotice?("A video was no longer on the phone. Please record it again.")
            onChange?()
            return
        }
        busy.insert(item.id)
        store.apply(.registering, to: item.id)
        let request = item.registerRequest
        let background = beginBackgroundWork()
        Task { @MainActor in
            defer {
                busy.remove(item.id)
                endBackgroundWork(background)
            }
            do {
                let answer = try await api.register(request)
                if let ticket = answer.upload {
                    if let updated = store.apply(.registered(ticket), to: item.id), updated.phase == .uploading {
                        startUpload(updated)
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
        guard let url = item.uploadURL else { return }
        let fileURL = store.fileURL(for: item)
        guard Files.exists(fileURL) else {
            store.apply(.fileLost, to: item.id)
            onChange?()
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
        task.resume()
    }

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
                if error.code == "not_uploaded" {
                    store.apply(.notUploaded, to: item.id)
                    pump()
                } else if error.status == 404 {
                    // The server has no record of it: start again from the top.
                    store.apply(.notUploaded, to: item.id)
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
                if error.code == "too_late" {
                    // It reached the customer before the delete reached the server.
                    store.apply(.deleteConfirmed, to: item.id)
                    onNotice?(error.message.isEmpty ? UploadManager.lockedMessage : error.message)
                    onChange?()
                } else if let status = error.status, (400..<500).contains(status), status != 429, status != 408 {
                    // The server never had it, or it is already gone.
                    store.apply(.deleteConfirmed, to: item.id)
                } else {
                    failed(item.id, error)
                }
            } catch {
                store.apply(.callFailed("Something went wrong.", retryAfter: nil), to: item.id)
            }
        }
    }

    static let lockedMessage = "This video already went to the customer. Call the office to change it."

    private func failed(_ id: String, _ error: APIError) {
        switch error {
        case .unauthorized:
            // Keep the queue: it sends after the next sign-in.
            onUnauthorized?()
        case .updateRequired:
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

    private func uploadEnded(id: String, taskIdentifier: Int, status: Int?, cancelled: Bool, errorText: String?) {
        // An old task reporting late (one that iOS cancelled when the app was
        // killed, say) must not be taken for the task that is running now.
        if let current = started[id], current != taskIdentifier { return }
        started[id] = nil
        shownProgress[id] = nil
        guard let item = store.item(id) else { return }
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
        let cancelled = (error as NSError?)?.code == NSURLErrorCancelled
        let errorText = error?.localizedDescription
        let taskIdentifier = task.taskIdentifier
        Task { @MainActor in
            self.uploadEnded(id: id, taskIdentifier: taskIdentifier, status: status, cancelled: cancelled, errorText: errorText)
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
