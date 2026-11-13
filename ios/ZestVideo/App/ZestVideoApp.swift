import SwiftUI

@main
struct ZestVideoApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @ObservedObject private var app = AppModel.shared
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(app)
                .environmentObject(app.queue)
                .environmentObject(app.camera)
                .environmentObject(app.location)
        }
        .onChange(of: scenePhase) { _, phase in
            switch phase {
            case .active:
                app.becameActive()
            case .background:
                app.wentToBackground()
            default:
                break
            }
        }
    }
}
