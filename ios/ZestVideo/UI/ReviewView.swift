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
        // `squeeze`: the clip takes whatever height is left, so the name, the
        // whole "I recommended something" row and both buttons always show
        // on first view, on every phone, with nothing below the fold.
        Screen(bar: "Review", title: "Save this video?", squeeze: true) {
            if let draft = app.draft {
                Text(draft.target.name)
                    .zestFont(.rowName)
                    .foregroundColor(Theme.ink)
                    .lineLimit(2)
                    .minimumScaleFactor(0.7)
                    .fixedSize(horizontal: false, vertical: true)
            }
            clip
            if app.config.recommendButton {
                recommendRow
            }
        } footer: {
            BigButton(title: stage == .checking ? "CHECKING…" : "SAVE", kind: .primary, isBusy: stage == .checking) {
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
            // As tall as the room allows, between these two limits.
            .frame(minHeight: Theme.clipMinHeight, idealHeight: Theme.clipMinHeight, maxHeight: Theme.clipHeight)
            .clipShape(RoundedRectangle(cornerRadius: Theme.corner, style: .continuous))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Your video. Double tap to play it again.")
        .accessibilityIdentifier("clip")
    }

    /// "I recommended something": a row with a big switch, blue when it is YES.
    private var recommendRow: some View {
        Button {
            // One press, one change: a double tap must not switch it back off.
            if TapGate.pass() {
                recommend.toggle()
            }
        } label: {
            Card {
                HStack(spacing: 14) {
                    Text("I recommended something")
                        .zestFont(.bodyBold)
                        .foregroundColor(Theme.ink)
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                    BigSwitch(isOn: recommend)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
                .frame(maxWidth: .infinity, minHeight: Theme.buttonHeight, alignment: .leading)
            }
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("I recommended something")
        .accessibilityValue(recommend ? "Yes" : "No")
        .accessibilityAddTraits(.isButton)
        .accessibilityIdentifier("recommend-switch")
    }

    // MARK: Warning

    private var warning: some View {
        let result = currentResult
        return Screen(bar: "Check Stop", title: "Is this the right stop?") {
            if let draft = app.draft {
                // The smaller card: the warning under it has to show too.
                StopHeadline(name: draft.target.name, address: draft.target.address, style: .small)
            }
            ForEach(result?.reasons ?? [], id: \.self) { reason in
                Banner(text: reason, tone: .attention)
            }
            if let better = result?.better {
                BodyText("It sounded like \(better.listName).", bold: true)
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

    /// A light page with one very large green tick: it worked, nothing more to do.
    private var saved: some View {
        let circle = min(Theme.screenWidth * 0.52, 240)
        return ZStack {
            Theme.page.ignoresSafeArea()
            VStack(spacing: 22) {
                ZStack {
                    Circle()
                        .fill(Theme.green)
                    Image(systemName: "checkmark")
                        .font(.system(size: circle * 0.5, weight: .heavy))
                        .foregroundColor(.white)
                }
                .frame(width: circle, height: circle)
                .accessibilityHidden(true)

                Text("Saved")
                    .zestFont(.saved)
                    .foregroundColor(Theme.ink)
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
                Text("It sends by itself.")
                    .zestFont(.heroStreet)
                    .foregroundColor(Theme.softInk)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(Theme.gutter)
        }
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("saved-screen")
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
        Screen(bar: "Pick Stop", onBack: goBack, title: "Pick the right stop") {
            if let better {
                row(better, headline: "It sounded like this one", highlighted: true)
            }
            ForEach(others) { stop in
                row(stop, headline: nil, highlighted: false)
            }
            if better == nil && others.isEmpty {
                Banner(text: "There are no other stops on your list today.", tone: .info)
            }
        } footer: {
            BigButton(title: "BACK", kind: .plain, action: goBack)
        }
    }

    private var suggested: Stop? {
        if case .done(let result) = app.check { return result.better }
        return nil
    }

    /// The arrow in the top bar and the big BACK button do the same thing.
    private func goBack() {
        app.openReview()
    }

    private func row(_ stop: Stop, headline: String?, highlighted: Bool) -> some View {
        StopCard(
            stop: stop,
            state: RowState(badge: .number(app.routeNumber(of: stop)), text: "PICK THIS STOP", tone: .action),
            headline: headline,
            distanceMeters: nil,
            highlighted: highlighted
        ) {
            app.retargetDraft(to: stop)
        }
    }
}

/// A large on/off switch, drawn to match the rest of the app: blue with
/// "YES" when on, grey with "NO" when off. It only shows the state; the whole
/// row it sits in is the button.
struct BigSwitch: View {
    let isOn: Bool

    var body: some View {
        let knob: CGFloat = 46
        ZStack {
            Capsule()
                .fill(isOn ? Theme.blue : Theme.hairline)
            HStack(spacing: 0) {
                if isOn {
                    word
                    knobView(knob)
                } else {
                    knobView(knob)
                    word
                }
            }
            .padding(4)
        }
        .frame(width: 112, height: knob + 8)
        .accessibilityHidden(true)
    }

    private var word: some View {
        Text(isOn ? "YES" : "NO")
            .font(.system(size: Theme.size(.pill), weight: .bold))
            .foregroundColor(isOn ? Color.white : Theme.ink)
            .lineLimit(1)
            .minimumScaleFactor(0.7)
            .frame(maxWidth: .infinity)
    }

    private func knobView(_ size: CGFloat) -> some View {
        Circle()
            .fill(Color.white)
            .shadow(color: Color.black.opacity(0.25), radius: 2, x: 0, y: 1)
            .frame(width: size, height: size)
    }
}
