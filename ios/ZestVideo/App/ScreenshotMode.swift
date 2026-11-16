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
/// `-ZestFixture odd` swaps the usual made-up route for one of awkward
/// customers (apostrophes, accents, hyphens, a very long business name, all
/// capitals, an emoji, a one-word name, a unit, a PO box), and
/// `-ZestStop <n>` picks which of them the screen is about. That is how CI
/// checks that long and unusual names wrap or shrink without being cut off.
///
/// Every name, address and number below is invented.
enum ScreenshotMode {
    static let argument = "-ZestScreen"
    static let fixtureArgument = "-ZestFixture"
    static let stopArgument = "-ZestStop"

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
        /// A stop a teammate already recorded, seen by a tech who is only
        /// helping there: covered, and he can still record his own.
        case team
        case queue
        case update
        /// "Your phone is almost full", on the way to the camera.
        case lowspace
        /// "Your phone is full": no recording.
        case full
        /// "Save this video?" for a clip left unsaved since yesterday.
        case olddraft
    }

    /// The screen asked for on the command line, if any.
    static var requested: Screen? {
        value(after: argument).flatMap { Screen(rawValue: $0.lowercased()) }
    }

    /// The route of awkward customers was asked for.
    static var wantsOddNames: Bool {
        value(after: fixtureArgument)?.lowercased() == "odd"
    }

    /// Which customer of that route the screen is about (0 is the first).
    static var stopIndex: Int {
        max(0, value(after: stopArgument).flatMap { Int($0) } ?? 0)
    }

    private static func value(after name: String) -> String? {
        let arguments = ProcessInfo.processInfo.arguments
        guard let index = arguments.firstIndex(of: name), index + 1 < arguments.count else { return nil }
        return arguments[index + 1]
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
        var storageStop: AppModel.StorageStop?
        var recordPhase: AppModel.RecordPhase = .idle
        var seeds = ScreenSeeds()
    }

    /// Which made-up customer plays which part in the pictures.
    struct Cast {
        /// The whole route, in route order.
        var stops: [Stop]
        /// No video yet, and the closest: the stop being recorded.
        var target: Stop
        /// The stop the words fit better, on the warning screen.
        var neighbour: Stop
        /// Sent 40 minutes ago: gone to the customer.
        var locked: Stop
        /// Sent a few minutes ago: still inside its 10 minutes.
        var changeable: Stop
        /// Past its 10 minutes and still sending.
        var sending: Stop
        /// Past its 10 minutes and waiting for signal.
        var waiting: Stop
        /// Recorded by a teammate; this tech is only helping there.
        var team: Stop
        /// Where the phone is: on the target's lawn.
        var phone: VideoLocation
    }

    /// The usual eight customers.
    static let usualCast = Cast(
        stops: [tom, susan, jane, robert, maria, david, linda, hoa],
        target: jane, neighbour: robert, locked: tom, changeable: susan, sending: david, waiting: linda,
        team: maria, phone: phoneFix
    )

    /// The awkward customers, with number `index` in the part the screen
    /// shows largest: the stop with a video on the two video screens, the
    /// stop being recorded everywhere else.
    static func oddCast(for screen: Screen, index: Int) -> Cast {
        let stops = oddStops
        func at(_ offset: Int) -> Stop { stops[(index + offset) % stops.count] }
        var cast = Cast(
            stops: stops, target: at(0), neighbour: at(1), locked: at(2), changeable: at(3),
            sending: at(4), waiting: at(5), team: at(6), phone: phoneFix
        )
        switch screen {
        case .stopvideo:
            cast.changeable = at(0)
            cast.target = at(3)
        case .locked:
            cast.locked = at(0)
            cast.target = at(2)
        case .team:
            cast.team = at(0)
            cast.target = at(6)
        default:
            break
        }
        if let lat = cast.target.lat, let lng = cast.target.lng {
            cast.phone = VideoLocation(lat: lat + 0.00012, lng: lng, accuracyM: 8)
        }
        return cast
    }

    @MainActor
    static func show(_ screen: Screen, in app: AppModel) {
        Files.remove(readyMarker)
        let now = Date()
        let cast = wantsOddNames ? oddCast(for: screen, index: stopIndex) : usualCast
        let target = cast.target
        let today = self.today(now: now, stops: cast.stops)
        var shot = Shot(route: .today, today: today)

        // The same day's worth of videos sits behind every signed-in screen.
        app.queue.loadScreenshotItems(items(now: now, day: today.date, cast: cast))
        app.location.setScreenshotFix(cast.phone)

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
            shot.target = RecordTarget(stop: target)
        case .far:
            shot.route = .confirm
            shot.target = RecordTarget(stop: target)
            shot.seeds.confirmStage = .far
        case .record:
            shot.route = .record
            shot.target = RecordTarget(stop: target)
            shot.seeds.standInPicture = true
            app.camera.setScreenshotState(status: .ready, recordingStartedAt: nil)
        case .recording:
            shot.route = .record
            shot.target = RecordTarget(stop: target)
            shot.recordPhase = .recording
            shot.seeds.standInPicture = true
            // Held still 2.2 seconds in: the first thing to say ("Hi Jane")
            // is up, and the ring has begun to drain around a 13.
            shot.seeds.frozenElapsed = 2.2
            app.camera.setScreenshotState(status: .recording, recordingStartedAt: now)
        case .review:
            shot.route = .review
            shot.draft = draft(now: now, day: today.date, cast: cast)
            shot.check = .done(cleanCheck)
            shot.seeds.standInPicture = true
        case .warning:
            shot.route = .review
            shot.draft = draft(now: now, day: today.date, cast: cast)
            shot.check = .done(warningCheck(better: cast.neighbour))
            shot.seeds.reviewStage = .warning
            shot.seeds.standInPicture = true
        case .saved:
            shot.route = .review
            shot.draft = draft(now: now, day: today.date, cast: cast)
            shot.check = .done(cleanCheck)
            shot.seeds.reviewStage = .saved
            shot.seeds.standInPicture = true
        case .stopvideo:
            // Recorded a few minutes ago: still inside the 10-minute window.
            shot.route = .stopVideo
            shot.focus = .stop(cast.changeable.customerId)
        case .locked:
            // Recorded 40 minutes ago: it has gone to the customer.
            shot.route = .stopVideo
            shot.focus = .stop(cast.locked.customerId)
        case .team:
            // A teammate recorded this stop 30 minutes ago and it went out.
            // This tech has no appointment of his own there, so the server
            // shows him that video as not his.
            var day = today
            if let index = day.stops.firstIndex(where: { $0.customerId == cast.team.customerId }) {
                day.stops[index].video = StopVideo(id: "screenshot-team", state: .sent, byYou: false)
            }
            shot.today = day
            shot.route = .stopVideo
            shot.focus = .stop(cast.team.customerId)
        case .queue:
            shot.route = .queue
            fillHeldSpace(for: app.queue)
        case .update:
            shot.route = .updateRequired
        case .lowspace:
            shot.route = .storage
            shot.target = RecordTarget(stop: target)
            shot.storageStop = .warn
        case .full:
            shot.route = .storage
            shot.target = RecordTarget(stop: target)
            shot.storageStop = .blocked
        case .olddraft:
            // Recorded yesterday and never saved: the draft stays, and the
            // choice is SAVE or DELETE VIDEO.
            var old = draft(now: now, day: today.date, cast: cast)
            old.stopDate = ZestDates.dayString(now.addingTimeInterval(-86_400))
            old.recordedAt = now.addingTimeInterval(-86_400)
            old.createdAt = old.recordedAt
            shot.route = .review
            shot.draft = old
            shot.check = .done(cleanCheck)
            shot.seeds.standInPicture = true
        }

        // The made-up day is built around this moment, and stays at it: a
        // card that says "7 MIN LEFT" says so until the app is closed.
        shot.seeds.frozenNow = now
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

    // The awkward customers. Every one is made up.
    static let oddStops: [Stop] = {
        let rows: [(String, String, String, String)] = [
            ("Sean O'Brien", "O'Brien", "220 Saint Andrews Dr", "Mowing"),
            ("José Peña", "Peña", "8 Cañada Verde Rd", "Pest Control"),
            ("Anne-Marie McDonald-Smith", "McDonald-Smith", "1200 Long Winding Meadow Creek Boulevard Apt 14B", "Fertilization"),
            ("Pieter van der Berg", "van der Berg", "77 5th Ave Unit 3", "Weed Control"),
            // A business, 45 characters long.
            ("Greater Riverbend Homeowners Association Inc.", "", "4500 Hwy 50", "Irrigation"),
            ("MARGARET THORNWOOD-HIGHTOWER", "THORNWOOD-HIGHTOWER", "14 St Marys Ct", "Mowing"),
            ("Kai 🌻 Rivers", "Rivers", "31 Mt Shasta Way", "Pest Control"),
            ("Prince", "", "9 Elm St", "Mowing"),
            ("Lee Wong", "Wong", "PO Box 1432", "Rodent Exclusion"),
            // One word of 24 letters: it cannot wrap, only shrink.
            ("Maximiliano Wolfeschlegelsteinhausen", "Wolfeschlegelsteinhausen", "14 Elm St", "Lawn Fertilization and Weed Control"),
        ]
        return rows.enumerated().map { index, row in
            Stop(
                customerId: 201 + index, name: row.0, lastName: row.1, address: row.2, cityLine: "Sampletown, CA 90000",
                lat: 38.67800 + Double(index) * 0.0020, lng: -121.17600 - Double(index) * 0.0015,
                appointmentIds: [(201 + index) * 10], services: [row.3],
                order: index + 1, checkIn: nil, checkOut: nil, done: false, video: nil
            )
        }
    }()

    private static func today(now: Date, stops: [Stop]) -> TodayResponse {
        var config = AppConfig.fallback
        config.officePhone = "+19165550100"
        return TodayResponse(
            date: ZestDates.dayString(now), serverTime: now, tech: tech, config: config,
            stops: stops, videos: []
        )
    }

    /// Five videos, one in each state a video can be in.
    private static func items(now: Date, day: String, cast: Cast) -> [QueueItem] {
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
            item("screenshot-sent", cast.locked, secondsAgo: 40 * 60),
            .serverAlreadyHas(state: .sent, emailAt: nil), now: now.addingTimeInterval(-39 * 60)
        )
        // Sent 200 seconds ago: 7 minutes left to change it.
        let changeable = QueueMachine.reduce(
            item("screenshot-changeable", cast.changeable, secondsAgo: 200),
            .serverAlreadyHas(state: .held, emailAt: now.addingTimeInterval(400)), now: now.addingTimeInterval(-150)
        )
        // Recorded 15 minutes ago and still going up: SENDING 40%.
        var sending = QueueMachine.reduce(item("screenshot-sending", cast.sending, secondsAgo: 15 * 60), .registered(ticket), now: now)
        sending = QueueMachine.reduce(sending, .progress(0.4), now: now)
        // Recorded 25 minutes ago with no signal since: WAITING TO SEND.
        let waiting = item("screenshot-waiting", cast.waiting, secondsAgo: 25 * 60)
        // A stop that was not on the list, recorded a minute ago: it has
        // gone to the office to be matched (no email is on its way).
        let offList = QueueMachine.reduce(
            item("screenshot-offlist", nil, secondsAgo: 60),
            .serverAlreadyHas(state: .unmatched, emailAt: nil), now: now.addingTimeInterval(-30)
        )

        return [sent, changeable, sending, waiting, offList]
    }

    private static func draft(now: Date, day: String, cast: Cast) -> Draft {
        Draft(
            id: "screenshot-draft", target: RecordTarget(stop: cast.target), fileName: "screenshot-draft.mp4",
            stopDate: day, recordedAt: now.addingTimeInterval(-20), durationSec: 14.6, sizeBytes: 5_000_000,
            location: cast.phone, createdAt: now
        )
    }

    private static let cleanCheck = CheckResult(
        signals: Signals(spoken: .match, gps: .match, checkIn: .unknown), clean: true, reasons: [],
        better: nil, transcript: "Hi Jane. Here's what I did. Here's what I recommend. I closed the backyard gate."
    )

    /// The tech picked one stop but said "Hi" to the neighbour by name.
    private static func warningCheck(better: Stop) -> CheckResult {
        let signals = Signals(spoken: .mismatch, gps: .match, checkIn: .unknown)
        return CheckResult(
            signals: signals, clean: false, reasons: TwoKeys.verdict(signals).reasons,
            better: better, transcript: PromptText.fill("Hi {name}. Here's what I did.", name: better.greetingName),
            spokenMiss: .otherStop
        )
    }

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
