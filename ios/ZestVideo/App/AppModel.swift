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
        /// A teammate's video, shown where this tech is only helping. The
        /// stop is covered. He can record his own too, as a new video: the
        /// customer gets both, and the teammate's is never replaced.
        case otherTech
        /// Past the window but still on the phone: it sends by itself.
        case waiting
        /// Gone to the customer. Only the office can change it.
        case locked
        /// The server refused it; shows the server's words.
        case blocked(String)
        /// Still on the phone, and the server will not take it. Waiting
        /// will not send it: the screen says to call the office.
        case stuck
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
    /// Filmed as "not on my list": it goes to the office to be matched, not
    /// to a customer. No screen may say an email is on its way.
    var toOffice = false
    /// The server is holding this video for the office to decide (the
    /// visit left the day's route after it was filmed). It reads SENT TO
    /// OFFICE, and nothing may say it went to the customer.
    var withOffice = false
    /// The stop as the phone last saw it, when the office has since taken
    /// it off the list. Only for showing the address and for RECORD AGAIN;
    /// `stop` stays nil, because the stop is not on the list.
    var formerStop: Stop?
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
    /// For a spoken mismatch: did the words name another stop on the list
    /// (a contradiction, never sent without asking), or was no name heard?
    var spokenMiss: SpokenMiss?
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
    /// The lists and the video screen read this moment instead of the
    /// clock, so "7 MIN LEFT" in a picture is still "7 MIN LEFT" a minute
    /// later while the picture is being checked.
    var frozenNow: Date?
    /// Draw a stand-in picture where the camera or the clip would be.
    var standInPicture = false
    /// Put the cursor in the sign-in field as soon as it shows.
    var autoFocus = true
}

/// The four ways a video's state is shown (the colors are in `Controls.swift`).
enum StateTone: Equatable {
    /// Something to tap: blue words.
    case action
    /// Sent, saved: green.
    case good
    /// Waiting, sending, look at this: amber.
    case waiting
    /// It did not go: dark.
    case problem
}

/// What a stop's card (or a video's row) shows: the round badge on the left
/// and the one line at the foot that says what tapping it does.
struct RowState: Equatable {
    enum Badge: Equatable {
        /// No video yet: the stop's number on the route.
        case number(Int)
        /// The server has it: a tick.
        case sent
        /// Still on the phone: an up arrow.
        case waiting
        /// It did not go.
        case problem
    }

    var badge: Badge
    /// In capitals: "RECORD VIDEO", "CHANGE VIDEO · 7 MIN LEFT", "SENT ✓".
    var text: String
    var tone: StateTone
}

/// The app's state and its one router. Screens are plain values of `route`:
/// no navigation stack, so there is nothing to swipe back through by mistake.
@MainActor
final class AppModel: ObservableObject {
    static let shared: AppModel = {
        #if targetEnvironment(simulator)
        UITestMode.markCodeStart()
        // UI-test reset: wipe the app's storage before any store reads it.
        UITestMode.resetIfAsked()
        #endif
        return AppModel()
    }()

    enum Route: Equatable {
        case login
        case today
        case confirm
        case record
        case review
        case pickStop
        case stopVideo
        case queue
        /// "Your phone is almost full" / "Your phone is full", on the way to
        /// the camera (see `enterCamera`).
        case storage
        case remindersOffer
        case updateRequired
    }

    /// Why the tech was stopped on the way to the camera.
    enum StorageStop: Equatable {
        /// Under 500 MB free: he can still record.
        case warn
        /// Under 200 MB free: no recording.
        case blocked
    }

    enum RecordPhase: Equatable {
        case idle
        case arming
        case recording
        case saving
    }

    @Published var route: Route = .login {
        didSet {
            #if targetEnvironment(simulator)
            // Performance tests only: numbers for each screen (see UITestMode).
            UITestMode.routeChanged(to: String(describing: route))
            #endif
        }
    }
    @Published private(set) var tech: TechInfo?
    @Published private(set) var today: TodayResponse? {
        didSet {
            // Every stop seen on today's list is remembered until the day
            // changes, so a stop the office takes off the route keeps its
            // address on the screens that still talk about it.
            guard let today else {
                seenStops = [:]
                return
            }
            for stop in today.stops {
                seenStops[stop.customerId] = stop
            }
        }
    }
    /// Today's stops as last seen, the ones since removed included.
    private var seenStops: [Int: Stop] = [:]
    /// When the day's list was last asked for, answered or not (this run
    /// of the app).
    private var lastListAsk: Date?
    @Published private(set) var target: RecordTarget?
    @Published private(set) var draft: Draft?
    @Published private(set) var check: CheckState = .idle
    @Published private(set) var recordPhase: RecordPhase = .idle
    @Published private(set) var focus: VideoFocus?
    @Published private(set) var storageStop: StorageStop?
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
    private var signingOut = false
    private var checkTask: Task<Void, Never>?
    /// The clip the camera is recording now. Its end is dealt with once.
    private var recordingID: String?
    /// A clip left by an earlier run, or one that would not save, is being
    /// looked at: one look at a time.
    private var recovering = false
    /// What RECORD ANYWAY or TRY AGAIN on the storage screen goes on to do.
    private var afterStorage: (@MainActor () -> Void)?
    /// Videos the tech deleted or recorded over in this run of the app.
    private var droppedVideoIDs = Set<String>()
    /// A teammate's video for a stop, as an earlier answer for the day
    /// showed it (see `TeamVideoMemory`).
    private var teamVideos: [Int: StopVideo] = [:]
    /// The route was asked for again while it was already being fetched.
    private var refreshQueued = false

