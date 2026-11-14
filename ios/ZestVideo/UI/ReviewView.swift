import AVFoundation
import SwiftUI
import ZestCore

/// One question: "Save this video?"
///
/// The clip plays back by itself. SAVE is the only thing the tech has to tap:
/// the video is queued on the phone and sends by itself. While the clip
/// plays, the two-keys check runs; if it does not pass, one warning is shown
/// here and the tech fixes it himself.
///
/// The draft stays: a clip left unsaved is still here the next day. Then the
/// screen says which day it was filmed and the choice is SAVE or DELETE
/// VIDEO, since he is not at that stop any more to record it again.
struct ReviewView: View {
    @EnvironmentObject private var app: AppModel
    @Environment(\.dynamicTypeSize) private var typeSize

    enum Stage: Equatable {
        case ask
        case checking
        case warning
        case saved
        /// "Delete this video?", for a clip from an earlier day.
        case confirmDelete
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
            case .confirmDelete: deleteQuestion
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
        // `squeeze`: the clip takes whatever height is left, so with an
        // ordinary name the name, the whole "I recommended something" row
        // and both buttons show on first view. The name is never what gives
        // way: a long one takes the lines it needs at its full size (held
        // to two, a 45-letter business was drawn at about 90% on the
        // smallest phone), the clip goes down to its smallest, and then the
        // page scrolls.
        Screen(bar: "Review", title: "Save this video?", squeeze: true) {
            // A call (or the like) ended the clip before its time: said
            // once, plainly. The question stays the same: save it, or
            // record again.
            if let note = app.draftCutNote {
                Banner(text: note, tone: .attention)
                    .accessibilityElement(children: .combine)
                    .accessibilityIdentifier("cut-note")
            }
            if let draft = app.draft {
                Text(draft.target.name)
                    .zestFont(.rowName)
                    .foregroundColor(Theme.ink)
                    .customerText()
                // A clip from an earlier day says which day.
                if let filmed = app.draftFilmedText {
                    Text(filmed)
                        .zestFont(.bodyBold)
                        .foregroundColor(Theme.ink)
                        .lineLimit(2)
                        .minimumScaleFactor(0.7)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("draft-filmed")
                }
            }
            clip
            if app.config.recommendButton {
                recommendRow
            }
        } footer: {
            BigButton(title: stage == .checking ? "CHECKING…" : "SAVE", kind: .primary, isBusy: stage == .checking) {
                save(override: false)
            }
            if filmedToday {
                BigButton(title: "RECORD AGAIN", kind: .plain) {
                    stopPlayer()
                    app.recordAgainFromReview()
                }
            } else {
                deleteButton
            }
        }
    }

    /// RECORD AGAIN only makes sense at the stop, on the day.
    private var filmedToday: Bool {
        DraftRule.canRecordAgain(app.draftStanding)
    }

    private var deleteButton: some View {
        BigButton(title: "DELETE VIDEO", kind: .dark) {
            stage = .confirmDelete
        }
    }

    // MARK: Delete (a clip from an earlier day)

    private var deleteQuestion: some View {
        Screen(bar: "Delete Video", title: "Delete this video?") {
            if let draft = app.draft {
                StopHeadline(name: draft.target.name, address: draft.target.address, style: .tight)
            }
            if let filmed = app.draftFilmedText {
                BodyText(filmed, bold: true)
            }
            BodyText("The customer will not get a video for this visit.")
        } footer: {
            BigButton(title: "YES, DELETE", kind: .dark) {
                stopPlayer()
                app.deleteDraft()
            }
            BigButton(title: "NO, KEEP IT", kind: .plain) {
                stage = .ask
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
            // With the phone's text at its largest sizes this page has to
            // be scrolled above its three buttons. Then why the app is
            // asking comes first, so it is never the part out of sight.
            if LargeType.isOn(typeSize) {
                warningReasons(result)
                warningStop
            } else {
                warningStop
                warningReasons(result)
            }
        } footer: {
            BigButton(title: "SAVE ANYWAY", kind: .attention) {
                save(override: true)
            }
            if filmedToday {
                BigButton(title: "PICK THE RIGHT STOP", kind: .plain) {
                    stopPlayer()
                    app.showStopPicker()
                }
                BigButton(title: "RECORD AGAIN", kind: .plain) {
                    stopPlayer()
                    app.recordAgainFromReview()
                }
            } else {
                // Another day's list is gone: there is no other stop to
                // pick and nothing to record again.
                deleteButton
            }
        }
    }

    /// The stop that was picked, and the day for a clip from an earlier one.
    @ViewBuilder
    private var warningStop: some View {
        if let draft = app.draft {
            // The smaller card: the warning has to show too.
            StopHeadline(name: draft.target.name, address: draft.target.address, style: .small)
        }
        if let filmed = app.draftFilmedText {
            BodyText(filmed, bold: true)
        }
    }

    /// Why the app is asking, and the stop the words fit better.
    @ViewBuilder
    private func warningReasons(_ result: CheckResult?) -> some View {
        ForEach(result?.reasons ?? [], id: \.self) { reason in
            Banner(text: reason, tone: .attention)
        }
        if let better = result?.better {
            BodyText("It sounded like \(better.listName).", bold: true)
        } else if result?.spokenMiss == .otherStop {
            // Another customer's name was said, but two of them share it.
            BodyText("It sounded like another customer.", bold: true)
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

/// A large on/off switch, drawn to match the rest of the app: deep blue with
/// "YES" and the knob on the right when on, dark grey with "NO" and the knob
/// on the left when off. The word and the knob are white on both, so each
/// reads against its track in bright sun (see `Theme.switchOn`). It only
/// shows the state; the whole row it sits in is the button.
struct BigSwitch: View {
    let isOn: Bool

    var body: some View {
        let knob: CGFloat = 46
        ZStack {
            Capsule()
                .fill(isOn ? Theme.switchOn : Theme.switchOff)
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
            .foregroundColor(Theme.white)
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
