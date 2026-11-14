#if targetEnvironment(simulator)
import Foundation
import UserNotifications
import ZestCore

/// UI-test mode. **Simulator only**: this whole file is compiled out of any
/// build that can run on a real phone, so it cannot be reached there.
///
/// Launched with `-ZestUITest 1`, the app is the real app (real sign-in, real
/// stores on disk, the real upload manager and queue, real navigation) with
/// these differences, and only these:
///
/// 1. It talks to a test server on this Mac (`ZEST_TEST_SERVER` in the launch
///    environment) instead of the live one.
/// 2. The camera is a stand-in recorder (the simulator has none). It writes a
///    real, playable video file where the camera would have, at once, and
///    finishes through the same code as the real camera.
/// 3. Speech-to-text does not run (the simulator's is unreliable and asks a
///    permission question). A test can supply the words it would have heard
///    with `ZEST_TEST_TRANSCRIPT`; without it the spoken key is "unknown".
/// 4. Uploads go through an ordinary URLSession, not a background one. The
///    upload manager's code is the same line for line; only the session's
///    configuration differs. So the queue, the retries and the recovery
///    after a kill are really tested. What is NOT tested this way is iOS
///    carrying an upload on while the app is closed: that needs the
///    background session (`ZEST_TEST_BACKGROUND_UPLOADS=1` turns it on for
///    the one test that looks at it) and, to be believed, a real phone.
/// 5. With the background session on, iOS is not asked to start the app by
///    itself when an upload finishes. Started that way it would have no
///    launch arguments, so it would be the ordinary app talking to the real
///    server in the middle of a test.
/// 6. Every upload task's start and end (with the error, if any) is posted
///    to the test server, so a failed test's report says why.
/// 7. With `ZEST_TEST_METRICS=1` (the performance tests only) the app also
///    posts a line of numbers a second after each screen change: its memory
///    footprint, what it holds on disk, and the time since it started.
/// 8. `ZEST_TEST_FREE_MB` says how much free space the phone "has", so the
///    almost-full and full screens can be reached without filling a disk.
///    The check that reads it is the real one.
/// 9. `ZEST_TEST_DRAFT_AGE_DAYS` makes the unsaved clip found at launch that
///    many days old, as if the phone had been put away with it. What the
///    app then does with an old clip is the real code.
/// 10. `ZEST_TEST_INTERRUPT_AT` (seconds) has the stand-in recorder
///    interrupted that long into the first clip of the launch, the way the
///    phone interrupts the camera when a call rings: the clip ends there,
///    the camera is away for `ZEST_TEST_INTERRUPT_FOR` seconds (3 unless
///    said), then comes back by itself. `ZEST_TEST_INTERRUPT_AS` names what
///    did it (`call_or_alarm` unless said; the values of `RecordingCut`). The
///    stand-in also behaves like the camera when the app leaves the screen:
///    a clip being recorded ends there, and the camera comes back when the
///    app does. What the app makes of the clip, what it says and where it
///    goes next is the real code. What is NOT tested this way is iOS itself
///    telling the app about a real call: that needs a real phone.
/// 11. `ZEST_TEST_SILENT_CAMERA=1` makes the stand-in recorder never report
///    that a recording is over, so the app's own watch for that can be seen
///    to end it.
///
/// `-ZestUITestReset 1` wipes everything the app has stored first, so a test
/// starts from a clean install. With `ZEST_TEST_TOKEN` in the environment
/// that clean install starts already signed in with that device token (the
/// test server issued it), so only the sign-in tests have to type a number
/// and a code. A relaunch WITHOUT the reset keeps everything, which is how
/// the tests prove nothing is lost when the app is killed.
enum UITestMode {
    static let argument = "-ZestUITest"
    static let resetArgument = "-ZestUITestReset"
    static let serverVariable = "ZEST_TEST_SERVER"
    static let transcriptVariable = "ZEST_TEST_TRANSCRIPT"
    static let tokenVariable = "ZEST_TEST_TOKEN"
    static let backgroundUploadsVariable = "ZEST_TEST_BACKGROUND_UPLOADS"
    static let metricsVariable = "ZEST_TEST_METRICS"
    static let freeSpaceVariable = "ZEST_TEST_FREE_MB"
    static let draftAgeVariable = "ZEST_TEST_DRAFT_AGE_DAYS"
    static let interruptAtVariable = "ZEST_TEST_INTERRUPT_AT"
    static let interruptForVariable = "ZEST_TEST_INTERRUPT_FOR"
    static let interruptAsVariable = "ZEST_TEST_INTERRUPT_AS"
    static let silentCameraVariable = "ZEST_TEST_SILENT_CAMERA"