    private enum Key {
        static let tech = "tech"
        static let askedNotifications = "askedNotifications"
        static let offeredReminders = "offeredReminders"
        static let pushToken = "pushToken"
        static let serverDay = "serverDay"
        static let serverDayFetchedOn = "serverDayFetchedOn"
    }

    private init() {
        #if targetEnvironment(simulator)
        // UI-test mode (see `UITestMode.swift`): talk to the test server and
        // use the stand-in recorder. None of this exists in a build for a
        // real phone.
        let api = APIClient(baseURL: UITestMode.serverURL)
        #else
        let api = APIClient()
        #endif
        let queue = QueueStore()
        self.api = api
        self.queue = queue
        self.uploads = UploadManager(api: api, store: queue)
        #if targetEnvironment(simulator)
        if UITestMode.isOn {
            camera.useStandInRecorder()
            if UITestMode.wantsReset {
                // Uploads left running by the test before this one.
                uploads.cancelEverything()
            }
        }
        #endif
    }

    // MARK: - Derived

    var config: AppConfig { today?.config ?? .fallback }

    var signedIn: Bool { api.token != nil }

    /// Today's stops, with one correction: a video of mine the server is still
    /// waiting for, that this phone no longer has, does not count.
    var stops: [Stop] {
        guard let today else { return [] }
        return today.stops.map { stop in
            guard let video = stop.video else { return stop }
            let local = queue.item(video.id)
            // (a) A video of mine the server still waits for, that this phone
            //     no longer has: it will never arrive.
            let lost = video.byYou && video.state == .awaitingUpload && local == nil
            // (b) A video I deleted or recorded over a moment ago. The server
            //     may not have heard yet; its old answer must not bring it back.
            let dropped = local?.phase == .deleting || droppedVideoIDs.contains(video.id)
            guard lost || dropped else { return stop }
            var copy = stop
            // His own video is gone. If a teammate's covered this stop
            // before he recorded his own, it covers it again; if not, the
            // stop is open.
            copy.video = TeamVideoMemory.whenOwnVideoIsGone(customerId: stop.customerId, remembered: teamVideos)
            return copy
        }
    }

    var ranked: StopRanker.Result {
        var haveVideo = queue.items.customersWithVideo(day: workingDay)
        // (A clip from an earlier day is not today's video for its customer.)
        if let draft, draftStanding == .today, let customerId = draft.target.customerId, !draft.target.notOnMyList {
            haveVideo.insert(customerId)
        }
        return StopRanker.rank(
            stops: stops, fix: location.freshFix(maxAge: 300), farMeters: config.farMeters, haveVideo: haveVideo
        )
    }

    var waitingCount: Int { queue.items.waitingCount }

    func stop(customerId: Int?) -> Stop? {
        guard let customerId else { return nil }
        return stops.first { $0.customerId == customerId }
    }

    /// The number on the stop's card: its place on the route as planned,
    /// counting from 1. Not where the card sits on the screen, and nothing
    /// depends on it. See `RouteNumbers`.
    func routeNumber(of stop: Stop) -> Int {
        RouteNumbers.number(of: stop.customerId, in: stops)
    }

    /// The stop on the confirm screen is no longer on the list the server
    /// sends: the office changed the route while the tech had it open. He
    /// is told, and can still record (the work may be done already).
    var targetLeftList: Bool {
        guard let target, !target.notOnMyList else { return false }
        return RouteList.hasLeft(customerId: target.customerId, list: today?.stops)
    }

    /// What a stop's card shows, and so what tapping it does.
    func rowState(for stop: Stop, now: Date = Date()) -> RowState {
        if let draft, draftStanding == .today, !draft.target.notOnMyList, draft.target.customerId == stop.customerId {
            return RowState(badge: .waiting, text: "NOT SAVED YET · FINISH IT", tone: .waiting)
        }
        return rowState(summary(for: .stop(stop.customerId), now: now), number: routeNumber(of: stop), now: now)
    }

    /// What a video's row on My Videos shows.
    func rowState(for item: QueueItem, now: Date = Date()) -> RowState {
        rowState(summary(for: videoFocus(for: item), now: now), number: nil, now: now)
    }

    /// A video is looked at through its stop when it is today's video for
    /// that stop, and by itself otherwise (off the list, replaced, or from
    /// an earlier day: yesterday's video is not about today's stop).
    private func videoFocus(for item: QueueItem) -> VideoFocus {
        if let customerId = item.customerId, !item.notOnMyList,
           queue.items.liveItem(customerId: customerId, day: workingDay)?.id == item.id {
            return .stop(customerId)
        }
        return .item(item.id)
    }

    /// The day a video on My Videos was filmed, when it was not today:
    /// "Filmed Thursday, Nov 12".
    func filmedText(for item: QueueItem) -> String? {
        guard DraftRule.standing(stopDate: item.stopDate, today: workingDay) != .today else { return nil }
        return DraftRule.filmedText(stopDate: item.stopDate)
    }

