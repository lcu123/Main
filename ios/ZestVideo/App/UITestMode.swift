#if targetEnvironment(simulator)
import Foundation
import UserNotifications

/// UI-test mode. **Simulator only**: this whole file is compiled out of any
/// build that can run on a real phone, so it cannot be reached there.
///
/// Launched with `-ZestUITest 1`, the app is the real app (real sign-in, real
/// stores on disk, real upload manager with its real background URLSession,
/// real navigation) with four differences, and only these four:
///
/// 1. It talks to a test server on this Mac (`ZEST_TEST_SERVER` in the launch
///    environment) instead of the live one.
/// 2. The camera is a stand-in recorder (the simulator has none). It writes a
///    real, playable video file where the camera would have, at once, and
///    finishes through the same code as the real camera.
/// 3. Speech-to-text does not run (the simulator's is unreliable and asks a
///    permission question). A test can supply the words it would have heard
///    with `ZEST_TEST_TRANSCRIPT`; without it the spoken key is "unknown".
/// 4. iOS is not asked to start the app by itself when a background upload
///    finishes. Started that way it would have no launch arguments, so it
///    would be the ordinary app talking to the real server. A test starts
///    the app again itself; the upload carries on in the system meanwhile.
///
/// `-ZestUITestReset 1` wipes everything the app has stored first, so a test
/// starts from a clean install. A relaunch WITHOUT it keeps everything, which
/// is how the tests prove nothing is lost when the app is killed.
enum UITestMode {
    static let argument = "-ZestUITest"
    static let resetArgument = "-ZestUITestReset"
    static let serverVariable = "ZEST_TEST_SERVER"
    static let transcriptVariable = "ZEST_TEST_TRANSCRIPT"

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
    }
}
#endif
