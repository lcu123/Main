import SwiftUI
import ZestCore

/// "Is this the stop?" in very large type, before the camera opens. A video
/// sent to the wrong customer shows one person's home to another, so this
/// screen is never skipped.
struct ConfirmStopView: View {
    @EnvironmentObject private var app: AppModel
    @EnvironmentObject private var location: LocationService

    enum Stage: Equatable {
        case confirm
        case far
        case storageWarn
        case storageBlocked
    }

    @State private var stage: Stage
    @State private var acceptedFar = false
    @State private var acceptedStorage = false

    /// The default is the real flow. Other values only come from `ScreenSeeds`.
    init(stage: Stage = .confirm) {
        _stage = State(initialValue: stage)
    }

    var body: some View {
        if let target = app.target {
            switch stage {
            case .confirm: confirm(target)
            case .far: farWarning(target)
            case .storageWarn: storageWarning
            case .storageBlocked: storageBlocked
            }
        } else {
            Screen(title: "Pick a stop") {
                BodyText("Go back and tap your stop.")
            } footer: {
                BigButton(title: "BACK", kind: .plain) { app.backToToday() }
            }
        }
    }

    // MARK: Stages

    private func confirm(_ target: RecordTarget) -> some View {
        Screen(title: target.notOnMyList ? "Not on my list" : "Is this the stop?") {
            if target.notOnMyList {
                BodyText("Record a video for a stop that is not on your list.", bold: true)
                BodyText("The office will match it to the customer.")
            } else {
                StopHeadline(name: target.name, address: target.address, cityLine: target.cityLine)
                    .padding(.top, 8)
            }
        } footer: {
            BigButton(title: target.notOnMyList ? "RECORD" : "THIS IS IT", kind: .go) {
                proceed()
            }
            BigButton(title: target.notOnMyList ? "BACK" : "WRONG STOP", kind: .plain) {
                app.backToToday()
            }
        }
    }

    private func farWarning(_ target: RecordTarget) -> some View {
        Screen(title: "You look far from this address") {
            StopHeadline(name: target.name, address: target.address, cityLine: target.cityLine)
            Banner(text: "Still right?", tone: .attention)
        } footer: {
            BigButton(title: "YES, STILL RIGHT", kind: .attention) {
                acceptedFar = true
                proceed()
            }
            BigButton(title: "PICK ANOTHER STOP", kind: .plain) {
                app.backToToday()
            }
        }
    }

    private var storageWarning: some View {
        Screen(title: "Your phone is almost full") {
            BodyText("You can still record this video.", bold: true)
            BodyText("Please free up some space on your phone soon.")
        } footer: {
            BigButton(title: "RECORD ANYWAY", kind: .attention) {
                acceptedStorage = true
                proceed()
            }
            BigButton(title: "BACK", kind: .plain) {
                app.backToToday()
            }
        }
    }

    private var storageBlocked: some View {
        Screen(title: "Your phone is full") {
            BodyText("There is no room to record a video.", bold: true)
            BodyText("Delete some photos or apps you do not need, then try again.")
            if app.waitingCount > 0 {
                BodyText("Videos waiting to send will clear by themselves when there is signal.")
            }
        } footer: {
            BigButton(title: "TRY AGAIN", kind: .primary) {
                stage = .confirm
            }
            BigButton(title: "BACK", kind: .plain) {
                app.backToToday()
            }
        }
    }

    // MARK: Guards, in order

    private func proceed() {
        let level = StorageMonitor.level()
        if level == .block {
            app.logStorage(blocked: true)
            stage = .storageBlocked
            return
        }
        if !acceptedFar, isFar {
            app.logFarWarning()
            stage = .far
            return
        }
        if level == .warn, !acceptedStorage {
            app.logStorage(blocked: false)
            stage = .storageWarn
            return
        }
        app.confirmTarget()
    }

    /// Warn, never block: GPS is only good to about one house width.
    private var isFar: Bool {
        guard let target = app.target, !target.notOnMyList, let stop = app.stop(customerId: target.customerId) else { return false }
        return TwoKeys.isFar(fix: location.freshFix(), stop: stop, farMeters: app.config.farMeters) == true
    }
}
