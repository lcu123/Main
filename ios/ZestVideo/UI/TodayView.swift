import SwiftUI
import ZestCore

/// "My Route": today's stops, laid out like the route list the techs already
/// use. A header card with the tech's name and the day's count, then one card
/// per stop. The app's best guess sits on top (checked in, or nearest), then
/// the next two, then the rest of the route.
struct TodayView: View {
    @EnvironmentObject private var app: AppModel
    @EnvironmentObject private var queue: QueueStore
    @EnvironmentObject private var location: LocationService

    var body: some View {
        let ranked = app.ranked
        Screen(
            bar: "My Route",
            barDetail: app.today.flatMap { ZestDates.longDay($0.date) },
            tab: .route,
            startAtBottom: app.seeds.scrollToEnd
        ) {
            // A card says how many minutes are left to change a video, so
            // the list looks at the clock again every little while.
            TimelineView(.periodic(from: Date(), by: 20)) { context in
                // Small gaps at the top, so the first stop sits high on the
                // screen; the stop cards keep their own, wider gaps.
                VStack(alignment: .leading, spacing: 10) {
                    header
                    banners

                    if ranked.all.isEmpty {
                        emptyState
                    } else {
                        if !ranked.suggested.isEmpty {
                            SmallLabel("Tap your stop")
                                .padding(.top, 2)
                            VStack(spacing: 18) {
                                ForEach(Array(ranked.suggested.enumerated()), id: \.element.id) { index, entry in
                                    card(entry, headline: headline(for: entry.stop, first: index == 0, reason: ranked.reason),
                                         highlighted: index == 0, now: app.seeds.frozenNow ?? context.date)
                                }
                            }
                        }
                        if !ranked.rest.isEmpty {
                            SmallLabel("Rest of today")
                                .padding(.top, 12)
                            VStack(spacing: 18) {
                                ForEach(ranked.rest) { entry in
                                    card(entry, headline: headline(for: entry.stop, first: false, reason: ranked.reason),
                                         highlighted: false, now: app.seeds.frozenNow ?? context.date)
                                }
                            }
                        }
                    }

                    notOnMyList
                        .padding(.top, 8)
                }
            }
        }
        #if targetEnvironment(simulator)
        .onAppear {
            // Performance tests only: say when the route could first be used.
            UITestMode.routeUsable(stops: app.stops.count)
        }
        #endif
        .task {
            // Keep the list fresh while it is on screen. Stops with the view.
            while !Task.isCancelled {
                // With no route yet, look again soon; otherwise every two minutes.
                let wait: UInt64 = app.today == nil ? 15 : 120
                try? await Task.sleep(nanoseconds: wait * 1_000_000_000)
                if Task.isCancelled { break }
                await app.refreshToday()
                location.refresh(precise: false)
            }
        }
    }

    // MARK: Header

