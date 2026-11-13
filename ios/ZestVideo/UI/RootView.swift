import SwiftUI

/// Shows the one screen the router points at. No navigation stack: every way
/// back is a big button, never a swipe.
struct RootView: View {
    @EnvironmentObject private var app: AppModel

    var body: some View {
        ZStack {
            Theme.background.ignoresSafeArea()
            screen
        }
        // Respect the phone's text size, within limits that keep every layout whole.
        .dynamicTypeSize(DynamicTypeSize.large ... DynamicTypeSize.accessibility1)
        .preferredColorScheme(.light)
        .tint(Theme.purple)
        .task {
            // The first "became active" can arrive before this view exists.
            app.becameActive()
        }
    }

    @ViewBuilder
    private var screen: some View {
        switch app.route {
        case .login:
            LoginView()
        case .today:
            TodayView()
        case .confirm:
            ConfirmStopView()
        case .record:
            RecordView()
        case .review:
            ReviewView()
        case .pickStop:
            PickStopView()
        case .stopVideo:
            StopVideoView()
        case .queue:
            QueueView()
        case .remindersOffer:
            RemindersOfferView()
        case .updateRequired:
            UpdateRequiredView()
        }
    }
}