    static var isOn: Bool { flag(argument) }

    static var wantsReset: Bool { isOn && flag(resetArgument) }

    /// The test server to talk to, when in UI-test mode.
    static var serverURL: URL? {
        guard isOn else { return nil }
        let raw = ProcessInfo.processInfo.environment[serverVariable] ?? "http://localhost:8765"
        return URL(string: raw)
    }

    /// The words the speech check "heard", if the test supplied any.
    static var transcript: String? {
        guard isOn else { return nil }
        let raw = ProcessInfo.processInfo.environment[transcriptVariable] ?? ""
        return raw.isEmpty ? nil : raw
    }

    /// Use the real background URLSession for uploads (one test asks for it).
    static var backgroundUploads: Bool {
        isOn && ProcessInfo.processInfo.environment[backgroundUploadsVariable] == "1"
    }

    /// The free space the phone is to "have", in bytes, if the test said.
    static var freeBytes: Int64? {
        guard isOn, let megabytes = Int64(ProcessInfo.processInfo.environment[freeSpaceVariable] ?? "") else { return nil }
        return megabytes * 1024 * 1024
    }

    /// How many days old the unsaved clip found at launch is to be made.
    static var draftAgeDays: Int? {
        guard isOn, let days = Int(ProcessInfo.processInfo.environment[draftAgeVariable] ?? ""), days > 0 else { return nil }
        return days
    }

    /// The interruption a test asked for: how far into the first clip, how
    /// long the camera then stays away, and what took it.
    static var interruption: (at: TimeInterval, lasting: TimeInterval, cut: RecordingCut)? {
        let environment = ProcessInfo.processInfo.environment
        guard isOn, let at = TimeInterval(environment[interruptAtVariable] ?? ""), at >= 0 else { return nil }
        let lasting = TimeInterval(environment[interruptForVariable] ?? "") ?? 3
        let cut = RecordingCut(rawValue: environment[interruptAsVariable] ?? "") ?? .callOrAlarm
        return (at, max(0.2, lasting), cut)
    }

    /// The stand-in camera is to say nothing when a recording should be
    /// over: not at the time limit, not when the ring is tapped. The app
    /// must notice and end the recording itself.
    static var silentCamera: Bool {
        isOn && ProcessInfo.processInfo.environment[silentCameraVariable] == "1"
    }

    /// The same clip, filmed `days` days earlier. (Its stop's check-in
    /// times move with it, as they would have been on that day.)
    static func aged(_ draft: Draft, days: Int) -> Draft {
        let back = TimeInterval(-days * 86_400)
        var old = draft
        old.recordedAt = draft.recordedAt.addingTimeInterval(back)
        old.createdAt = draft.createdAt.addingTimeInterval(back)
        old.stopDate = ZestDates.dayString(
            (ZestDates.parseISO(draft.stopDate + "T12:00:00Z") ?? draft.recordedAt).addingTimeInterval(back),
            timeZone: TimeZone(identifier: "UTC") ?? .current
        )
        if var stop = old.target.stop {
            stop.checkIn = stop.checkIn?.addingTimeInterval(back)
            stop.checkOut = stop.checkOut?.addingTimeInterval(back)
            old.target.stop = stop
        }
        return old
    }

