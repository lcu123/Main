import Foundation
import SwiftUI
import UIKit
import ZestCore

/// Which video a screen is about.
enum VideoFocus: Equatable {
    case stop(Int)
    case item(String)
}

/// Everything the "this stop has a video" screen needs, worked out in one place.
struct VideoSummary: Equatable {
    enum Mode: Equatable {
        /// Inside the undo window: Record again and Delete are offered.
        case changeable(closesAt: Date)
        /// Another tech's video that has not gone out yet: Record again only.
        case otherTech
        /// Past the window but still on the phone: it sends by itself.
        case waiting
        /// Gone to the customer. Only the office can change it.
        case locked
        /// The server refused it; shows the server's words.
        case blocked(String)
        /// Nothing to show any more.
        case gone
    }

    var name: String
    var address: String
    var statusText: String
    var mode: Mode
    var stop: Stop?
    var itemID: String?
    var serverVideoID: String?
}

/// The two-keys check for the clip on the review screen.
enum CheckState: Equatable {
    case idle
    case running
    case done(CheckResult)
}

struct CheckResult: Equatable {
    var signals: Signals
    var clean: Bool
    var reasons: [String]
    /// A different stop the spoken words fit better.
    var better: Stop?
    var transcript: String?
}

/// Starting values for the screens' own state.
///
/// In a real build these are always the defaults below: nothing can change
/// them. Only the simulator-only screenshot mode (`ScreenshotMode.swift`)
/// sets them, to open a screen part-way through for a picture.
struct ScreenSeeds: Equatable {
    var loginStep: LoginView.Step = .contact
    var loginContact = ""
    var loginCode = ""
    var confirmStage: ConfirmStopView.Stage = .confirm
    var reviewStage: ReviewView.Stage = .ask
    var recommend = false
    /// Today opens scrolled to the end of the list.
    var scrollToEnd = false
    /// The recording ring and prompts hold still at this many seconds in.
    var frozenElapsed: TimeInterval?
    /// Draw a stand-in picture where the camera or the clip would be.
    var standInPicture = false
}

/// One line under a stop on the Today list.
struct RowStatus: Equatable {
    enum Kind: Equatable {
        case none
        case waiting
        case sent
        case problem
    }

    var text: String
    var kind: Kind
}

/// The app's state and its one router. Screens are plain values of `route`:
/// no navigation stack, so there is nothing to swipe back through by mistake.
@MainActor
final class AppModel: ObservableObject {
    static let shared = AppModel()

    enum Route: Equatable {
        case login
        case today
        case confirm
        case record
        case review
        case pickStop
        case stopVideo
        case queue
        case remindersOffer
        case updateRequired
    }

    enum RecordPhase: Equatable {
        case idle
        case arming
        case recording
        case saving
    }

    @Published var route: Route = .login
    @Published private(set) var tech: TechInfo?
    @Published private(set) var today: TodayResponse?
    @Published private(set) var target: RecordTarget?
    @Published private(set) var draft: Draft?
    @Published private(set) var check: CheckState = .idle
    @Published private(set) var recordPhase: RecordPhase = .idle
    @Published private(set) var focus: VideoFocus?
    @Published private(set) var offline = false
    @Published private(set) var refreshing = false
    /// One line for the top of Today, e.g. "Video deleted."
    @Published var notice: String?
    /// Shown on the record screen when a clip did not save.
    @Published var recordProblem: String?
    /// Always the defaults outside screenshot mode. See `ScreenSeeds`.
    private(set) var seeds = ScreenSeeds()

    let api: APIClient
    let queue: QueueStore
    let camera = CameraService()
    let location = LocationService()
    let events = EventLog()
    let uploads: UploadManager

    private let todayStore = TodayStore()
    private let draftStore = DraftStore()
    private let defaults = UserDefaults.standard
    private var started = false
    private var checkTask: Task<Void, Never>?

