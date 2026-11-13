#if targetEnvironment(simulator)
import Foundation
import UserNotifications

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