    /// The tech's own card, kept low so the first stop shows high on the
    /// screen: a round badge with his initial, his first name and the day's
    /// two numbers on one row, and a blue bar along the bottom edge.
    private var header: some View {
        let name = app.tech?.firstName ?? ""
        let shown = name.isEmpty ? "Today" : name
        let total = app.stops.count
        return Card {
            VStack(spacing: 0) {
                HStack(alignment: .center, spacing: 12) {
                    ZStack {
                        Circle()
                            .fill(Theme.blue)
                        Text(PersonName.initial(name))
                            .font(.system(size: Theme.headerBadgeSize * 0.5, weight: .bold))
                            .foregroundColor(.white)
                    }
                    .frame(width: Theme.headerBadgeSize, height: Theme.headerBadgeSize)
                    .accessibilityHidden(true)

                    // The name beside the two numbers when it fits (a short
                    // name at 32 pt, a longer one at 26 pt); a long name
                    // goes on its own line above them. Never shrunk further.
                    ViewThatFits(in: .horizontal) {
                        HStack(alignment: .center, spacing: 12) {
                            headerName(shown, role: .rowName)
                            Spacer(minLength: 4)
                            stats(total: total)
                        }
                        HStack(alignment: .center, spacing: 12) {
                            headerName(shown, role: .bodyBold)
                            Spacer(minLength: 4)
                            stats(total: total)
                        }
                        VStack(alignment: .leading, spacing: 4) {
                            headerName(shown, role: .rowName)
                            stats(total: total)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 14)
                .frame(maxWidth: .infinity, alignment: .leading)

                Rectangle()
                    .fill(Theme.blue)
                    .frame(height: 6)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(shown). Sent \(app.videosSentCount) of \(total). Waiting \(app.waitingCount).")
        .accessibilityIdentifier("route-header")
    }

    private func headerName(_ name: String, role: Theme.TextRole) -> some View {
        Text(name)
            .zestFont(role)
            .foregroundColor(Theme.ink)
            .lineLimit(1)
            .fixedSize()
    }

    /// The day's two numbers, side by side, each under a one-line label so
    /// the numbers sit on the same line.
    private func stats(total: Int) -> some View {
        HStack(alignment: .top, spacing: 16) {
            stat("Sent", "\(app.videosSentCount)/\(total)")
            stat("Waiting", "\(app.waitingCount)")
        }
        .fixedSize()
    }

    private func stat(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(label.uppercased())
                .tracking(1)
                .zestFont(.label)
                .foregroundColor(Theme.softInk)
                .lineLimit(1)
            Text(value)
                .zestFont(.stat)
                .foregroundColor(Theme.ink)
                .lineLimit(1)
        }
    }

    // MARK: Cards

    private func card(_ entry: StopRanker.RankedStop, headline: String?, highlighted: Bool, now: Date) -> some View {
        StopCard(
            stop: entry.stop,
            state: app.rowState(for: entry.stop, now: now),
            headline: headline,
            distanceMeters: entry.distanceMeters,
            highlighted: highlighted
        ) {
            app.open(stop: entry.stop)
        }
    }

    /// The blue line at the top of the first card: why it is first.
    ///
    /// "Checked in" is only said of the stop the list has put first for
    /// that reason, which it does only when the check-in is believable now
    /// (`StopRanker.believesOpenCheckIn`). A stop left checked in for hours
    /// while the tech is at another house carries no such line.
    private func headline(for stop: Stop, first: Bool, reason: StopRanker.Reason) -> String? {
        guard first else { return nil }
        switch reason {
        case .checkedIn: return "Checked in"
        case .nearest: return "Closest stop"
        // No location, and he completed this one within the hour without
        // filming it: he is most likely still standing there.
        case .justFinished: return "Just finished"
        case .routeOrder: return "Next stop"
        }
    }

    @ViewBuilder
    private var banners: some View {
        if app.draft != nil {
            Button {
                app.openReview()
            } label: {
                Card(tint: Theme.paleAmber) {
                    VStack(spacing: 0) {
                        Text("1 video not saved yet")
                            .zestFont(.rowName)
                            .foregroundColor(Theme.ink)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(18)
                        Hairline()
                        ActionRow(text: "FINISH IT", tone: .waiting)
                    }
                }
            }
            .buttonStyle(.plain)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("1 video not saved yet. Finish it.")
            .accessibilityAddTraits(.isButton)
            .accessibilityIdentifier("draft-card")
        }
        if let notice = app.notice {
            Banner(text: notice, tone: .attention)
        }
        // While a video is still on the phone, deleting the app would lose
        // it: the same line says so.
        if let waiting = KeepApp.waitingBanner(waiting: app.waitingCount) {
            Button {
                app.showQueue()
            } label: {
                Banner(text: waiting, tone: .attention)
            }
            .buttonStyle(.plain)
            .accessibilityHint("Opens My Videos")
            .accessibilityIdentifier("waiting-banner")
        }
        if app.offline {
            Banner(text: app.today == nil ? "No signal. Your stops will show when there is signal." : "No signal. Showing your saved list.", tone: .info)
        }
    }

    @ViewBuilder
    private var emptyState: some View {
        if app.today == nil && !app.offline {
            // Still asking. Any failure sets `offline`, so this never sticks.
            HStack(spacing: 14) {
                ProgressView()
                    .progressViewStyle(.circular)
                    .scaleEffect(1.4)
                    .accessibilityHidden(true)
                BodyText("Getting your stops…", bold: true)
            }
            .padding(.vertical, 8)
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("loading-route")
        } else if app.today == nil {
            BodyText("Your stops are not here yet.", bold: true)
            BodyText("You can still record. Tap STOP NOT ON MY LIST.")
        } else {
            BodyText("No stops on your list today.", bold: true)
            BodyText("To record a stop anyway, tap STOP NOT ON MY LIST.")
        }
    }

    /// The last card: for an add-on or a re-service that is not on the route.
    private var notOnMyList: some View {
        Button {
            app.openNotOnMyList()
        } label: {
            Card {
                ActionRow(text: "STOP NOT ON MY LIST", tone: .action)
            }
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Stop not on my list")
        .accessibilityIdentifier("not-on-my-list")
    }
}

/// One stop: a white card that is one big button. A round badge on the left,
/// the stop's details on the right, and one line along the bottom that says
/// what tapping the card does.
struct StopCard: View {
    let stop: Stop
    let state: RowState
    /// The blue line on top ("CLOSEST STOP"), if there is something to say.
    var headline: String?
    var distanceMeters: Double?
    /// A blue edge round the stop the app thinks the tech is at.
    var highlighted = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Card(outline: highlighted ? Theme.blue : nil) {
                VStack(spacing: 0) {
                    HStack(alignment: .top, spacing: 16) {
                        StopBadge(badge: state.badge)
                        VStack(alignment: .leading, spacing: 4) {
                            if let headline {
                                ActionText(headline)
                            }
                            MetaLines(parts: meta)
                            // Three lines and a little shrinking: a long
                            // business name must never end in "…".
                            Text(stop.listName)
                                .zestFont(.rowName)
                                .foregroundColor(Theme.ink)
                                .lineLimit(3)
                                .minimumScaleFactor(0.7)
                                .multilineTextAlignment(.leading)
                                .fixedSize(horizontal: false, vertical: true)
                            Text(stop.address)
                                .zestFont(.rowStreet)
                                .foregroundColor(Theme.ink)
                                .lineLimit(3)
                                .minimumScaleFactor(0.7)
                                .multilineTextAlignment(.leading)
                                .fixedSize(horizontal: false, vertical: true)
                            if !stop.cityLine.isEmpty {
                                Text(stop.cityLine)
                                    .zestFont(.rowStreet)
                                    .foregroundColor(Theme.softInk)
                                    .lineLimit(1)
                                    .minimumScaleFactor(0.7)
                            }
                        }
                        Spacer(minLength: 0)
                    }
                    .padding(18)

                    Hairline()
                    ActionRow(text: state.text, tone: state.tone)
                }
            }
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spokenLabel)
        .accessibilityAddTraits(.isButton)
        .accessibilityIdentifier("stop-\(stop.customerId)")
    }

    /// Everything on the card, read out in order: "Closest stop. Smith, Jane,
    /// 123 Main St. Record Video."
    private var spokenLabel: String {
        let lead = headline.map { $0 + ". " } ?? ""
        return "\(lead)\(stop.listName), \(stop.address). \(state.text.capitalized)."
    }

    /// The small grey facts: how far away, and the first service.
    private var meta: [String] {
        var parts: [String] = []
        if let distanceMeters {
            parts.append(Geo.distanceText(meters: distanceMeters))
        }
        if let service = stop.services.first, !service.isEmpty {
            parts.append(service)
        }
        return parts
    }
}

/// The small grey facts on a card. On one line with a dot between them when
/// that fits ("50 FT AWAY • MOWING"); otherwise one clean line each, with no
/// dot left hanging at the start or end of a line.
struct MetaLines: View {
    let parts: [String]

    var body: some View {
        if parts.count > 1 {
            ViewThatFits(in: .horizontal) {
                Text(parts.joined(separator: " • ").uppercased())
                    .tracking(1)
                    .zestFont(.label)
                    .foregroundColor(Theme.softInk)
                    .lineLimit(1)
                    .fixedSize()
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(parts, id: \.self) { part in
                        SmallLabel(part)
                    }
                }
            }
        } else if let only = parts.first {
            SmallLabel(only)
        }
    }
}

/// The big round badge on the left of a card: the stop's number in blue,
/// a white tick on green once the video is sent, an up arrow on amber while
/// it is still on the phone.
struct StopBadge: View {
    let badge: RowState.Badge

