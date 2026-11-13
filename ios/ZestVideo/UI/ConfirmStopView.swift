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
    }

    @State private var stage: Stage
    @State private var acceptedFar = false

    /// The default is the real flow. Other values only come from `ScreenSeeds`.
    init(stage: Stage = .confirm) {
        _stage = State(initialValue: stage)
    }

    var body: some View {
        if let target = app.target {
            switch stage {
            case .confirm: confirm(target)
            case .far: farWarning(target)
            }
        } else {
            Screen(bar: "Confirm Stop", onBack: { app.backToToday() }, title: "Pick a stop") {
                BodyText("Go back and tap your stop.")
            } footer: {
                BigButton(title: "BACK", kind: .plain) { app.backToToday() }
            }
        }
    }

    // MARK: Stages

    private func confirm(_ target: RecordTarget) -> some View {
        Screen(
            bar: target.notOnMyList ? "Not On My List" : "Confirm Stop",
            onBack: { app.backToToday() },
            title: target.notOnMyList ? "Not on my list" : "Is this the stop?"
        ) {
            if target.notOnMyList {
                Card {
                    VStack(alignment: .leading, spacing: 10) {
                        BodyText("Record a video for a stop that is not on your list.", bold: true)
                        BodyText("The office will match it to the customer.", color: Theme.softInk)
                    }
                    .padding(20)
                }
            } else {
                StopHeadline(name: target.name, address: target.address, cityLine: target.cityLine)
            }
        } footer: {
            BigButton(title: target.notOnMyList ? "RECORD" : "THIS IS IT", kind: .primary) {
                proceed()
            }
            BigButton(title: target.notOnMyList ? "BACK" : "WRONG STOP", kind: .plain) {
                app.backToToday()
            }
        }
    }

    private func farWarning(_ target: RecordTarget) -> some View {
        Screen(bar: "Confirm Stop", onBack: { app.backToToday() }, title: "You look far from this address") {
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

    // MARK: Guards, in order

    private func proceed() {
        // A full phone is said first: no point asking "still right?" when
        // nothing can be recorded. Otherwise the far-away question comes
        // before the camera, and the free-space check (which every way into
        // the camera shares, see `AppModel.enterCamera`) comes last.
        if !acceptedFar, isFar, !app.phoneIsFull {
            app.logFarWarning()
            stage = .far
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
