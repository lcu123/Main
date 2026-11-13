import AVFoundation
import SwiftUI
import ZestCore

/// One question: "Save this video?"
///
/// The clip plays back by itself. SAVE is the only thing the tech has to tap:
/// the video is queued on the phone and sends by itself. While the clip
/// plays, the two-keys check runs; if it does not pass, one warning is shown
/// here and the tech fixes it himself.
struct ReviewView: View {
    @EnvironmentObject private var app: AppModel

    enum Stage: Equatable {
        case ask
        case checking
        case warning
        case saved
    }

    @State private var stage: Stage
    @State private var player: AVPlayer?
    @State private var ended = false
    @State private var recommend: Bool

    private let finished = NotificationCenter.default.publisher(for: .AVPlayerItemDidPlayToEndTime)

    /// The defaults are the real flow. Other values only come from `ScreenSeeds`.
    init(stage: Stage = .ask, recommend: Bool = false) {
        _stage = State(initialValue: stage)
        _recommend = State(initialValue: recommend)
    }

    var body: some View {
        Group {
            switch stage {
            case .ask, .checking: ask
            case .warning: warning
            case .saved: saved
            }
        }
        .onAppear(perform: startPlayer)
        .onDisappear(perform: stopPlayer)
        .onReceive(finished) { _ in
            ended = true
        }
        .onChange(of: app.check) { _, newValue in
            // SAVE was tapped before the check finished: carry on now.
            if stage == .checking, case .done = newValue {
                save(override: false)
            }
        }
    }

    // MARK: Ask

    private var ask: some View {
        Screen(title: "Save this video?") {
            if let draft = app.draft {
                Text(draft.target.name)
                    .zestFont(.rowName)
                    .foregroundColor(Theme.ink)
                    .lineLimit(2)
                    .minimumScaleFactor(0.7)
            }
            clip
            if app.config.recommendButton {
                recommendRow
            }
        } footer: {
            BigButton(title: stage == .checking ? "CHECKING…" : "SAVE", kind: .go, isBusy: stage == .checking) {
                save(override: false)
            }
            BigButton(title: "RECORD AGAIN", kind: .plain) {
                stopPlayer()
                app.recordAgainFromReview()
            }
        }
    }