    private func rowState(_ summary: VideoSummary, number: Int?, now: Date) -> RowState {
        // Still on the phone, whatever the clock says.
        let onPhone = summary.itemID.flatMap { queue.item($0) }?.isWaiting ?? false
        switch summary.mode {
        case .gone:
            return RowState(badge: number.map { .number($0) } ?? .problem, text: "RECORD VIDEO", tone: .action)
        case .changeable(let closesAt):
            let seconds = max(0, Int(closesAt.timeIntervalSince(now).rounded(.up)))
            let minutes = HoldWindow.minutesLeft(secondsLeft: seconds)
            return RowState(badge: onPhone ? .waiting : .sent, text: "CHANGE VIDEO · \(minutes) MIN LEFT", tone: .action)
        case .otherTech:
            // Covered, in green like SENT. Tapping it still offers RECORD VIDEO.
            return RowState(badge: .sent, text: AppModel.teamText, tone: .good)
        case .waiting:
            let text = summary.statusText == "WAITING" ? "WAITING TO SEND" : summary.statusText
            return RowState(badge: .waiting, text: text, tone: .waiting)
        case .stuck:
            return RowState(badge: .problem, text: VideoWords.stuckRow, tone: .problem)
        case .locked:
            // "SENT ✓", or "SENT TO OFFICE" for a video not on the list.
            return RowState(badge: .sent, text: summary.toOffice ? VideoWords.sentToOffice : VideoWords.sent, tone: .good)
        case .blocked:
            return RowState(badge: .problem, text: "NOT SENT · SEE WHY", tone: .problem)
        }
    }

    /// What a stop says when a teammate recorded it and this tech did not.
    static let teamText = "DONE BY TEAM"

    /// My own videos as the server lists them, less the ones this phone
    /// knows are gone (see `stops`).
    private var myServerVideos: [TodayVideo] {
        (today?.videos ?? []).filter { video in
            let local = queue.item(video.id)
            if droppedVideoIDs.contains(video.id) || local?.phase == .deleting { return false }
            return !(video.state == .awaitingUpload && local == nil)
        }
    }

    /// For the header: how many of today's stops have a video the server
    /// holds. A stop a teammate covered counts: it is not waiting on anyone.
    var videosSentCount: Int {
        let sentHere = Set(queue.items.forDay(workingDay).filter { $0.phase == .done && !$0.notOnMyList }.compactMap { $0.customerId })
        return stops.filter { stop in
            sentHere.contains(stop.customerId) || (stop.liveVideo?.state.serverHasFile ?? false)
        }.count
    }

    private func blockedItem(customerId: Int) -> QueueItem? {
        queue.items.forDay(workingDay).first { $0.customerId == customerId && !$0.notOnMyList && $0.phase == .blocked }
    }

    /// Videos that deleting the app would lose: waiting or sending, and a
    /// clip that is recorded and not saved yet.
    var videosOnPhoneCount: Int { waitingCount + (draft == nil ? 0 : 1) }

    private func hasVideo(_ stop: Stop) -> Bool {
        queue.items.liveItem(customerId: stop.customerId, day: workingDay) != nil
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
        today = todayStore.load { [defaults] day in
            AppModel.isCurrent(day: day, defaults: defaults)
        }
        queue.pruneOldDays(today: workingDay)
        loadDraft()

        uploads.onUnauthorized = { [weak self] in self?.signedOutByServer() }
        uploads.onUpdateRequired = { [weak self] in self?.route = .updateRequired }
        uploads.onNotice = { [weak self] text in self?.notice = text }
        uploads.onChange = { [weak self] in self?.queueChanged() }
        uploads.onUploadProblem = { [weak self] id, detail in self?.uploadProblem(videoID: id, detail: detail) }
        location.onLeave = { [weak self] customerId in self?.leftStop(customerId: customerId) }

        route = signedIn ? .today : .login
        uploads.resume()
        Task { await recoverInterruptedRecording() }
    }

