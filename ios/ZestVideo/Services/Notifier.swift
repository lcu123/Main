import Foundation
import UIKit
import UserNotifications

/// Nudges made on the phone itself, so they work with no signal.
enum Notifier {
    private static let waitingID = "zest.waiting"
    private static let draftID = "zest.draft"
    private static let leavePrefix = "zest.leave."

    static func isAllowed() async -> Bool {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        return settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional
    }

    /// Shows the system question. Asked after the first video is saved, never at launch.
    @discardableResult
    static func requestPermission() async -> Bool {
        let granted = (try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])) ?? false
        if granted {
            await registerForPush()
        }
        return granted
    }

    /// Asks Apple for a push token when notifications are already allowed.
    @MainActor
    static func registerForPush() {
        UIApplication.shared.registerForRemoteNotifications()
    }

    static func registerForPushIfAllowed() async {
        if await isAllowed() {
            await registerForPush()
        }
    }

    // MARK: Videos stuck on the phone

    /// One reminder 30 minutes from now, unless one is already set.
    static func scheduleWaitingNudge(count: Int) {
        UNUserNotificationCenter.current().getPendingNotificationRequests { pending in
            guard !pending.contains(where: { $0.identifier == waitingID }) else { return }
            let content = UNMutableNotificationContent()
            content.title = count == 1 ? "1 video is still on your phone" : "\(count) videos are still on your phone"
            content.body = "Open Zest Video so they can send."
            content.sound = .default
            let trigger = UNTimeIntervalNotificationTrigger(timeInterval: 30 * 60, repeats: false)
            let request = UNNotificationRequest(identifier: waitingID, content: content, trigger: trigger)
            UNUserNotificationCenter.current().add(request, withCompletionHandler: nil)
        }
    }

    static func cancelWaitingNudge() {
        UNUserNotificationCenter.current().removePendingNotificationRequests(withIdentifiers: [waitingID])
        UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: [waitingID])
    }

    // MARK: Recorded but not saved

    static func scheduleDraftNudge(name: String) {
        let center = UNUserNotificationCenter.current()
        center.removePendingNotificationRequests(withIdentifiers: [draftID])
        let content = UNMutableNotificationContent()
        content.title = "1 video not saved yet"
        content.body = "Open Zest Video and tap SAVE for \(name)."
        content.sound = .default
        let trigger = UNTimeIntervalNotificationTrigger(timeInterval: 10 * 60, repeats: false)
        center.add(UNNotificationRequest(identifier: draftID, content: content, trigger: trigger), withCompletionHandler: nil)
    }

    static func cancelDraftNudge() {
        UNUserNotificationCenter.current().removePendingNotificationRequests(withIdentifiers: [draftID])
        UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: [draftID])
    }

    // MARK: Left a stop without a video

    static func leaveReminder(name: String, customerId: Int) {
        let content = UNMutableNotificationContent()
        content.title = "Did you record a video?"
        content.body = "No video yet for \(name)."
        content.sound = .default
        let request = UNNotificationRequest(identifier: leavePrefix + String(customerId), content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request, withCompletionHandler: nil)
    }
}
