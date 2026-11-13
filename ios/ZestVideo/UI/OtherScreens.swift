import SwiftUI

/// Shown once, after the first saved video: the offer of leave reminders.
/// Only if the tech says yes is the phone asked for "Always" location.
struct RemindersOfferView: View {
    @EnvironmentObject private var app: AppModel

    var body: some View {
        Screen(title: "Want a reminder?") {
            BodyText("Zest Video can remind you if you leave a stop with no video.", bold: true)
            BodyText("To do that, your phone will ask about your location. Pick \u{201C}Change to Always Allow\u{201D}.")
            BodyText("The app works fine without it.", color: Theme.softInk)
        } footer: {
            BigButton(title: "TURN ON REMINDERS", kind: .primary) {
                app.answerRemindersOffer(turnOn: true)
            }
            BigButton(title: "NOT NOW", kind: .plain) {
                app.answerRemindersOffer(turnOn: false)
            }
        }
    }
}

/// The server needs a newer version of the app.
struct UpdateRequiredView: View {
    @EnvironmentObject private var app: AppModel

    var body: some View {
        Screen(title: "Please update Zest Video") {
            BodyText("This version is too old to send videos.", bold: true)
            if app.appStoreURL == nil {
                BodyText("Update Zest Video the same way you installed it, or call the office.")
            } else {
                BodyText("Tap UPDATE NOW, then tap Update.")
            }
            if app.waitingCount > 0 {
                Banner(text: "Videos on your phone are safe. They send after you update.", tone: .info)
            }
        } footer: {
            if app.appStoreURL != nil {
                BigButton(title: "UPDATE NOW", kind: .primary) {
                    app.openAppStore()
                }
            }
            if app.canCallOffice {
                BigButton(title: "CALL OFFICE", kind: .plain) {
                    app.callOffice()
                }
            }
        }
    }
}