    /// The app came to the front.
    func becameActive() {
        guard started else { return }
        // A new day: yesterday's route and finished rows go. An unsaved clip
        // stays: the tech is asked about it (see `askAboutEarlierDraft`).
        if let today, !AppModel.isCurrent(day: today.date, defaults: defaults) {
            self.today = nil
            teamVideos = [:]
            todayStore.wipe()
            queue.pruneOldDays(today: workingDay)
        }
        discardExpiredDraft()
        // A clip whose file could not be made ready the last time (the
        // phone was locked in the middle of it, say) is tried again now.
        Task { await recoverInterruptedRecording() }
        guard signedIn else { return }
        askAboutEarlierDraft()
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
        // And look at the list again: the office may have changed the route.
        Task { await refreshToday() }
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

    /// Files the sweep must leave alone although no queue row points at them.
    private func protectedFileNames() -> Set<String> {
        var names = Set<String>()
        if let draft { names.insert(draft.fileName) }
        if let pending = draftStore.loadPending() {
            // The camera's file, and the .mp4 it is being re-wrapped into.
            // Without the second, a sweep that lands in the middle of the
            // re-wrap (at launch, while a recording is being recovered) would
            // delete the clip as it is being saved.
            names.insert(pending.movieFileName)
            names.insert(pending.videoFileName)
            names.insert("small-" + pending.videoFileName)
        }
        return names
    }

    /// An upload ended with an error or was refused. Kept in the audit
    /// trail, so "why did this video not go?" has an answer afterwards.
    private func uploadProblem(videoID: String, detail: [String: String]) {
        events.log(.uploadFailed, videoId: videoID, customerId: queue.item(videoID)?.customerId, detail: detail)
    }

    /// A video reached the server or left the queue.
    private func queueChanged() {
        syncNudges()
        // A video just moved, so there is signal: send the audit trail too.
        // (Otherwise it would wait for the app to be opened again.)
        events.flush(using: api)
        // The route's own view of each stop is now out of date.
        Task { await refreshToday() }
    }

    // MARK: - Which day is it

    /// Is a route dated `day` the one for right now?
    ///
    /// Normally that just means "the phone's date". But the date on a route
    /// is the office's, and a phone in another time zone (travel, or Apple's
    /// reviewer) can be a calendar day ahead or behind. So the server's day
    /// also counts as current for the rest of the phone-day it was fetched
    /// on. It is still wiped the next morning.
    private static func isCurrent(day: String, defaults: UserDefaults) -> Bool {
        let phoneDay = ZestDates.dayString(Date())
        if day == phoneDay { return true }
        return defaults.string(forKey: Key.serverDay) == day
            && defaults.string(forKey: Key.serverDayFetchedOn) == phoneDay
    }

    /// The date today's videos are filed under: the office's day when known.
    private var workingDay: String {
        let phoneDay = ZestDates.dayString(Date())
        if let today, AppModel.isCurrent(day: today.date, defaults: defaults) { return today.date }
        if defaults.string(forKey: Key.serverDayFetchedOn) == phoneDay,
           let serverDay = defaults.string(forKey: Key.serverDay) {
            return serverDay
        }
        return phoneDay
    }

    // MARK: - Today

    /// How old the list may be before it is fetched again as My Route
    /// comes on screen or a stop is tapped: half a minute. (The server's
    /// own copy can be three minutes behind FieldRoutes; the phone should
    /// not add much to that.)
    static let staleListSeconds: TimeInterval = 30

    /// Fetches the list again if it was last asked for more than
    /// `staleListSeconds` ago. Never waits: the screen carries on.
    /// (The first fetch of a run is `becameActive`'s; this adds none to it.)
    func refreshTodayIfStale() {
        guard signedIn, !refreshing, let lastListAsk,
              Date().timeIntervalSince(lastListAsk) >= AppModel.staleListSeconds else { return }
        Task {
            guard !refreshing else { return }
            await refreshToday()
        }
    }

    func refreshToday() async {
        guard signedIn else { return }
        // Asked again while a fetch is in the air (a video was just deleted
        // or confirmed, say): that fetch may have left before the change, so
        // one more goes out as soon as it is back.
        if refreshing {
            refreshQueued = true
            return
        }
        refreshing = true
        defer { refreshing = false }
        repeat {
            refreshQueued = false
            await fetchToday()
        } while refreshQueued && signedIn
    }

    private func fetchToday() async {
        // (Answered or not: a phone with no signal does not ask on every tap.)
        defer { lastListAsk = Date() }
        do {
            let fresh = try await api.today()
            today = fresh
            tech = fresh.tech
            if let data = try? ZestJSONBridge.encoder.encode(fresh.tech) {
                defaults.set(data, forKey: Key.tech)
            }
            todayStore.save(fresh)
            // Remember which phone-day the server's day was fetched on (see `isCurrent`).
            defaults.set(fresh.date, forKey: Key.serverDay)
            defaults.set(ZestDates.dayString(Date()), forKey: Key.serverDayFetchedOn)
            offline = false
            // The server's word on each of my videos.
            for video in fresh.videos where queue.item(video.id) != nil {
                // (A stop's own video object may be the one that says the
                // office is holding it: either list's word counts.)
                let held = video.isWithOffice || fresh.stops.contains { $0.video?.id == video.id && $0.video?.isWithOffice == true }
                queue.apply(.serverState(video.state, emailAt: video.emailAt, withOffice: held), to: video.id)
            }
            // Dropped videos the server no longer lists as live can be forgotten.
            let stillLive = Set(fresh.stops.compactMap { $0.liveVideo?.id })
            droppedVideoIDs = droppedVideoIDs.intersection(stillLive)
            // A teammate's video the server showed for a stop is remembered
            // for as long as the server has not said the stop is open.
            teamVideos = TeamVideoMemory.after(fresh.stops, remembered: teamVideos)
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
        case .offline, .badResponse, .http:
            // Any failed load counts as "could not reach the server", so the
            // screen says so instead of looking as if it were still loading.
            offline = true
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
        guard signedIn, !signingOut else { return }
        signingOut = true
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
        signingOut = false
        Keychain.remove(Keychain.deviceTokenKey)
        api.token = nil
        tech = nil
        today = nil
        teamVideos = [:]
        todayStore.wipe()
        defaults.removeObject(forKey: Key.tech)
        defaults.removeObject(forKey: Key.serverDay)
        defaults.removeObject(forKey: Key.serverDayFetchedOn)
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
        // The office may have changed the route since the list was last
        // fetched. The stop opens at once from the list the phone has; the
        // fresh list follows in a moment, and if this stop has left it the
        // confirm screen says so.
        refreshTodayIfStale()
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
        focus = videoFocus(for: item)
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
        enterCamera { [weak self] in
            self?.route = .record
            self?.camera.prepare()
        }
    }

    func logFarWarning() {
        events.log(.farWarning, customerId: target?.customerId)
    }

    // MARK: - The one door to the camera

    /// True when there is no room to record at all. (The confirm screen
    /// asks, so that a full phone is said before anything else is.)
    var phoneIsFull: Bool { StorageMonitor.level() == .block }

    /// Every way into the camera goes through here: THIS IS IT, RECORD
    /// AGAIN on the review screen, RECORD AGAIN on a stop's video.
    ///
    /// "Check free space before recording. Warn under 500 MB free and block
    /// under 200 MB." Under 200 MB the camera is not reached and `enter`
    /// never runs, so nothing is lost on the way (RECORD AGAIN deletes the
    /// old clip only once the new one can be recorded). Under 500 MB the
    /// tech is told once and RECORD ANYWAY carries on.
    private func enterCamera(acceptedWarning: Bool = false, _ enter: @escaping @MainActor () -> Void) {
        let level = StorageMonitor.level()
        if level == .block || (level == .warn && !acceptedWarning) {
            events.log(level == .block ? .storageBlock : .storageWarn, customerId: target?.customerId)
            storageStop = level == .block ? .blocked : .warn
            afterStorage = enter
            route = .storage
            return
        }
        storageStop = nil
        afterStorage = nil
        enter()
    }

    /// RECORD ANYWAY on "Your phone is almost full".
    func recordAnyway() {
        guard route == .storage, let enter = afterStorage else { return }
        // The space is looked at again: it may have run out while he read.
        enterCamera(acceptedWarning: true, enter)
    }

    /// TRY AGAIN on "Your phone is full", after freeing some space.
    func tryStorageAgain() {
        guard route == .storage, let enter = afterStorage else { return }
        enterCamera(enter)
    }

    /// BACK on either storage screen: nothing was recorded and nothing was
    /// deleted. An unsaved clip is still there to be saved.
    func leaveStorage() {
        storageStop = nil
        afterStorage = nil
        if draft != nil {
            target = nil
            focus = nil
            openReview()
        } else {
            backToToday()
        }
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
        // The same check once more at the button: the phone may have filled
        // up while the camera was open (or a clip that did not save is
        // being recorded again).
        if StorageMonitor.level() == .block {
            enterCamera { [weak self] in
                self?.route = .record
                self?.camera.prepare()
            }
            return
        }
        recordPhase = .arming
        recordProblem = nil

        // A new recording replaces any unsaved one.
        discardDraft(log: true)

        let pending = PendingRecording(
            id: QueueItem.newID(), target: target, stopDate: today?.date ?? workingDay,
            location: location.freshFix(), startedAt: Date()
        )
        draftStore.savePending(pending)
        events.log(.recordStart, videoId: pending.id, customerId: target.customerId)

        // Buzz and beep first, so the beep is not on the customer's video.
        Sounds.allowDuringRecording()
        Haptics.thump()
        Sounds.beginRecording()
        let maxSeconds = config.maxSeconds
        let movie = Files.video(pending.movieFileName)
        recordingID = pending.id
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 350_000_000)
            self.recordPhase = .recording
            let rolling = Date()
            self.camera.startRecording(to: movie, maxSeconds: maxSeconds) { [weak self] end in
                Task { @MainActor in
                    await self?.finishRecording(pending: pending, end: end)
                }
            }
            // Never stuck. The camera reports the end of every recording.
            // If one day it does not, the recording is ended here a few
            // seconds after its time is up: whatever is on disk is handed
            // over, the red frame comes down, and the tech can carry on.
            try? await Task.sleep(nanoseconds: UInt64((Double(maxSeconds) + RecordingRule.overdueSeconds) * 1_000_000_000))
            if self.recordingID == pending.id, self.recordPhase == .recording,
               RecordingRule.isOverdue(elapsed: Date().timeIntervalSince(rolling), maxSeconds: Double(maxSeconds)) {
                self.camera.giveUpRecording(of: movie)
            }
        }
    }