    /// Tells the test server something worth reading in a failure report:
    /// an upload task was made, started, or ended, and with what error.
    static func note(_ text: String) {
        guard isOn, let base = serverURL, let url = URL(string: "/__test/note", relativeTo: base) else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["note": text])
        request.timeoutInterval = 10
        URLSession.shared.dataTask(with: request).resume()
    }

    // MARK: Numbers for the performance tests

    /// The performance tests asked for numbers.
    static var wantsMetrics: Bool {
        isOn && ProcessInfo.processInfo.environment[metricsVariable] == "1"
    }

    /// When the app's own code first ran in this launch. (The time iOS takes
    /// to load the app before that is measured by the test, from outside.)
    static let codeStarted = Date()

    /// Called first thing, so `codeStarted` is as early as it can be.
    static func markCodeStart() {
        _ = codeStarted
    }

    /// The app's memory footprint in bytes: the figure iOS itself goes by
    /// when it decides an app is using too much. 0 if it cannot be read.
    static func footprintBytes() -> UInt64 {
        #if canImport(Darwin)
        var info = task_vm_info_data_t()
        var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<integer_t>.size)
        let result = withUnsafeMutablePointer(to: &info) { pointer in
            pointer.withMemoryRebound(to: integer_t.self, capacity: Int(count)) { rebound in
                task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), rebound, &count)
            }
        }
        guard result == KERN_SUCCESS else { return 0 }
        return UInt64(info.phys_footprint)
        #else
        return 0
        #endif
    }

    /// Every byte under a folder, however deep.
    static func bytes(under folder: URL) -> Int64 {
        guard let walker = FileManager.default.enumerator(at: folder, includingPropertiesForKeys: [.fileSizeKey]) else { return 0 }
        var total: Int64 = 0
        for case let url as URL in walker {
            total += Files.size(of: url)
        }
        return total
    }

    /// Posts one line of numbers to the test server.
    @MainActor
    static func metric(_ name: String) {
        guard wantsMetrics else { return }
        let milliseconds = Int(Date().timeIntervalSince(codeStarted) * 1000)
        let megabytes = (Double(footprintBytes()) / 1_048_576 * 10).rounded() / 10
        let videoFiles = Files.fileNames(in: Files.videos).count
        let videoBytes = Files.totalBytes(in: Files.videos)
        let storedBytes = bytes(under: Files.root)
        let tempBytes = bytes(under: URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true))
        note(
            "metric \(name) ms=\(milliseconds) memoryMB=\(megabytes) videoFiles=\(videoFiles) videoBytes=\(videoBytes) "
                + "storedBytes=\(storedBytes) tempBytes=\(tempBytes)"
        )
    }

    /// A screen changed: take the numbers once the new one has settled.
    @MainActor
    static func routeChanged(to route: String) {
        guard wantsMetrics else { return }
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            metric("route=\(route)")
        }
    }

    @MainActor private static var saidRouteUsable = false

    /// My Route came up with stops on it: say how long that took, once.
    @MainActor
    static func routeUsable(stops: Int) {
        guard wantsMetrics, stops > 0, !saidRouteUsable else { return }
        saidRouteUsable = true
        metric("routeUsable stops=\(stops)")
    }

    private static func flag(_ name: String) -> Bool {
        let arguments = ProcessInfo.processInfo.arguments
        guard let index = arguments.firstIndex(of: name), index + 1 < arguments.count else { return false }
        return arguments[index + 1] == "1"
    }

    /// Wipes the app's own storage. Called once, from the one place the app
    /// model is made, before anything is read from disk.
    static func resetIfAsked() {
        guard wantsReset else { return }

        // The stores: route cache, queue, draft, recording marker, events, videos.
        Files.remove(Files.root)
        Files.prepare(Files.root)
        Files.prepare(Files.videos)

        // Flags and the cached tech.
        if let domain = Bundle.main.bundleIdentifier {
            UserDefaults.standard.removePersistentDomain(forName: domain)
        }
        // The device token.
        Keychain.remove(Keychain.deviceTokenKey)

        let center = UNUserNotificationCenter.current()
        center.removeAllPendingNotificationRequests()
        center.removeAllDeliveredNotifications()

        // Start signed in, if the test handed over a token from its server.
        let token = ProcessInfo.processInfo.environment[tokenVariable] ?? ""
        if !token.isEmpty {
            Keychain.set(token, for: Keychain.deviceTokenKey)
        }
    }
}
#endif