    private enum Key {
        static let tech = "tech"
        static let askedNotifications = "askedNotifications"
        static let offeredReminders = "offeredReminders"
        static let pushToken = "pushToken"
    }

    private init() {
        let api = APIClient()
        let queue = QueueStore()
        self.api = api
        self.queue = queue
        self.uploads = UploadManager(api: api, store: queue)
    }

    // MARK: - Derived

    var config: AppConfig { today?.config ?? .fallback }

    var signedIn: Bool { api.token != nil }

    /// Today's stops, with one correction: a video of mine the server is still
    /// waiting for, that this phone no longer has, does not count.
    var stops: [Stop] {
        guard let today else { return [] }
        return today.stops.map { stop in
            guard let video = stop.video, video.byYou, video.state == .awaitingUpload,
                  queue.item(video.id) == nil else { return stop }
            var copy = stop
            copy.video = nil
            return copy
        }
    }

    var ranked: StopRanker.Result {
        var haveVideo = queue.items.customersWithVideo
        if let draft, let customerId = draft.target.customerId, !draft.target.notOnMyList {
            haveVideo.insert(customerId)
        }
        return StopRanker.rank(stops: stops, location: location.freshFix(maxAge: 300)?.coordinate, haveVideo: haveVideo)
    }

    var waitingCount: Int { queue.items.waitingCount }

    func stop(customerId: Int?) -> Stop? {
        guard let customerId else { return nil }
        return stops.first { $0.customerId == customerId }
    }

    func rowStatus(for stop: Stop) -> RowStatus {
        if let draft, !draft.target.notOnMyList, draft.target.customerId == stop.customerId {
            return RowStatus(text: "NOT SAVED YET", kind: .waiting)
        }
        if let item = queue.items.liveItem(customerId: stop.customerId) {
            return RowStatus(text: item.displayText, kind: item.phase == .done ? .sent : .waiting)
        }
        if blockedItem(customerId: stop.customerId) != nil {
            return RowStatus(text: "NOT SENT", kind: .problem)
        }
        if let video = stop.liveVideo {
            return video.state == .awaitingUpload
                ? RowStatus(text: "WAITING", kind: .waiting)
                : RowStatus(text: "SENT ✓", kind: .sent)
        }
        return RowStatus(text: "NO VIDEO YET", kind: .none)
    }

    private func blockedItem(customerId: Int) -> QueueItem? {
        queue.items.first { $0.customerId == customerId && !$0.notOnMyList && $0.phase == .blocked }
    }

    private func hasVideo(_ stop: Stop) -> Bool {
        queue.items.liveItem(customerId: stop.customerId) != nil
            || blockedItem(customerId: stop.customerId) != nil
            || stop.liveVideo != nil
    }

    // MARK: - Launch and lifecycle

    /// Called once from the app delegate, also when iOS launches the app in
    /// the background for a finished upload or a left stop.
    func start() {
        guard !started else { return }
        started = true

        api.token = Keychain.get(Keychain.deviceTokenKey)
        if let data = defaults.data(forKey: Key.tech) {
            tech = try? ZestJSONBridge.decoder.decode(TechInfo.self, from: data)
        }
        today = todayStore.load()
        queue.pruneOldDays(today: ZestDates.dayString(Date()))
        loadDraft()

        uploads.onUnauthorized = { [weak self] in self?.signedOutByServer() }
        uploads.onUpdateRequired = { [weak self] in self?.route = .updateRequired }
        uploads.onNotice = { [weak self] text in self?.notice = text }
        uploads.onChange = { [weak self] in self?.syncNudges() }
        location.onLeave = { [weak self] customerId in self?.leftStop(customerId: customerId) }

        route = signedIn ? .today : .login
        uploads.resume()
        Task { await recoverInterruptedRecording() }
    }