    /// Tapping the ring ends the clip early, but not in the first three seconds.
    func stopRecordingEarly() {
        guard recordPhase == .recording, let startedAt = camera.recordingStartedAt,
              Countdown.canStopEarly(elapsed: Date().timeIntervalSince(startedAt)) else { return }
        camera.stopRecording()
    }

    /// The camera stopped: at the time limit, at the tech's tap, or because
    /// the phone took it away (a call, the screen locked: `end.cut`).
    private func finishRecording(pending: PendingRecording, end: RecordingEnd) async {
        // Handed over once only (the camera, or the watch for a silent one).
        guard recordingID == pending.id else { return }
        recordingID = nil
        recordPhase = .saving
        Haptics.thump()
        Sounds.endRecording()

        // The phone may have just been locked, or a call may have the
        // screen: iOS is asked for the time to finish saving the clip.
        // (If that time runs out first, the camera's file and its record
        // are on disk: the clip is saved the next time the app is opened.)
        let background = UIApplication.shared.beginBackgroundTask(withName: "zest.save.clip", expirationHandler: nil)
        defer {
            if background != .invalid {
                UIApplication.shared.endBackgroundTask(background)
            }
        }

        // How it ended is written down first, so that a second try at
        // saving it (or a restart) treats the clip the same way.
        var ended = pending
        ended.cut = end.cut
        ended.seconds = end.seconds
        if let rolled = end.clip?.startedAt {
            // When the camera really began, not when the button was tapped.
            ended.startedAt = rolled
        }
        draftStore.savePending(ended)

        let made = await makeDraft(from: ended)
        recordPhase = .idle
        settle(made, pending: ended)
    }

    private enum Made {
        /// The clip is saved as a draft. `seconds` is how long it really is.
        case draft(Draft, seconds: Double)
        /// The camera's file had nothing in it worth keeping.
        case nothing
        /// The file could not be made ready this time. It is kept, and
        /// tried again when the app is next opened.
        case keptForLater
    }

    /// Re-wraps the camera file as the upload-ready .mp4 and stores it as a draft.
    private func makeDraft(from pending: PendingRecording) async -> Made {
        let movie = Files.video(pending.movieFileName)
        let fileName = pending.videoFileName
        let output: VideoExporter.Output
        switch await VideoExporter.makeMP4(from: movie, to: Files.video(fileName)) {
        case .success(let made):
            output = made
        case .failure(.nothingThere):
            Files.remove(movie)
            draftStore.clearPending()
            return .nothing
        case .failure(.failed):
            // Nothing lost: the camera's own file stays where it is.
            var kept = pending
            kept.tries = (pending.tries ?? 0) + 1
            if RecordingRule.keepForAnotherTry(triesSoFar: kept.tries ?? 1) {
                draftStore.savePending(kept)
                return .keptForLater
            }
            Files.remove(movie)
            draftStore.clearPending()
            return .nothing
        }

        let draft = Draft(
            id: pending.id, target: pending.target, fileName: fileName, stopDate: pending.stopDate,
            recordedAt: pending.startedAt, durationSec: output.seconds, sizeBytes: output.bytes,
            location: pending.location, createdAt: Date(), cut: pending.cut
        )
        // On disk the instant recording stops: it survives anything from here.
        // The draft is written BEFORE the recording marker is cleared, so
        // there is no moment at which a kill leaves the clip unaccounted for.
        draftStore.saveDraft(draft)
        self.draft = draft
        Files.remove(movie)
        draftStore.clearPending()
        Notifier.scheduleDraftNudge(name: draft.target.name)

        // How long the clip ran. The file says; except in a UI test, where
        // the stand-in camera's file is always the same two seconds and the
        // clock is what was "recorded".
        var seconds = output.seconds
        #if targetEnvironment(simulator)
        if camera.standIn, let clock = pending.seconds {
            seconds = clock
        }
        #endif
        return .draft(draft, seconds: seconds)
    }

