#if targetEnvironment(simulator)
import Foundation
import ZestCore

/// Screenshot mode. **Simulator only**: this whole file is compiled out of
/// any build that can run on a real phone, so it cannot be reached there.
///
/// Launch the app with `-ZestScreen <name>` and it opens straight on that
/// screen with made-up data: no sign-in, no network, no Keychain, no
/// permission prompts, no camera. CI uses it to take pictures of the real
/// screens (see the `screens` job in `.github/workflows/ios.yml`).
///
/// Every name, address and number below is invented.
enum ScreenshotMode {
    static let argument = "-ZestScreen"

    enum Screen: String, CaseIterable {
        case login
        case code
        case today
        /// The same list, scrolled to the end: the rest of the route.
        case today2
        case confirm
        case far
        case record
        case recording
        case review
        case warning
        case saved
        case stopvideo
        case locked
        case queue
        case update
    }

    /// The screen asked for on the command line, if any.
    static var requested: Screen? {
        let arguments = ProcessInfo.processInfo.arguments
        guard let index = arguments.firstIndex(of: argument), index + 1 < arguments.count else { return nil }
        return Screen(rawValue: arguments[index + 1].lowercased())
    }

    /// A small file the app writes once the screen is up and the main thread
    /// is free again. The CI job waits for it before taking the picture, so a
    /// slow first launch gives a late picture rather than a blank one, and a
    /// screen that never comes up is reported as such.
    static var readyMarker: URL {
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSTemporaryDirectory())
        return documents.appendingPathComponent("zest-screen-ready.txt")
    }

    /// Called when the first screen appears. Writes the marker a moment later.
    static func markReady() {
        guard let screen = requested else { return }
        let name = screen.rawValue
        let marker = readyMarker
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) {
            try? FileManager.default.createDirectory(
                at: marker.deletingLastPathComponent(), withIntermediateDirectories: true
            )
            try? Data(name.utf8).write(to: marker, options: .atomic)
        }
    }

    /// Everything the model needs to show one screen.
    struct Shot {
        var route: AppModel.Route
        var tech: TechInfo? = ScreenshotMode.tech
        var today: TodayResponse?
        var target: RecordTarget?
        var draft: Draft?
        var check: CheckState = .idle
        var focus: VideoFocus?
        var recordPhase: AppModel.RecordPhase = .idle
        var seeds = ScreenSeeds()
    }

    @MainActor
    static func show(_ screen: Screen, in app: AppModel) {
        Files.remove(readyMarker)
        let now = Date()
        let today = self.today(now: now)
        var shot = Shot(route: .today, today: today)

        // The same day's worth of videos sits behind every signed-in screen.
        app.queue.loadScreenshotItems(items(now: now, day: today.date))
        app.location.setScreenshotFix(phoneFix)

        switch screen {
        case .login:
            // A made-up mobile number, typed in. No keyboard is raised, so the
            // whole screen shows.
            shot = Shot(route: .login, tech: nil, today: nil)
            shot.seeds.loginContact = "9165550100"
            shot.seeds.autoFocus = false
        case .code:
            shot = Shot(route: .login, tech: nil, today: nil)
            shot.seeds.loginStep = .code
            shot.seeds.loginContact = "9165550100"
            shot.seeds.loginCode = "482"
            shot.seeds.autoFocus = false
        case .today:
            break
        case .today2:
            shot.seeds.scrollToEnd = true
        case .confirm:
            shot.route = .confirm
            shot.target = RecordTarget(stop: jane)
        case .far:
            shot.route = .confirm
            shot.target = RecordTarget(stop: jane)
            shot.seeds.confirmStage = .far
        case .record:
            shot.route = .record
            shot.target = RecordTarget(stop: jane)
            shot.seeds.standInPicture = true
            app.camera.setScreenshotState(status: .ready, recordingStartedAt: nil)
        case .recording:
            shot.route = .record
            shot.target = RecordTarget(stop: jane)
            shot.recordPhase = .recording
            shot.seeds.standInPicture = true
            // Held still 6.2 seconds in: the ring is part-drained around a 9.
            shot.seeds.frozenElapsed = 6.2
            app.camera.setScreenshotState(status: .recording, recordingStartedAt: now)
        case .review:
            shot.route = .review
            shot.draft = draft(now: now, day: today.date)
            shot.check = .done(cleanCheck)
            shot.seeds.standInPicture = true
        case .warning:
            shot.route = .review
            shot.draft = draft(now: now, day: today.date)
            shot.check = .done(warningCheck)
            shot.seeds.reviewStage = .warning
            shot.seeds.standInPicture = true
        case .saved:
            shot.route = .review
            shot.draft = draft(now: now, day: today.date)
            shot.check = .done(cleanCheck)
            shot.seeds.reviewStage = .saved
            shot.seeds.standInPicture = true
        case .stopvideo:
            // Recorded a few minutes ago: still inside the 10-minute window.
            shot.route = .stopVideo
            shot.focus = .stop(susan.customerId)
        case .locked:
            // Recorded 40 minutes ago: it has gone to the customer.
            shot.route = .stopVideo
            shot.focus = .stop(tom.customerId)
        case .queue:
            shot.route = .queue
            fillHeldSpace(for: app.queue)
        case .update:
            shot.route = .updateRequired
        }

        app.loadScreenshot(shot)
    }

    // MARK: - Made-up data

    static let tech = TechInfo(id: 1, firstName: "Sam", demo: true)

    private static func stop(
        _ id: Int, _ name: String, _ last: String, _ address: String, _ lat: Double, _ lng: Double,
        order: Int, service: String, done: Bool = false
    ) -> Stop {
        Stop(
            customerId: id, name: name, lastName: last, address: address, cityLine: "Folsom, CA 95630",
            lat: lat, lng: lng, appointmentIds: [id * 10], services: [service],
            order: order, checkIn: nil, checkOut: nil, done: done, video: nil
        )
    }

    // One stop of each kind, so the pictures show every state a card can be in.
    /// No video yet, and the closest: the suggested stop.
    static let jane = stop(101, "Jane Smith", "Smith", "123 Main St", 38.67800, -121.17600, order: 3, service: "Mowing")
    /// No video yet: the next-door neighbour.
    static let robert = stop(102, "Robert Johnson", "Johnson", "125 Main St", 38.67800, -121.17625, order: 4, service: "Pest Control")
    static let maria = stop(103, "Maria Garcia", "Garcia", "48 Oak Ave", 38.68040, -121.17900, order: 5, service: "Fertilization")
    /// Past its 10 minutes and still sending.
    static let david = stop(104, "David Lee", "Lee", "910 Elm Ct", 38.68900, -121.18800, order: 6, service: "Mowing", done: true)
    /// Past its 10 minutes and waiting for signal.
    static let linda = stop(105, "Linda Brown", "Brown", "77 Pine Way", 38.69500, -121.19500, order: 7, service: "Weed Control", done: true)
    /// A business: no last name, shown as it is.
    static let hoa = stop(106, "Green Valley HOA", "", "5 Park Blvd", 38.70200, -121.20100, order: 8, service: "Irrigation")
    /// Sent 40 minutes ago: gone to the customer, locked.
    static let tom = stop(107, "Tom Wilson", "Wilson", "300 Cedar Ln", 38.66100, -121.16000, order: 1, service: "Mowing", done: true)
    /// Sent a few minutes ago: still inside its 10 minutes.
    static let susan = stop(108, "Susan Clark", "Clark", "12 River Rd", 38.67100, -121.16900, order: 2, service: "Pest Control", done: true)

    /// The phone is standing on Jane Smith's lawn.
    static let phoneFix = VideoLocation(lat: 38.67812, lng: -121.17600, accuracyM: 8)

    private static func today(now: Date) -> TodayResponse {
        var config = AppConfig.fallback
        config.officePhone = "+19165550100"
        return TodayResponse(
            date: ZestDates.dayString(now), serverTime: now, tech: tech, config: config,
            stops: [tom, susan, jane, robert, maria, david, linda, hoa], videos: []
        )
    }

    /// Five videos, one in each state a video can be in.
    private static func items(now: Date, day: String) -> [QueueItem] {
        func item(_ id: String, _ stop: Stop?, secondsAgo: TimeInterval) -> QueueItem {
            QueueItem(
                id: id, stopDate: day, customerId: stop?.customerId, customerName: stop?.listName ?? "Not on my list",
                appointmentIds: stop?.appointmentIds ?? [], notOnMyList: stop == nil,
                recordedAt: now.addingTimeInterval(-secondsAgo), durationSec: 14.6, sizeBytes: 5_000_000,
                location: nil, signals: Signals(spoken: .match, gps: .match, checkIn: .unknown),
                override: false, recommend: false, spokenText: nil, fileName: "\(id).mp4"
            )
        }
        let ticket = UploadTicket(
            url: URL(string: "https://bucket.example.com/video.mp4")!,
            headers: ["Content-Type": "video/mp4"], expiresAt: now.addingTimeInterval(3600)
        )

        // Sent 40 minutes ago: locked.
        let sent = QueueMachine.reduce(
            item("screenshot-sent", tom, secondsAgo: 40 * 60),
            .serverAlreadyHas(state: .sent, emailAt: nil), now: now.addingTimeInterval(-39 * 60)
        )
        // Sent 200 seconds ago: 7 minutes left to change it.
        let changeable = QueueMachine.reduce(
            item("screenshot-changeable", susan, secondsAgo: 200),
            .serverAlreadyHas(state: .held, emailAt: now.addingTimeInterval(400)), now: now.addingTimeInterval(-150)
        )
        // Recorded 15 minutes ago and still going up: SENDING 40%.
        var sending = QueueMachine.reduce(item("screenshot-sending", david, secondsAgo: 15 * 60), .registered(ticket), now: now)
        sending = QueueMachine.reduce(sending, .progress(0.4), now: now)
        // Recorded 25 minutes ago with no signal since: WAITING TO SEND.
        let waiting = item("screenshot-waiting", linda, secondsAgo: 25 * 60)
        // A stop that was not on the list, recorded a minute ago.
        let offList = item("screenshot-offlist", nil, secondsAgo: 60)

        return [sent, changeable, sending, waiting, offList]
    }

    private static func draft(now: Date, day: String) -> Draft {
        Draft(
            id: "screenshot-draft", target: RecordTarget(stop: jane), fileName: "screenshot-draft.mp4",
            stopDate: day, recordedAt: now.addingTimeInterval(-20), durationSec: 14.6, sizeBytes: 5_000_000,
            location: phoneFix, createdAt: now
        )
    }

    private static let cleanCheck = CheckResult(
        signals: Signals(spoken: .match, gps: .match, checkIn: .unknown), clean: true, reasons: [],
        better: nil, transcript: "This is Sam at the Smith home on Main Street."
    )

    /// The tech picked 123 Main St but said the neighbour's name.
    private static let warningCheck: CheckResult = {
        let signals = Signals(spoken: .mismatch, gps: .match, checkIn: .unknown)
        return CheckResult(
            signals: signals, clean: false, reasons: TwoKeys.verdict(signals).reasons,
            better: robert, transcript: "This is Sam at the Johnson home."
        )
    }()

    /// A blank 5 MB file for each unsent video, so "My Videos" shows a real
    /// figure for the space they would be holding.
    @MainActor
    private static func fillHeldSpace(for queue: QueueStore) {
        for item in queue.items where item.needsFile {
            let url = queue.fileURL(for: item)
            if !Files.exists(url) {
                _ = FileManager.default.createFile(atPath: url.path, contents: Data(count: 5 * 1024 * 1024))
            }
        }
    }
}
#endif