    /// The app came to the front.
    func becameActive() {
        guard started else { return }
        // A new day: yesterday's route and finished rows go.
        if let today, !ZestDates.isToday(today.date) {
            self.today = nil
            todayStore.wipe()
            queue.pruneOldDays(today: ZestDates.dayString(Date()))
            discardStaleDraft()
        }
        guard signedIn else { return }
        location.askWhenInUse()
        location.refresh(precise: false)
        uploads.pump(wake: true)
        events.flush(using: api)
        syncNudges()
        Task {
            await refreshToday()
            await Notifier.registerForPushIfAllowed()
        }
    }

    func wentToBackground() {
        guard started else { return }
        syncNudges()
    }

    /// A notification was tapped, or a push arrived: get things moving.
    func nudged() {
        guard started, signedIn else { return }
        uploads.pump(wake: true)
    }

    private func syncNudges() {
        let waiting = waitingCount
        if waiting > 0 {
            Notifier.scheduleWaitingNudge(count: waiting)
        } else {
            Notifier.cancelWaitingNudge()
        }
        queue.removeOrphanFiles(keeping: protectedFileNames())
    }

    private func protectedFileNames() -> Set<String> {
        var names = Set<String>()
        if let draft { names.insert(draft.fileName) }
        if let pending = draftStore.loadPending() { names.insert(pending.movieFileName) }
        return names
    }

    // MARK: - Today

    func refreshToday() async {
        guard signedIn, !refreshing else { return }
        refreshing = true
        defer { refreshing = false }
        do {
            let fresh = try await api.today()
            today = fresh
            tech = fresh.tech
            if let data = try? ZestJSONBridge.encoder.encode(fresh.tech) {
                defaults.set(data, forKey: Key.tech)
            }
            todayStore.save(fresh)
            offline = false
            // The server's word on each of my videos.
            for video in fresh.videos where queue.item(video.id) != nil {
                queue.apply(.serverState(video.state, emailAt: video.emailAt), to: video.id)
            }
            if fresh.config.minBuild > APIClient.build {
                route = .updateRequired
            }
            updateLeaveRegions()
            syncNudges()
        } catch let error as APIError {
            handle(error)
        } catch {
            offline = true
        }
    }

    private func handle(_ error: APIError) {
        switch error {
        case .unauthorized:
            signedOutByServer()
        case .updateRequired:
            route = .updateRequired
        case .offline, .badResponse:
            offline = true
        case .http:
            break
        }
    }

    // MARK: - Sign in and out

    /// Returns a plain-words problem, or `nil` when the code is on its way.
    func requestCode(contact: String) async -> String? {
        do {
            try await api.requestCode(contact: contact)
            return nil
        } catch let error as APIError {
            if case .updateRequired = error { route = .updateRequired }
            return error.message
        } catch {
            return "No signal. Please try again."
        }
    }

    /// Returns a plain-words problem, or `nil` when signed in.
    func verify(contact: String, code: String) async -> String? {
        let request = VerifyRequest(
            contact: contact, code: code, deviceName: UIDevice.current.name, deviceModel: AppModel.deviceModel,
            osVersion: UIDevice.current.systemVersion, appBuild: APIClient.build
        )
        do {
            let answer = try await api.verify(request)
            Keychain.set(answer.token, for: Keychain.deviceTokenKey)
            api.token = answer.token
            tech = answer.tech
            if let data = try? ZestJSONBridge.encoder.encode(answer.tech) {
                defaults.set(data, forKey: Key.tech)
            }
            route = .today
            becameActive()
            if let pushToken = defaults.string(forKey: Key.pushToken) {
                sendPushToken(pushToken)
            }
            return nil
        } catch let error as APIError {
            if case .updateRequired = error { route = .updateRequired }
            if let left = error.httpBody?.triesLeft, error.code == "bad_code" {
                let tries = left == 1 ? "1 try left." : "\(left) tries left."
                return "That code is not right. \(tries)"
            }
            return error.message
        } catch {
            return "No signal. Please try again."
        }
    }