    /// What becomes of a recording once its file has been dealt with (see
    /// `RecordingRule`): "Save this video?", with a line if it was cut
    /// short; or, cut too short to be worth sending, one line on the
    /// camera screen and the record button again.
    private func settle(_ made: Made, pending: PendingRecording) {
        let customerId = pending.target.customerId
        switch made {
        case .draft(_, let seconds):
            switch RecordingRule.next(seconds: seconds, cut: pending.cut) {
            case .review:
                events.log(
                    .recordStop, videoId: pending.id, customerId: customerId,
                    detail: RecordingRule.auditDetail(seconds: seconds, cut: pending.cut, kept: true)
                )
                // "Save this video?" straight away, not a card to go and
                // find. (Unless another screen is asking something.)
                guard signedIn, route == .record || route == .today else { return }
                // The camera's job is done: free the microphone and the battery.
                camera.shutDown()
                openReview()
            case .recordAgain(let message):
                events.log(
                    .recordStop, videoId: pending.id, customerId: customerId,
                    detail: RecordingRule.auditDetail(seconds: seconds, cut: pending.cut, kept: false)
                )
                discardDraft(log: false)
                say(message)
            }
        case .nothing:
            events.log(
                .recordStop, videoId: pending.id, customerId: customerId,
                detail: RecordingRule.auditDetail(seconds: 0, cut: pending.cut ?? .unknown, kept: false)
            )
            say(pending.cut.map { RecordingRule.tooShort($0) } ?? RecordingRule.notSaved)
        case .keptForLater:
            // Said only to a tech who is looking at the camera. If the
            // phone is locked or on a call, the clip is tried again when
            // the app is back, and he is asked about it then.
            if UIApplication.shared.applicationState == .active {
                say(RecordingRule.notSaved)
            }
        }
    }

    /// One line about a clip that was not kept: on the camera screen if he
    /// is there, else at the top of My Route.
    private func say(_ message: String) {
        if route == .record {
            recordProblem = message
        } else if route == .today {
            notice = message
        }
    }

    /// A recording was left unfinished: the app was killed in the middle of
    /// a clip, or the clip's file could not be made ready the first time.
    /// Looked at when the app starts and each time it is opened.
    private func recoverInterruptedRecording() async {
        guard !recovering, recordPhase == .idle, var pending = draftStore.loadPending() else { return }
        recovering = true
        defer { recovering = false }
        guard draft == nil, Files.size(of: Files.video(pending.movieFileName)) > 0 else {
            Files.remove(Files.video(pending.movieFileName))
            draftStore.clearPending()
            return
        }
        if pending.seconds == nil, pending.cut == nil {
            // The camera never got to say how the clip ended: the app
            // itself was stopped while it was recording.
            pending.cut = .appStopped
        }
        let made = await makeDraft(from: pending)
        // (A new recording may have begun while that ran: leave it be.)
        guard recordPhase == .idle else { return }
        settle(made, pending: pending)
    }

    private func loadDraft() {
        #if targetEnvironment(simulator)
        // UI tests only: make the saved clip some days old (see UITestMode).
        if let days = UITestMode.draftAgeDays, let saved = draftStore.loadDraft() {
            draftStore.saveDraft(UITestMode.aged(saved, days: days))
        }
        #endif
        draft = draftStore.loadDraft()
        // Killed between "queued" and "draft cleared": the video is already
        // in the queue, so the leftover draft record must not offer it again.
        if let loaded = draft, queue.item(loaded.id) != nil {
            draftStore.clearDraft()
            draft = nil
        }
        discardExpiredDraft()
    }

    /// Where the unsaved clip stands: filmed today, on an earlier day, or
    /// too long ago to send.
    var draftStanding: DraftRule.Standing {
        guard let draft else { return .today }
        return DraftRule.standing(stopDate: draft.stopDate, today: workingDay)
    }

    /// The one line on "Save this video?" when the clip was cut short: "A
    /// call stopped this video early."
    var draftCutNote: String? {
        draft?.cut.map { RecordingRule.reviewNote($0) }
    }

    /// "Filmed Thursday, Nov 12", for a clip from an earlier day.
    var draftFilmedText: String? {
        guard let draft, draftStanding != .today else { return nil }
        return DraftRule.filmedText(stopDate: draft.stopDate)
    }

    /// The draft stays. An unsaved clip is not thrown away because the day
    /// changed; only when it is too old for the server to take (14 days),
    /// and then it is logged.
    private func discardExpiredDraft() {
        guard let draft, case .expired(let daysOld) = draftStanding else { return }
        events.log(
            .draftDiscarded, videoId: draft.id, customerId: draft.target.customerId,
            detail: ["reason": "too_old", "daysOld": String(daysOld), "stopDate": draft.stopDate]
        )
        discardDraft(log: false)
    }

