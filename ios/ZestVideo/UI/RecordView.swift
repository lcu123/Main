import SwiftUI
import UIKit
import ZestCore

/// The camera. One giant button starts the clip; it stops by itself.
///
/// While recording there is no way to miss it: a thick red border, a red
/// RECORDING pill, and a big ring that drains away around the seconds left.
struct RecordView: View {
    @EnvironmentObject private var app: AppModel
    @EnvironmentObject private var camera: CameraService
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            switch camera.status {
            case .noCamera:
                problem(
                    title: "Camera not available",
                    text: "This device has no camera. Zest Video needs an iPhone camera to record.",
                    showSettings: false
                )
            case .denied:
                problem(
                    title: "Camera is turned off",
                    text: "Zest Video needs the camera and the microphone. Tap OPEN SETTINGS and turn both on.",
                    showSettings: true
                )
            case .failed(let text):
                problem(title: "Camera problem", text: text, showSettings: false)
            case .idle, .starting, .ready, .recording:
                live
            }
        }
        .statusBarHidden(true)
        .onAppear {
            // Keep the screen awake while the camera is up.
            UIApplication.shared.isIdleTimerDisabled = true
            camera.prepare()
        }
        .onDisappear {
            UIApplication.shared.isIdleTimerDisabled = false
        }
        .onChange(of: scenePhase) { _, phase in
            // Back from a call or the home screen: bring the camera back.
            if phase == .active && app.recordPhase == .idle {
                camera.prepare()
            }
        }
    }

    // MARK: Live camera

    private var isRecording: Bool {
        camera.recordingStartedAt != nil
    }

    private var live: some View {
        ZStack {
            picture

            if let startedAt = camera.recordingStartedAt {
                recordingOverlay(startedAt: startedAt)
            } else if app.recordPhase == .saving {
                savingOverlay
            } else {
                readyOverlay
            }
        }
    }

    /// What the camera sees. (In the simulator's screenshot mode, where there
    /// is no camera, a stand-in picture.)
    @ViewBuilder
    private var picture: some View {
        #if targetEnvironment(simulator)
        if app.seeds.standInPicture {
            StandInPicture()
                .ignoresSafeArea()
                .accessibilityHidden(true)
        } else {
            cameraPreview
        }
        #else
        cameraPreview
        #endif
    }

    private var cameraPreview: some View {
        CameraPreview(session: camera.session) {
            capturePressed()
        }
        .ignoresSafeArea()
        .accessibilityHidden(true)
    }

    /// The screen tap, a volume button, or the ring: one action for all.
    private func capturePressed() {
        if isRecording {
            app.stopRecordingEarly()
        } else {
            app.startRecording()
        }
    }

    // MARK: Before recording

    private var readyOverlay: some View {
        ZStack {
            // Top and bottom furniture.
            VStack(spacing: 12) {
                HStack(alignment: .top, spacing: 12) {
                    flipButton
                    Spacer(minLength: 0)
                }
                if let target = app.target {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(target.name)
                            .font(Theme.heading)
                            .lineLimit(2)
                            .minimumScaleFactor(0.7)
                        if !target.notOnMyList {
                            Text(target.address)
                                .font(Theme.bodyBold)
                                .lineLimit(2)
                                .minimumScaleFactor(0.7)
                        }
                    }
                    .foregroundColor(.white)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(14)
                    .background(RoundedRectangle(cornerRadius: Theme.corner, style: .continuous).fill(Color.black.opacity(0.6)))
                    .accessibilityElement(children: .combine)
                }
                if let problem = app.recordProblem {
                    Banner(text: problem, tone: .attention)
                }
                Spacer(minLength: 0)
                BigButton(title: "BACK", kind: .plain) {
                    app.backToToday()
                }
            }
            .padding(.horizontal, Theme.gutter)
            .padding(.top, 8)
            .padding(.bottom, 14)

            // The one giant button, in the middle of the picture.
            VStack(spacing: 16) {
                Button(action: capturePressed) {
                    ZStack {
                        Circle()
                            .fill(Color.white)
                        Circle()
                            .fill(Theme.recordRed)
                            .padding(12)
                    }
                    .frame(width: 200, height: 200)
                    .shadow(color: .black.opacity(0.5), radius: 8, x: 0, y: 3)
                    .contentShape(Circle())
                }
                .buttonStyle(.plain)
                .disabled(camera.status != .ready || app.recordPhase != .idle)
                .opacity(camera.status == .ready ? 1 : 0.5)
                .accessibilityLabel("Record")
                .accessibilityHint("Starts a \(app.config.maxSeconds) second video. It stops by itself.")

                Text(readyLabel)
                    .font(.system(size: 30, weight: .heavy, design: .rounded))
                    .foregroundColor(.white)
                    .padding(.horizontal, 18)
                    .padding(.vertical, 8)
                    .background(Capsule().fill(Color.black.opacity(0.65)))
                    .accessibilityHidden(true)
            }
        }
    }

    private var readyLabel: String {
        if app.recordPhase != .idle { return "GET READY" }
        return camera.status == .ready ? "TAP TO RECORD" : "STARTING CAMERA"
    }

    private var flipButton: some View {
        Button {
            camera.flip()
        } label: {
            Text("FLIP")
                .font(Theme.button)
                .foregroundColor(.white)
                .frame(minWidth: 120, minHeight: Theme.buttonHeight)
                .padding(.horizontal, 10)
                .background(RoundedRectangle(cornerRadius: Theme.corner, style: .continuous).fill(Color.black.opacity(0.6)))
                .overlay(RoundedRectangle(cornerRadius: Theme.corner, style: .continuous).strokeBorder(Color.white, lineWidth: 3))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(camera.status != .ready)
        .accessibilityLabel(camera.usingFrontCamera ? "Flip to back camera" : "Flip to front camera")
    }

    // MARK: While recording

    private func recordingOverlay(startedAt: Date) -> some View {
        let maxSeconds = Double(app.config.maxSeconds)
        return ZStack {
            // Thick red border round the whole screen.
            Rectangle()
                .strokeBorder(Theme.recordRed, lineWidth: 12)
                .ignoresSafeArea()
                .allowsHitTesting(false)
                .accessibilityHidden(true)

            VStack(spacing: 12) {
                RecordingPill(steady: reduceMotion || app.seeds.frozenElapsed != nil)
                PromptCard(
                    startedAt: startedAt, maxSeconds: maxSeconds, prompts: app.config.prompts,
                    frozenElapsed: app.seeds.frozenElapsed
                )
                Spacer(minLength: 0)
            }
            .padding(.horizontal, Theme.gutter + 6)
            .padding(.top, 18)

            // The ring sits where the record button was. Tapping it ends the
            // clip early (after the first three seconds).
            Button(action: capturePressed) {
                CountdownRing(startedAt: startedAt, maxSeconds: maxSeconds, frozenElapsed: app.seeds.frozenElapsed)
                    .contentShape(Circle())
            }
            .buttonStyle(.plain)
            .accessibilityHint("Stops by itself. Double tap to finish early.")

            Text("Stops by itself. Tap to finish early.")
                .font(Theme.bodyBold)
                .foregroundColor(.white)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 16)
                .padding(.vertical, 8)
                .background(Capsule().fill(Color.black.opacity(0.65)))
                .offset(y: 170)
                .accessibilityHidden(true)
        }
    }

    private var savingOverlay: some View {
        ZStack {
            Color.black.opacity(0.6).ignoresSafeArea()
            VStack(spacing: 18) {
                ProgressView()
                    .progressViewStyle(.circular)
                    .tint(.white)
                    .scaleEffect(2)
                Text("Saving…")
                    .font(Theme.title)
                    .foregroundColor(.white)
            }
        }
    }

    // MARK: No camera / no permission

    private func problem(title: String, text: String, showSettings: Bool) -> some View {
        Screen(title: title) {
            BodyText(text)
        } footer: {
            if showSettings {
                BigButton(title: "OPEN SETTINGS", kind: .primary) {
                    if let url = URL(string: UIApplication.openSettingsURLString) {
                        UIApplication.shared.open(url)
                    }
                }
            }
            BigButton(title: "BACK", kind: .plain) {
                app.backToToday()
            }
        }
    }
}