    /// The tech chose to sign out. Videos still waiting stay on the phone and
    /// send after the next sign-in.
    func signOut() {
        let api = self.api
        Task { try? await api.logout() }
        // Give the logout call its token, then forget it.
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 300_000_000)
            self.clearSession()
        }
    }

    /// A 401 from anywhere: back to the code screen. The queue is kept.
    private func signedOutByServer() {
        guard signedIn else { return }
        clearSession()
    }

    private func clearSession() {
        Keychain.remove(Keychain.deviceTokenKey)
        api.token = nil
        tech = nil
        today = nil
        todayStore.wipe()
        defaults.removeObject(forKey: Key.tech)
        location.clearLeaveRegions()
        camera.shutDown()
        route = .login
    }

    private static var deviceModel: String {
        var info = utsname()
        uname(&info)
        return withUnsafePointer(to: &info.machine) { pointer in
            pointer.withMemoryRebound(to: CChar.self, capacity: 1) { String(cString: $0) }
        }
    }

    // MARK: - Picking a stop

    func open(stop: Stop) {
        notice = nil
        // One thing at a time: an unsaved clip is dealt with first.
        if draft != nil {
            openReview()
            return
        }
        if hasVideo(stop) {
            focus = .stop(stop.customerId)
            route = .stopVideo
        } else {
            beginConfirm(RecordTarget(stop: stop))
        }
    }

    func openNotOnMyList() {
        notice = nil
        if draft != nil {
            openReview()
            return
        }
        beginConfirm(.notOnMyList)
    }

    func open(item: QueueItem) {
        if let customerId = item.customerId, !item.notOnMyList {
            focus = .stop(customerId)
        } else {
            focus = .item(item.id)
        }
        route = .stopVideo
    }

    private func beginConfirm(_ target: RecordTarget) {
        self.target = target
        recordProblem = nil
        route = .confirm
        // Warm the camera now so the record screen opens instantly.
        camera.prepare()
        location.refresh(precise: true)
    }

    /// "This is it": on to the camera.
    func confirmTarget() {
        guard target != nil else { return }
        route = .record
        camera.prepare()
    }

    func logFarWarning() {
        events.log(.farWarning, customerId: target?.customerId)
    }

    func logStorage(blocked: Bool) {
        events.log(blocked ? .storageBlock : .storageWarn)
    }

    /// Back to the list from confirm, record, queue or a video screen.
    func backToToday() {
        if recordPhase == .idle {
            camera.shutDown()
        }
        target = nil
        focus = nil
        route = signedIn ? .today : .login
    }

    func showQueue() {
        notice = nil
        route = .queue
    }

    // MARK: - Recording

    /// One tap (or a volume button): start the clip.
    func startRecording() {
        guard recordPhase == .idle, camera.status == .ready, let target else { return }
        recordPhase = .arming
        recordProblem = nil

        // A new recording replaces any unsaved one.
        discardDraft(log: true)

        let pending = PendingRecording(
            id: QueueItem.newID(), target: target, stopDate: today?.date ?? ZestDates.dayString(Date()),
            location: location.freshFix(), startedAt: Date()
        )
        draftStore.savePending(pending)
        events.log(.recordStart, videoId: pending.id, customerId: target.customerId)

        // Buzz and beep first, so the beep is not on the customer's video.
        Sounds.allowDuringRecording()
        Haptics.thump()
        Sounds.beginRecording()
        let maxSeconds = config.maxSeconds
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 350_000_000)
            self.recordPhase = .recording
            self.camera.startRecording(to: Files.video(pending.movieFileName), maxSeconds: maxSeconds) { [weak self] clip in
                Task { @MainActor in
                    await self?.finishRecording(pending: pending, clip: clip)
                }
            }
        }
    }

    /// Tapping the ring ends the clip early, but not in the first three seconds.
    func stopRecordingEarly() {
        guard recordPhase == .recording, let startedAt = camera.recordingStartedAt,
              Date().timeIntervalSince(startedAt) >= Countdown.earliestStopSeconds else { return }
        camera.stopRecording()
    }

    private func finishRecording(pending: PendingRecording, clip: RecordedClip?) async {
        recordPhase = .saving
        Haptics.thump()
        Sounds.endRecording()
        events.log(.recordStop, videoId: pending.id, customerId: pending.target.customerId)

        let saved = await makeDraft(from: pending, startedAt: clip?.startedAt)
        recordPhase = .idle
        if saved {
            // The camera's job is done: free the microphone and the battery.
            camera.shutDown()
            openReview()
        } else {
            recordProblem = "That video did not save. Please record it again."
        }
    }

    /// Re-wraps the camera file as the upload-ready .mp4 and stores it as a draft.
    private func makeDraft(from pending: PendingRecording, startedAt: Date?) async -> Bool {
        let movie = Files.video(pending.movieFileName)
        let fileName = "\(pending.id).mp4"
        let output = await VideoExporter.makeMP4(from: movie, to: Files.video(fileName))
        Files.remove(movie)
        draftStore.clearPending()
        guard let output else { return false }

        let draft = Draft(
            id: pending.id, target: pending.target, fileName: fileName, stopDate: pending.stopDate,
            recordedAt: startedAt ?? pending.startedAt, durationSec: output.seconds, sizeBytes: output.bytes,
            location: pending.location, createdAt: Date()
        )
        // On disk the instant recording stops: it survives anything from here.
        draftStore.saveDraft(draft)
        self.draft = draft
        Notifier.scheduleDraftNudge(name: draft.target.name)
        return true
    }

    /// The app was killed, or a call came in, in the middle of a clip.
    private func recoverInterruptedRecording() async {
        guard let pending = draftStore.loadPending(), recordPhase == .idle else { return }
        if draft == nil, Files.size(of: Files.video(pending.movieFileName)) > 0 {
            _ = await makeDraft(from: pending, startedAt: nil)
        } else {
            Files.remove(Files.video(pending.movieFileName))
            draftStore.clearPending()
        }
    }

    private func loadDraft() {
        draft = draftStore.loadDraft()
        discardStaleDraft()
    }

    /// An unsaved clip from an earlier day is thrown away.
    private func discardStaleDraft() {
        if let draft, !ZestDates.isToday(ZestDates.dayString(draft.createdAt)) {
            discardDraft(log: true)
        }
    }

    private func discardDraft(log: Bool) {
        guard let old = draft else { return }
        checkTask?.cancel()
        checkTask = nil
        Files.remove(Files.video(old.fileName))
        draftStore.clearDraft()
        draft = nil
        check = .idle
        Notifier.cancelDraftNudge()
        if log {
            events.log(.draftDiscarded, videoId: old.id, customerId: old.target.customerId)
        }
    }

    // MARK: - Review: "Save this video?"

    /// Opens the unsaved clip (after recording, or from the Today banner).
    func openReview() {
        guard draft != nil else {
            route = .today
            return
        }
        notice = nil
        route = .review
        if check == .idle {
            startCheck()
        }
    }

    /// Runs the two-keys check while the clip plays back.
    private func startCheck() {
        guard let draft else { return }
        checkTask?.cancel()
        if draft.target.notOnMyList {
            // Nothing to check against: the office matches these.
            check = .done(CheckResult(signals: .allUnknown, clean: true, reasons: [], better: nil, transcript: nil))
            // Still worth the words: they help the office find the customer.
            let fileURL = Files.video(draft.fileName)
            let draftID = draft.id
            checkTask = Task { @MainActor in
                let transcript = await SpeechChecker.transcribe(fileURL: fileURL, hints: [])
                guard !Task.isCancelled, self.draft?.id == draftID, case .done(var result) = self.check else { return }
                result.transcript = transcript
                self.check = .done(result)
            }
            return
        }

        check = .running
        let hints = SpokenMatcher.hints(for: stops)
        let fileURL = Files.video(draft.fileName)
        let draftID = draft.id
        checkTask = Task { @MainActor in
            let transcript = await SpeechChecker.transcribe(fileURL: fileURL, hints: hints)
            guard !Task.isCancelled, self.draft?.id == draftID else { return }
            self.finishCheck(transcript: transcript)
        }
    }

    private func finishCheck(transcript: String?) {
        guard let draft else { return }
        guard let stop = self.stop(customerId: draft.target.customerId) else {
            // The stop is not on the saved list any more: nothing to compare.
            let verdict = TwoKeys.verdict(.allUnknown)
            check = .done(CheckResult(signals: .allUnknown, clean: false, reasons: verdict.reasons, better: nil, transcript: transcript))
            return
        }
        let outcome = TwoKeys.signals(
            transcript: transcript, fix: draft.location, recordedAt: draft.recordedAt,
            stop: stop, all: stops, farMeters: config.farMeters
        )
        let verdict = TwoKeys.verdict(outcome.signals)
        check = .done(CheckResult(
            signals: outcome.signals, clean: verdict.clean, reasons: verdict.reasons,
            better: verdict.clean ? nil : outcome.better, transcript: transcript
        ))
        if !verdict.clean {
            Haptics.warning()
            events.log(.twoKeysWarning, videoId: draft.id, customerId: draft.target.customerId, detail: signalDetail(outcome.signals))
        }
    }

    private func signalDetail(_ signals: Signals) -> [String: String] {
        ["spoken": signals.spoken.rawValue, "gps": signals.gps.rawValue, "checkIn": signals.checkIn.rawValue]
    }

    /// SAVE. The one tap: the video is queued on disk and sends by itself.
    /// - Returns: false when the check has not finished yet (the screen waits).
    @discardableResult
    func saveDraft(recommend: Bool, override: Bool) -> Bool {
        guard let draft, case .done(let result) = check else { return false }

        let item = QueueItem(
            id: draft.id, stopDate: draft.stopDate, customerId: draft.target.customerId,
            customerName: draft.target.name, appointmentIds: draft.target.appointmentIds,
            notOnMyList: draft.target.notOnMyList, recordedAt: draft.recordedAt, durationSec: draft.durationSec,
            sizeBytes: draft.sizeBytes, location: draft.location, signals: result.signals, override: override,
            recommend: recommend, spokenText: result.transcript.map { String($0.prefix(500)) }, fileName: draft.fileName
        )

        // Recording again replaces the old one by itself: no question asked.
        if let customerId = item.customerId, !item.notOnMyList {
            let old = queue.items.filter {
                $0.customerId == customerId && !$0.notOnMyList && $0.id != item.id && $0.phase != .deleting
            }
            for previous in old {
                uploads.drop(previous.id)
            }
        }

        // Disk first, network after.
        queue.add(item)
        checkTask?.cancel()
        checkTask = nil
        draftStore.clearDraft()
        self.draft = nil
        check = .idle
        Notifier.cancelDraftNudge()
        if override {
            events.log(.override, videoId: item.id, customerId: item.customerId, detail: signalDetail(result.signals))
        }
        Haptics.success()
        uploads.pump()
        syncNudges()
        return true
    }

    /// After the "SAVED ✓" moment: back to the list, or the one-time reminders offer.
    func finishSave() {
        target = nil
        if config.leaveReminders, !defaults.bool(forKey: Key.offeredReminders), location.status == .authorizedWhenInUse {
            defaults.set(true, forKey: Key.offeredReminders)
            defaults.set(true, forKey: Key.askedNotifications)
            route = .remindersOffer
            return
        }
        if !defaults.bool(forKey: Key.askedNotifications) {
            // Asked after the first saved video, never at launch.
            defaults.set(true, forKey: Key.askedNotifications)
            Task { await Notifier.requestPermission() }
        }
        route = .today
        Task { await refreshToday() }
    }

    func answerRemindersOffer(turnOn: Bool) {
        Task {
            let allowed = await Notifier.requestPermission()
            if turnOn && allowed {
                location.askAlways()
            }
        }
        events.log(.permission, detail: ["leaveReminders": turnOn ? "yes" : "no"])
        route = .today
    }

    /// RECORD AGAIN from the review screen: the clip is deleted at once.
    func recordAgainFromReview() {
        guard let old = draft else {
            route = .today
            return
        }
        target = old.target
        events.log(.retake, videoId: old.id, customerId: old.target.customerId)
        discardDraft(log: false)
        recordProblem = nil
        route = .record
        camera.prepare()
        location.refresh(precise: true)
    }

    /// Leaves the review screen without deciding. The draft stays.
    func leaveReview() {
        route = .today
    }

    func showStopPicker() {
        route = .pickStop
    }

    /// PICK THE RIGHT STOP: the same clip, a different customer.
    func retargetDraft(to stop: Stop) {
        guard var current = draft else { return }
        let from = current.target.customerId
        current.target = RecordTarget(stop: stop)
        draft = current
        draftStore.saveDraft(current)
        events.log(.stopSwitched, videoId: current.id, customerId: stop.customerId, detail: ["from": from.map { String($0) } ?? "none"])
        // Same words, same place, new stop: no need to listen again.
        if case .done(let previous) = check {
            finishCheck(transcript: previous.transcript)
        }
        route = .review
    }

    // MARK: - A stop that already has a video

    func summary(for focus: VideoFocus, now: Date = Date()) -> VideoSummary {
        var stop: Stop?
        var item: QueueItem?
        switch focus {
        case .stop(let customerId):
            stop = self.stop(customerId: customerId)
            item = queue.items.liveItem(customerId: customerId) ?? blockedItem(customerId: customerId)
        case .item(let id):
            item = queue.item(id)
        }

        let name = stop?.name ?? item?.customerName ?? "This stop"
        let address = stop?.address ?? ""

        if let item {
            var summary = VideoSummary(
                name: name, address: address, statusText: item.displayText, mode: .waiting,
                stop: stop, itemID: item.id, serverVideoID: nil
            )
            if item.phase == .blocked {
                summary.mode = .blocked(item.blockedMessage ?? UploadManager.lockedMessage)
            } else if HoldWindow.isOpen(recordedAt: item.recordedAt, now: now, holdMinutes: config.holdMinutes) {
                summary.mode = .changeable(closesAt: HoldWindow.closesAt(recordedAt: item.recordedAt, holdMinutes: config.holdMinutes))
            } else if item.phase == .done {
                summary.mode = .locked
            } else {
                summary.mode = .waiting
            }
            return summary
        }

        if let stop, let video = stop.liveVideo {
            var summary = VideoSummary(
                name: name, address: address, statusText: "SENT ✓", mode: .locked,
                stop: stop, itemID: nil, serverVideoID: video.id
            )
            let notOutYet = video.state == .held || video.state == .awaitingUpload
            if video.byYou {
                let closesAt = today?.videos.first { $0.id == video.id }?.emailAt
                if notOutYet, let closesAt, HoldWindow.isOpen(closesAt: closesAt, now: now) {
                    summary.mode = .changeable(closesAt: closesAt)
                }
            } else if notOutYet {
                summary.mode = .otherTech
            }
            return summary
        }

        return VideoSummary(name: name, address: address, statusText: "", mode: .gone, stop: stop, itemID: nil, serverVideoID: nil)
    }

    /// RECORD AGAIN from a stop's video screen. The new clip replaces the old
    /// one by itself when it is saved.
    func recordAgain(_ summary: VideoSummary) {
        if let stop = summary.stop {
            target = RecordTarget(stop: stop)
        } else {
            target = .notOnMyList
        }
        events.log(.retake, videoId: summary.itemID ?? summary.serverVideoID, customerId: summary.stop?.customerId)
        recordProblem = nil
        focus = nil
        route = .record
        camera.prepare()
        location.refresh(precise: true)
    }

    /// DELETE VIDEO, after the confirming tap.
    func deleteVideo(_ summary: VideoSummary) async {
        if let itemID = summary.itemID {
            uploads.drop(itemID)
            notice = "Video deleted."
        } else if let serverVideoID = summary.serverVideoID {
            do {
                _ = try await api.deleteVideo(id: serverVideoID)
                notice = "Video deleted."
            } catch let error as APIError {
                if error.code == "too_late" {
                    notice = error.message.isEmpty ? UploadManager.lockedMessage : error.message
                } else {
                    handle(error)
                    notice = error.message
                }
            } catch {
                notice = "No signal. Please try again."
            }
        }
        focus = nil
        route = signedIn ? .today : .login
        await refreshToday()
    }

    /// OK on a video the server refused: clear it off the list.
    func dismissBlocked(_ summary: VideoSummary) {
        if let itemID = summary.itemID {
            uploads.drop(itemID)
        }
        backToToday()
    }

    /// CALL OFFICE.
    func callOffice() {
        guard let phone = config.officePhone else { return }
        let dialable = phone.filter { $0.isNumber || $0 == "+" }
        guard !dialable.isEmpty, let url = URL(string: "tel:\(dialable)") else { return }
        UIApplication.shared.open(url)
    }

    var canCallOffice: Bool {
        guard let phone = config.officePhone else { return false }
        return phone.contains { $0.isNumber }
    }

    // MARK: - Leave reminders and push

    private func updateLeaveRegions() {
        guard config.leaveReminders, location.isAlways else {
            location.clearLeaveRegions()
            return
        }
        let haveVideo = queue.items.customersWithVideo
        let wanted = stops.filter { !$0.done && $0.liveVideo == nil && !haveVideo.contains($0.customerId) }
        location.setLeaveRegions(wanted)
    }

    private func leftStop(customerId: Int) {
        guard signedIn, config.leaveReminders, let stop = self.stop(customerId: customerId) else { return }
        if hasVideo(stop) { return }
        if let draft, draft.target.customerId == customerId { return }
        Notifier.leaveReminder(name: stop.name, customerId: customerId)
    }

    func pushTokenReceived(_ hex: String) {
        defaults.set(hex, forKey: Key.pushToken)
        sendPushToken(hex)
    }

    private func sendPushToken(_ hex: String) {
        guard signedIn else { return }
        let api = self.api
        #if DEBUG
        let sandbox = true
        #else
        let sandbox = false
        #endif
        Task { try? await api.registerPush(token: hex, sandbox: sandbox) }
    }

    // MARK: - Update gate

    var appStoreURL: URL? {
        let raw = (Bundle.main.object(forInfoDictionaryKey: "ZestAppStoreURL") as? String ?? "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !raw.isEmpty else { return nil }
        return URL(string: raw)
    }

    func openAppStore() {
        if let url = appStoreURL {
            UIApplication.shared.open(url)
        }
    }
}

#if targetEnvironment(simulator)
extension AppModel {
    /// Screenshot mode only (see `ScreenshotMode.swift`). Puts fixture state
    /// straight into the model. `start()` is never called in that mode, so
    /// there is no Keychain, no network, no permission prompt and no timer.
    func loadScreenshot(_ shot: ScreenshotMode.Shot) {
        tech = shot.tech
        today = shot.today
        target = shot.target
        draft = shot.draft
        check = shot.check
        focus = shot.focus
        recordPhase = shot.recordPhase
        seeds = shot.seeds
        offline = false
        notice = nil
        recordProblem = nil
        route = shot.route
    }
}
#endif

private extension APIError {
    var httpBody: APIErrorBody? {
        if case .http(_, let body, _) = self { return body }
        return nil
    }
}