    /// On the next open after the day changed, the one question about the
    /// clip that was left unsaved: save it or delete it.
    private func askAboutEarlierDraft() {
        guard signedIn, draft != nil, DraftRule.canDelete(draftStanding), route == .today else { return }
        openReview()
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
        // The names to listen for: today's list, and the clip's own stop
        // (which is not on today's list when the clip is from an earlier day).
        let hints = SpokenMatcher.hints(for: (draft.target.stop.map { [$0] } ?? []) + stops)
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
        // A clip filmed today is checked against today's list. A clip from
        // an earlier day is checked against the stop as it was when it was
        // filmed, which the draft carries: that day's list is gone, and the
        // same customer on today's list is a different visit.
        let filmedToday = draftStanding == .today
        let liveStop = filmedToday ? self.stop(customerId: draft.target.customerId) : nil
        guard let stop = liveStop ?? draft.target.stop else {
            // Nothing to compare with.
            let verdict = TwoKeys.verdict(.allUnknown)
            check = .done(CheckResult(signals: .allUnknown, clean: false, reasons: verdict.reasons, better: nil, transcript: transcript))
            return
        }
        let outcome = TwoKeys.signals(
            transcript: transcript, fix: draft.location, recordedAt: draft.recordedAt,
            stop: stop, all: liveStop == nil ? [stop] : stops, farMeters: config.farMeters,
            // "This is Sam with Zest" must not point at a customer called Sam.
            techName: tech?.firstName
        )
        // Only keys that can be read for this stop count towards the two.
        // And if the words named another stop on the list, it is never
        // clean, whatever the other two keys say.
        let verdict = TwoKeys.verdict(outcome.signals, stop: stop, spokenMiss: outcome.spokenMiss)
        let result = CheckResult(
            signals: outcome.signals, clean: verdict.clean, reasons: verdict.reasons,
            better: verdict.clean ? nil : outcome.better, transcript: transcript,
            spokenMiss: outcome.spokenMiss
        )
        check = .done(result)
        if !verdict.clean {
            Haptics.warning()
            events.log(.twoKeysWarning, videoId: draft.id, customerId: draft.target.customerId, detail: signalDetail(result))
        }
    }

    /// What the audit trail keeps of a check: the three keys, and for a
    /// spoken mismatch whether another customer's name was said or none.
    private func signalDetail(_ result: CheckResult) -> [String: String] {
        TwoKeys.auditDetail(result.signals, spokenMiss: result.spokenMiss, better: result.better)
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
            // Only that day's: a late-saved clip from yesterday and today's
            // video for the same customer are two visits, and both are kept.
            let old = queue.items.filter {
                $0.customerId == customerId && !$0.notOnMyList && $0.id != item.id && $0.phase != .deleting
                    && $0.stopDate == item.stopDate
            }
            for previous in old {
                droppedVideoIDs.insert(previous.id)
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
            events.log(.override, videoId: item.id, customerId: item.customerId, detail: signalDetail(result))
        }
        Haptics.success()
        uploads.pump()
        syncNudges()
        return true
    }

    /// After the "SAVED ✓" moment: back to the list, or the one-time reminders offer.
    func finishSave() {
        target = nil
        // The server may have signed this phone out, or asked for an update,
        // while "Saved" was showing. Those screens stand.
        guard signedIn, route != .updateRequired else {
            if !signedIn { route = .login }
            return
        }
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
        route = signedIn ? .today : .login
    }

    /// RECORD AGAIN from the review screen. The old clip is deleted only
    /// once there is room to record the new one.
    func recordAgainFromReview() {
        guard let old = draft else {
            // A second tap of the same button: the first already moved on.
            if route == .review { route = .today }
            return
        }
        // A clip from an earlier day cannot be recorded again: the tech is
        // not at that stop any more. It is saved or deleted.
        guard DraftRule.canRecordAgain(draftStanding) else { return }
        target = old.target
        enterCamera { [weak self] in
            guard let self, let old = self.draft else { return }
            self.events.log(.retake, videoId: old.id, customerId: old.target.customerId)
            self.discardDraft(log: false)
            self.recordProblem = nil
            self.route = .record
            self.camera.prepare()
            self.location.refresh(precise: true)
        }
    }

    /// YES, DELETE on the question about a clip from an earlier day.
    func deleteDraft() {
        guard draft != nil else {
            if route == .review { route = signedIn ? .today : .login }
            return
        }
        discardDraft(log: true)
        target = nil
        notice = "Video deleted."
        route = signedIn ? .today : .login
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
            item = queue.items.liveItem(customerId: customerId, day: workingDay) ?? blockedItem(customerId: customerId)
        case .item(let id):
            item = queue.item(id)
        }

        // A video for a stop the office has since taken off the list: the
        // stop as it was when the phone last saw it.
        var formerStop: Stop?
        if stop == nil, let item, item.stopDate == workingDay, let customerId = item.customerId {
            formerStop = seenStops[customerId]
        }

        let name = stop?.listName ?? item?.customerName ?? "This stop"
        let address = stop?.address ?? formerStop?.address ?? ""

        if let item {
            var summary = VideoSummary(
                name: name, address: address, statusText: item.displayText, mode: .waiting,
                stop: stop, itemID: item.id, serverVideoID: nil, toOffice: item.goesToOffice, formerStop: formerStop
            )
            summary.withOffice = item.withOffice == true
            if item.phase == .blocked {
                summary.mode = .blocked(item.blockedMessage ?? UploadManager.lockedMessage)
            } else if item.isStuck {
                summary.mode = .stuck
            } else if item.phase == .done, item.withOffice == true {
                // The office has it: the server takes no change from the
                // phone now, whatever the clock says.
                summary.mode = .locked
            } else if HoldWindow.isOpen(recordedAt: item.recordedAt, now: now, holdMinutes: config.holdMinutes) {
                summary.mode = .changeable(closesAt: HoldWindow.closesAt(recordedAt: item.recordedAt, holdMinutes: config.holdMinutes))
            } else if item.phase == .done {
                summary.mode = .locked
            } else {
                summary.mode = .waiting
            }
            return summary
        }