    var body: some View {
        let size = Theme.badgeSize
        ZStack {
            Circle()
                .fill(fill)
            switch badge {
            case .number(let number):
                Text("\(number)")
                    .font(.system(size: size * 0.5, weight: .bold))
                    .foregroundColor(.white)
                    .lineLimit(1)
                    .minimumScaleFactor(0.5)
                    .padding(.horizontal, 6)
            case .sent:
                Image(systemName: "checkmark")
                    .font(.system(size: size * 0.46, weight: .heavy))
                    .foregroundColor(.white)
            case .waiting:
                Image(systemName: "arrow.up")
                    .font(.system(size: size * 0.46, weight: .heavy))
                    .foregroundColor(Theme.ink)
            case .problem:
                Image(systemName: "exclamationmark")
                    .font(.system(size: size * 0.46, weight: .heavy))
                    .foregroundColor(.white)
            }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }

    private var fill: Color {
        switch badge {
        case .number: return Theme.blue
        case .sent: return Theme.green
        case .waiting: return Theme.amber
        case .problem: return Theme.ink
        }
    }
}

/// The one line along the bottom of a card, at least 64 pt tall: what
/// tapping the card does ("RECORD VIDEO") or where its video is ("SENT ✓").
struct ActionRow: View {
    let text: String
    let tone: StateTone

    var body: some View {
        Text(text)
            .tracking(0.6)
            .zestFont(.action)
            .foregroundColor(tone.ink)
            .multilineTextAlignment(.center)
            .lineLimit(2)
            .minimumScaleFactor(0.7)
            .padding(.horizontal, 12)
            // Air above and below, for when the words take two lines (the
            // largest text): they must not touch the card's edge. One line
            // is shorter than the row either way, so it looks as before.
            .padding(.vertical, 8)
            .frame(maxWidth: .infinity, minHeight: Theme.actionRowHeight)
            .background(tone.fill)
    }
}
