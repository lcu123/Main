import UIKit
import UserNotifications

/// The few things SwiftUI's app lifecycle does not cover: background upload
/// wake-ups, the push token, and notifications.
///
/// Pushes from the server are ordinary visible alerts. The app declares no
/// background mode and handles no silent pushes: a tapped alert simply opens
/// the app, which then gets the queue moving.
final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        #if targetEnvironment(simulator)
        // `-ZestScreen <name>`: open one screen with made-up data for a
        // picture. This branch does not exist in a build for a real phone.
        if let screen = ScreenshotMode.requested {
            ScreenshotMode.show(screen, in: AppModel.shared)
            return true
        }
        #endif
        // Also runs when iOS starts the app in the background (a finished
        // upload, a left stop), so the queue and the route are ready.
        AppModel.shared.start()
        return true
    }

    /// iOS relaunched or woke the app because background uploads finished.
    func application(
        _ application: UIApplication,
        handleEventsForBackgroundURLSession identifier: String,
        completionHandler: @escaping () -> Void
    ) {
        AppModel.shared.start()
        AppModel.shared.uploads.attachBackgroundCompletion(completionHandler)
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        let hex = deviceToken.map { String(format: "%02x", $0) }.joined()
        AppModel.shared.pushTokenReceived(hex)
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        // No push token (the simulator, or push not set up yet). The app's own
        // local reminders still work.
    }

    // MARK: UNUserNotificationCenterDelegate

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        Task { @MainActor in
            AppModel.shared.nudged()
        }
        completionHandler()
    }
}