        // One video per tech, per property: only his own video can be
        // changed here. A teammate's is something to know about.
        if let stop {
            let cover = StopCover.of(stop, ownVideos: myServerVideos)
            if let video = cover.video {
                // (A video the server holds for the office has gone to nobody.)
                let held = cover.isMine && video.isWithOffice
                var summary = VideoSummary(
                    name: name, address: address, statusText: held ? VideoWords.sentToOffice : VideoWords.sent, mode: .locked,
                    stop: stop, itemID: nil, serverVideoID: video.id, toOffice: held, withOffice: held
                )
                switch cover.standing(now: now) {
                case .change(let closesAt):
                    summary.mode = .changeable(closesAt: closesAt)
                case .teamCovered:
                    summary.mode = .otherTech
                    summary.statusText = AppModel.teamText
                    // Not his video: nothing here may delete or replace it.
                    summary.serverVideoID = nil
                case .locked, .record:
                    break
                }
                return summary
            }
        }

        return VideoSummary(name: name, address: address, statusText: "", mode: .gone, stop: stop, itemID: nil, serverVideoID: nil)
    }

    /// RECORD VIDEO at a stop a teammate already recorded. A plain new
    /// recording, by the same steps as any other: nothing is replaced, and
    /// the customer gets both videos.
    func recordOwn(_ summary: VideoSummary) {
        guard summary.mode == .otherTech, let stop = summary.stop else { return }
        focus = nil
        beginConfirm(RecordTarget(stop: stop))
    }

    /// RECORD AGAIN from a stop's video screen. The new clip replaces this
    /// tech's own old one by itself when it is saved.
    func recordAgain(_ summary: VideoSummary) {
        // Only ever his own video: a teammate's is not his to replace.
        guard summary.mode != .otherTech else {
            recordOwn(summary)
            return
        }
        let item = summary.itemID.flatMap { queue.item($0) }
        if let stop = summary.stop {
            target = RecordTarget(stop: stop)
        } else if let item, !item.notOnMyList, item.stopDate == workingDay, let customerId = item.customerId {
            // His own video of today for a customer, whose stop the office
            // has since taken off the list. Recording again is still for
            // that customer, and the new clip replaces this one. (It used
            // to become a "not on my list" video here, which replaced
            // nothing: the old clip went to the customer and the new one
            // to the office.)
            target = summary.formerStop.map { RecordTarget(stop: $0) }
                ?? RecordTarget(customerId: customerId, name: item.customerName, appointmentIds: item.appointmentIds)
        } else {
            target = .notOnMyList
        }
        let customer = target?.customerId
        enterCamera { [weak self] in
            guard let self else { return }
            self.events.log(.retake, videoId: summary.itemID ?? summary.serverVideoID, customerId: customer)
            self.recordProblem = nil
            self.focus = nil
            self.route = .record
            self.camera.prepare()
            self.location.refresh(precise: true)
        }
    }

    /// DELETE VIDEO, after the confirming tap. Only ever his own video.
    func deleteVideo(_ summary: VideoSummary) async {
        guard summary.mode != .otherTech else { return }
        if let itemID = summary.itemID {
            droppedVideoIDs.insert(itemID)
            uploads.drop(itemID)
            notice = "Video deleted."
        } else if let serverVideoID = summary.serverVideoID {
            do {
                _ = try await api.deleteVideo(id: serverVideoID)
                droppedVideoIDs.insert(serverVideoID)
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
        guard let url = officeCallURL else { return }
        UIApplication.shared.open(url)
    }

    private var officeCallURL: URL? {
        guard let phone = config.officePhone else { return nil }
        let dialable = phone.filter { $0.isASCII && ($0.isNumber || $0 == "+") }
        guard dialable.contains(where: { $0.isNumber }) else { return nil }
        return URL(string: "tel:\(dialable)")
    }

    /// True only when this device can really place the call. An iPad (where
    /// Apple also runs the app) cannot, so there the number is shown instead
    /// of a button that would do nothing.
    var canCallOffice: Bool {
        guard let url = officeCallURL else { return false }
        return UIApplication.shared.canOpenURL(url)
    }

    /// The office number in words, for devices that cannot dial it.
    var officePhoneText: String? {
        guard !canCallOffice, let phone = config.officePhone else { return nil }
        let digits = PhoneFormat.digits(from: phone)
        guard PhoneFormat.isComplete(digits) else { return nil }
        return "Office: " + PhoneFormat.display(digits)
    }

    // MARK: - Terms and privacy

    /// Opens the server's Terms page in Safari.
    func openTerms() {
        openServerPage("/terms")
    }

    /// Opens the server's Privacy Policy page in Safari.
    func openPrivacy() {
        openServerPage("/privacy")
    }

    private func openServerPage(_ path: String) {
        var base = api.baseURL.absoluteString
        while base.hasSuffix("/") { base.removeLast() }
        if let url = URL(string: base + path) {
            UIApplication.shared.open(url)
        }
    }

    // MARK: - Leave reminders and push

    private func updateLeaveRegions() {
        guard config.leaveReminders, location.isAlways else {
            location.clearLeaveRegions()
            return
        }
        let haveVideo = queue.items.customersWithVideo(day: workingDay)
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
        storageStop = shot.storageStop
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