/// "● RECORDING" on red, with a blinking dot.
private struct RecordingPill: View {
    let steady: Bool

    var body: some View {
        TimelineView(.periodic(from: Date(), by: 0.5)) { context in
            let on = steady || Int(context.date.timeIntervalSinceReferenceDate * 2) % 2 == 0
            HStack(spacing: 12) {
                Circle()
                    .fill(Color.white)
                    .frame(width: 22, height: 22)
                    .opacity(on ? 1 : 0.15)
                Text("RECORDING")
                    .font(.system(size: 30, weight: .heavy, design: .rounded))
                    .foregroundColor(.white)
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 10)
            .background(Capsule().fill(Theme.recordRed))
            .overlay(Capsule().strokeBorder(Color.white, lineWidth: 3))
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Recording")
    }
}

/// The four things to say, one at a time, each for its share of the clip.
private struct PromptCard: View {
    let startedAt: Date
    let maxSeconds: Double
    let prompts: [String]
    var frozenElapsed: TimeInterval?

    var body: some View {
        TimelineView(.periodic(from: startedAt, by: 0.25)) { context in
            let elapsed = frozenElapsed ?? context.date.timeIntervalSince(startedAt)
            let index = Countdown.promptIndex(elapsed: elapsed, maxSeconds: maxSeconds, count: prompts.count)
            VStack(spacing: 4) {
                Text("\(index + 1) of \(prompts.count)")
                    .font(Theme.bodyBold)
                    .foregroundColor(Theme.yellow)
                Text(prompts.isEmpty ? "" : prompts[index])
                    .font(.system(size: 34, weight: .heavy, design: .rounded))
                    .foregroundColor(.white)
                    .multilineTextAlignment(.center)
                    .lineLimit(2)
                    .minimumScaleFactor(0.6)
            }
            .frame(maxWidth: .infinity)
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .background(RoundedRectangle(cornerRadius: Theme.corner, style: .continuous).fill(Color.black.opacity(0.65)))
            .accessibilityElement(children: .combine)
        }
    }
}