    /// The clip. Tapping it plays it again; there are no small controls.
    private var clip: some View {
        Button(action: playAgain) {
            ZStack {
                Color.black
                #if targetEnvironment(simulator)
                if app.seeds.standInPicture {
                    StandInPicture()
                } else if let player {
                    ClipPlayerView(player: player)
                }
                #else
                if let player {
                    ClipPlayerView(player: player)
                }
                #endif
                if ended {
                    Text("TAP TO PLAY AGAIN")
                        .zestFont(.bodyBold)
                        .foregroundColor(.white)
                        .padding(.horizontal, 16)
                        .padding(.vertical, 10)
                        .background(Capsule().fill(Color.black.opacity(0.7)))
                        .overlay(Capsule().strokeBorder(Color.white, lineWidth: 2))
                }
            }
            .frame(maxWidth: .infinity)
            .frame(height: Theme.clipHeight)
            .clipShape(RoundedRectangle(cornerRadius: Theme.corner, style: .continuous))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Your video. Double tap to play it again.")
    }

    private var recommendRow: some View {
        Button {
            recommend.toggle()
        } label: {
            HStack(spacing: 14) {
                Text(recommend ? "YES" : "NO")
                    .zestFont(.bodyBold)
                    .foregroundColor(Theme.black)
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
                    .frame(width: 88, height: 60)
                    .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(recommend ? Theme.lime : Theme.white))
                    .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Theme.black, lineWidth: 3))
                Text("I recommended something")
                    .zestFont(.bodyBold)
                    .foregroundColor(Theme.ink)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }
            .frame(maxWidth: .infinity, minHeight: Theme.buttonHeight, alignment: .leading)
            .padding(.horizontal, 14)
            .background(RoundedRectangle(cornerRadius: Theme.corner, style: .continuous).fill(Theme.card))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("I recommended something")
        .accessibilityValue(recommend ? "Yes" : "No")
        .accessibilityAddTraits(.isButton)
    }

    // MARK: Warning

    private var warning: some View {
        let result = currentResult
        return Screen(title: "Is this the right stop?") {
            if let draft = app.draft {
                StopHeadline(name: draft.target.name, address: draft.target.address)
            }
            ForEach(result?.reasons ?? [], id: \.self) { reason in
                Banner(text: reason, tone: .attention)
            }
            if let better = result?.better {
                BodyText("It sounded like \(better.name).", bold: true)
            }
        } footer: {
            BigButton(title: "SAVE ANYWAY", kind: .attention) {
                save(override: true)
            }
            BigButton(title: "PICK THE RIGHT STOP", kind: .plain) {
                stopPlayer()
                app.showStopPicker()
            }
            BigButton(title: "RECORD AGAIN", kind: .plain) {
                stopPlayer()
                app.recordAgainFromReview()
            }
        }
    }

    private var currentResult: CheckResult? {
        if case .done(let result) = app.check { return result }
        return nil
    }

    // MARK: Saved

    private var saved: some View {
        ZStack {
            Theme.lime.ignoresSafeArea()
            VStack(spacing: 18) {
                Text("SAVED ✓")
                    .zestFont(.saved)
                    .foregroundColor(Theme.black)
                    .minimumScaleFactor(0.6)
                    .lineLimit(1)
                Text("It sends by itself.")
                    .zestFont(.title)
                    .foregroundColor(Theme.black)
                    .multilineTextAlignment(.center)
            }
            .padding(Theme.gutter)
        }
        .accessibilityElement(children: .combine)
    }

    // MARK: Actions

    private func save(override: Bool) {
        guard stage != .saved else { return }
        guard let result = currentResult else {
            // The check is still listening (six seconds at most).
            stage = .checking
            return
        }
        if !result.clean && !override {
            stage = .warning
            return
        }
        guard app.saveDraft(recommend: recommend, override: override && !result.clean) else { return }
        stopPlayer()
        stage = .saved
        Task {
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            app.finishSave()
        }
    }

    private func startPlayer() {
        guard player == nil, !app.seeds.standInPicture, let draft = app.draft else { return }
        Sounds.preparePlayback()
        let item = AVPlayerItem(url: Files.video(draft.fileName))
        let newPlayer = AVPlayer(playerItem: item)
        newPlayer.actionAtItemEnd = .pause
        player = newPlayer
        ended = false
        newPlayer.play()
    }

    private func playAgain() {
        guard let player else { return }
        ended = false
        player.seek(to: .zero)
        player.play()
    }

    /// Lets go of the player (and the file) as soon as the screen is done.
    private func stopPlayer() {
        player?.pause()
        player?.replaceCurrentItem(with: nil)
        player = nil
    }
}

/// PICK THE RIGHT STOP: the same clip, moved to another customer.
struct PickStopView: View {
    @EnvironmentObject private var app: AppModel

    var body: some View {
        let better = suggested
        let others = app.stops.filter { stop in
            stop.customerId != app.draft?.target.customerId && stop.customerId != better?.customerId
        }
        Screen(title: "Pick the right stop") {
            if let better {
                Text("IT SOUNDED LIKE THIS ONE")
                    .zestFont(.bodyBold)
                    .foregroundColor(Theme.purple)
                row(better, highlighted: true)
            }
            ForEach(others) { stop in
                row(stop, highlighted: false)
            }
            if better == nil && others.isEmpty {
                BodyText("There are no other stops on your list today.")
            }
        } footer: {
            BigButton(title: "BACK", kind: .plain) {
                app.openReview()
            }
        }
    }

    private var suggested: Stop? {
        if case .done(let result) = app.check { return result.better }
        return nil
    }

    private func row(_ stop: Stop, highlighted: Bool) -> some View {
        StopCard(
            entry: StopRanker.RankedStop(stop: stop, distanceMeters: nil),
            status: app.rowStatus(for: stop),
            highlighted: highlighted,
            hint: nil
        ) {
            app.retargetDraft(to: stop)
        }
    }
}
