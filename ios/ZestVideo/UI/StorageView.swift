import SwiftUI
import ZestCore

/// On the way to the camera: "Your phone is almost full" (under 500 MB free,
/// he can still record) or "Your phone is full" (under 200 MB, he cannot).
///
/// These are the techs' own phones, so every way into the camera passes the
/// same check (`AppModel.enterCamera`): THIS IS IT, and RECORD AGAIN from
/// either place it is offered. Nothing is deleted on the way here, so BACK
/// always returns to what was there before.
struct StorageView: View {
    @EnvironmentObject private var app: AppModel

    var body: some View {
        switch app.storageStop {
        case .warn: almostFull
        case .blocked, .none: full
        }
    }

    private var almostFull: some View {
        Screen(bar: "Storage", onBack: { app.leaveStorage() }, title: "Your phone is almost full") {
            BodyText("You can still record this video.", bold: true)
            BodyText("Please free up some space on your phone soon.")
        } footer: {
            BigButton(title: "RECORD ANYWAY", kind: .attention) {
                app.recordAnyway()
            }
            BigButton(title: "BACK", kind: .plain) {
                app.leaveStorage()
            }
        }
    }

    private var full: some View {
        Screen(bar: "Storage", onBack: { app.leaveStorage() }, title: "Your phone is full") {
            BodyText("There is no room to record a video.", bold: true)
            BodyText("Delete some photos or apps you do not need, then try again.")
            if app.waitingCount > 0 {
                BodyText("Videos waiting to send will clear by themselves when there is signal.")
            }
        } footer: {
            BigButton(title: "TRY AGAIN", kind: .primary) {
                app.tryStorageAgain()
            }
            BigButton(title: "BACK", kind: .plain) {
                app.leaveStorage()
            }
        }
    }
}
