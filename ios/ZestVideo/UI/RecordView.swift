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
    /// Grows with the phone's text-size setting, so the room kept for the
    /// words around the ring grows with them.
    @ScaledMetric(relativeTo: .body) private var typeScale: CGFloat = 1

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
        // The second tap of a double tap (on THIS IS IT, or RECORD AGAIN)
        // must not land on the record button.
        .settleFirst()
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

            if isRecording {
                // Thick red border round the whole screen.
                Rectangle()
                    .strokeBorder(Theme.recordRed, lineWidth: 12)
                    .ignoresSafeArea()
                    .allowsHitTesting(false)
                    .accessibilityHidden(true)
            }

            // The ring and the record button are sized from the room there is.
            GeometryReader { geometry in
                Group {
                    if let startedAt = camera.recordingStartedAt {
                        recordingOverlay(startedAt: startedAt, size: geometry.size)
                    } else if app.recordPhase == .saving {
                        savingOverlay
                    } else {
                        readyOverlay(size: geometry.size)
                    }
                }
                .frame(width: geometry.size.width, height: geometry.size.height)
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

    /// The ring and the record button share one size: about two thirds of
    /// the screen's width, or less when the screen is too short for that.
    private func controlDiameter(in size: CGSize, reserved: CGFloat) -> CGFloat {
        max(120, min(size.width * Theme.ringShare, size.height - reserved))
    }

    private func readyOverlay(size: CGSize) -> some View {
        // A short screen puts FLIP and the name on one row to leave room.
        let short = size.height < 640
        let reserved = (short ? 360 : 470) * Theme.scale * typeScale + (app.recordProblem == nil ? 0 : 100)
        let diameter = controlDiameter(in: size, reserved: reserved)

        return VStack(spacing: 12) {
            if short {
                HStack(alignment: .center, spacing: 12) {
                    flipButton
                    nameCard(withAddress: false)
                }
            } else {
                HStack(alignment: .top, spacing: 12) {
                    flipButton
                    Spacer(minLength: 0)
                }
                nameCard(withAddress: true)
            }
            if let problem = app.recordProblem {
                Banner(text: problem, tone: .attention)
            }

            Spacer(minLength: 4)

            // The one giant button, in the middle of the picture.
            Button(action: capturePressed) {
                ZStack {
                    Circle()
                        .fill(Color.white)
                    Circle()
                        .fill(Theme.recordRed)
                        .padding(diameter * 0.06)
                }
                .frame(width: diameter, height: diameter)
                .shadow(color: .black.opacity(0.5), radius: 8, x: 0, y: 3)
                .contentShape(Circle())
            }
            .buttonStyle(.plain)
            .disabled(camera.status != .ready || app.recordPhase != .idle)
            .opacity(camera.status == .ready ? 1 : 0.5)
            .accessibilityLabel("Record")
            .accessibilityHint("Starts a \(app.config.maxSeconds) second video. It stops by itself.")
            .accessibilityIdentifier("record-button")

            Text(readyLabel)
                .zestFont(.recordLabel)
                .foregroundColor(.white)
                .lineLimit(1)
                .minimumScaleFactor(0.6)
                .padding(.horizontal, 20)
                .padding(.vertical, 8)
                .background(Capsule().fill(Color.black.opacity(0.65)))
                .accessibilityHidden(true)

            Spacer(minLength: 4)

            BigButton(title: "BACK", kind: .plain) {
                app.backToToday()
            }
        }
        .padding(.horizontal, Theme.gutter)
        .padding(.top, 8)
        .padding(.bottom, 14)
    }

    @ViewBuilder
    private func nameCard(withAddress: Bool) -> some View {
        if let target = app.target {
            VStack(alignment: .leading, spacing: 4) {
                Text(target.name)
                    .zestFont(.rowName)
                    .lineLimit(2)
                    .minimumScaleFactor(0.6)
                if withAddress && !target.notOnMyList {
                    Text(target.address)
                        .zestFont(.rowStreet)
                        .lineLimit(2)
                        .minimumScaleFactor(0.7)
                }
            }
            .foregroundColor(.white)
            .frame(maxWidth: .infinity, minHeight: withAddress ? 0 : Theme.buttonHeight, alignment: .leading)
            .padding(.horizontal, 14)
            .padding(.vertical, withAddress ? 14 : 4)
            .background(RoundedRectangle(cornerRadius: Theme.corner, style: .continuous).fill(Color.black.opacity(0.6)))
            .accessibilityElement(children: .combine)
        }
    }

    private var readyLabel: String {
        if app.recordPhase != .idle { return "GET READY" }
        return camera.status == .ready ? "TAP TO RECORD" : "STARTING CAMERA"
    }

    private var flipButton: some View {
        Button {
            // One press, one flip.
            if TapGate.pass() {
                camera.flip()
            }
        } label: {
            // White with blue words, like every other secondary button.
            Text("FLIP")
                .tracking(0.5)
                .zestFont(.button)
                .foregroundColor(Theme.blue)
                .lineLimit(1)
                .frame(minWidth: 120, minHeight: Theme.buttonHeight)
                .padding(.horizontal, 10)
                .background(RoundedRectangle(cornerRadius: Theme.corner, style: .continuous).fill(Theme.card))
                .overlay(RoundedRectangle(cornerRadius: Theme.corner, style: .continuous).strokeBorder(Theme.blue, lineWidth: 2))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(camera.status != .ready)
        .accessibilityLabel(camera.usingFrontCamera ? "Flip to back camera" : "Flip to front camera")
        .accessibilityIdentifier("flip-button")
    }

    // MARK: While recording

    private func recordingOverlay(startedAt: Date, size: CGSize) -> some View {
        let maxSeconds = Double(app.config.maxSeconds)
        let diameter = controlDiameter(in: size, reserved: 380 * Theme.scale * typeScale)
        return VStack(spacing: 12) {
            RecordingPill(steady: reduceMotion || app.seeds.frozenElapsed != nil)
            PromptCard(
                startedAt: startedAt, maxSeconds: maxSeconds, prompts: app.config.prompts,
                frozenElapsed: app.seeds.frozenElapsed
            )

            Spacer(minLength: 4)

            // The ring takes the record button's place and size. Tapping
            // it ends the clip early (after the first three seconds).
            Button(action: capturePressed) {
                CountdownRing(
                    startedAt: startedAt, maxSeconds: maxSeconds, diameter: diameter,
                    lineWidth: Theme.ringStroke, frozenElapsed: app.seeds.frozenElapsed
                )
                .contentShape(Circle())
            }
            .buttonStyle(.plain)
            .accessibilityHint("Stops by itself. Double tap to finish early.")
            .accessibilityIdentifier("countdown-ring")

            Text("Stops by itself. Tap to finish early.")
                .zestFont(.bodyBold)
                .foregroundColor(.white)
                .multilineTextAlignment(.center)
                .lineLimit(2)
                .minimumScaleFactor(0.7)
                .padding(.horizontal, 18)
                .padding(.vertical, 8)
                .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(Color.black.opacity(0.65)))
                .accessibilityHidden(true)

            Spacer(minLength: 4)
        }
        .padding(.horizontal, Theme.gutter + 6)
        .padding(.top, 18)
        .padding(.bottom, 14)
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
                    .zestFont(.title)
                    .foregroundColor(.white)
            }
        }
    }

    // MARK: No camera / no permission

    private func problem(title: String, text: String, showSettings: Bool) -> some View {
        Screen(bar: "Record", onBack: { app.backToToday() }, title: title) {
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
                    .frame(width: 26, height: 26)
                    .opacity(on ? 1 : 0.15)
                Text("RECORDING")
                    .zestFont(.recordLabel)
                    .foregroundColor(.white)
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
            }
            .padding(.horizontal, 22)
            .padding(.vertical, 10)
            .background(Capsule().fill(Theme.recordRed))
            .overlay(Capsule().strokeBorder(Color.white, lineWidth: 3))
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Recording")
        .accessibilityIdentifier("recording-pill")
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
                    .zestFont(.bodyBold)
                    .foregroundColor(Theme.ringYellow)
                Text(prompts.isEmpty ? "" : prompts[index])
                    .zestFont(.prompt)
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
