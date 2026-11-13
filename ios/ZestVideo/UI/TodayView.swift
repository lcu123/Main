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
        Screen(bar: "My Route", tab: .route, startAtBottom: app.seeds.scrollToEnd) {
            // A card says how many minutes are left to change a video, so
            // the list looks at the clock again every little while.
            TimelineView(.periodic(from: Date(), by: 20)) { context in
                VStack(alignment: .leading, spacing: 18) {
                    header
                    banners

                    if ranked.all.isEmpty {
                        emptyState
                    } else {
                        if !ranked.suggested.isEmpty {
                            SmallLabel("Tap your stop")
                            ForEach(Array(ranked.suggested.enumerated()), id: \.element.id) { index, entry in
                                card(entry, headline: headline(for: entry.stop, first: index == 0, reason: ranked.reason),
                                     highlighted: index == 0, now: context.date)
                            }
                        }
                        if !ranked.rest.isEmpty {
                            SmallLabel("Rest of today")
                                .padding(.top, 6)
                            ForEach(ranked.rest) { entry in
                                card(entry, headline: headline(for: entry.stop, first: false, reason: ranked.reason),
                                     highlighted: false, now: context.date)
                            }
                        }
                    }

                    notOnMyList
                }
            }
        }
        .task {
            // Keep the list fresh while it is on screen. Stops with the view.
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 120 * 1_000_000_000)
                if Task.isCancelled { break }
                await app.refreshToday()
                location.refresh(precise: false)
            }
        }
    }

    // MARK: Header

    /// The tech's own card: a round badge with his initial, his first name,
    /// and the day's two numbers, with a blue bar along the bottom edge.
    private var header: some View {
        let name = app.tech?.firstName ?? ""
        let total = app.stops.count
        return VStack(spacing: 12) {
            if let day = app.today.flatMap({ ZestDates.longDay($0.date) }) {
                Text(day)
                    .zestFont(.action)
                    .foregroundColor(Theme.blue)
                    .frame(maxWidth: .infinity, alignment: .center)
                    .accessibilityAddTraits(.isHeader)
            }
            Card {
                VStack(spacing: 0) {
                    HStack(alignment: .center, spacing: 16) {
                        ZStack {
                            Circle()
                                .fill(Theme.blue)
                            Text(PersonName.initial(name))
                                .font(.system(size: Theme.badgeSize * 0.5, weight: .bold))
                                .foregroundColor(.white)
                        }
                        .frame(width: Theme.badgeSize, height: Theme.badgeSize)
                        .accessibilityHidden(true)

                        VStack(alignment: .leading, spacing: 8) {
                            Text(name.isEmpty ? "Today" : name)
                                .zestFont(.heroStreet)
                                .foregroundColor(Theme.ink)
                                .lineLimit(1)
                                .minimumScaleFactor(0.6)
                            HStack(alignment: .top, spacing: 12) {
                                stat("Videos sent", "\(app.videosSentCount)/\(total)")
                                stat("Waiting", "\(app.waitingCount)")
                            }
                        }
                    }
                    .padding(18)
                    .frame(maxWidth: .infinity, alignment: .leading)

                    Rectangle()
                        .fill(Theme.blue)
                        .frame(height: 6)
                }
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("\(name). Videos sent \(app.videosSentCount) of \(total). Waiting \(app.waitingCount).")
        }
    }

    private func stat(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            SmallLabel(label)
            Text(value)
                .zestFont(.stat)
                .foregroundColor(Theme.ink)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
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

    /// The blue line at the top of a card: the most useful thing about it.
    private func headline(for stop: Stop, first: Bool, reason: StopRanker.Reason) -> String? {
        if stop.isCheckedInNow { return "Checked in" }
        guard first else { return nil }
        switch reason {
        case .checkedIn: return "Checked in"
        case .nearest: return "Closest stop"
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
        }
        if let notice = app.notice {
            Banner(text: notice, tone: .attention)
        }
        if app.waitingCount > 0 {
            Button {
                app.showQueue()
            } label: {
                Banner(
                    text: app.waitingCount == 1 ? "1 video waiting to send" : "\(app.waitingCount) videos waiting to send",
                    tone: .attention
                )
            }
            .buttonStyle(.plain)
            .accessibilityHint("Opens My Videos")
        }
        if app.offline {
            Banner(text: app.today == nil ? "No signal. Your stops will show when there is signal." : "No signal. Showing your saved list.", tone: .info)
        }
    }

    @ViewBuilder
    private var emptyState: some View {
        if app.today == nil && app.refreshing {
            BodyText("Getting your stops…", bold: true)
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
        .padding(.top, 6)
        .accessibilityLabel("Stop not on my list")
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
                            if let meta {
                                SmallLabel(meta)
                            }
                            Text(stop.listName)
                                .zestFont(.rowName)
                                .foregroundColor(Theme.ink)
                                .lineLimit(2)
                                .minimumScaleFactor(0.7)
                                .multilineTextAlignment(.leading)
                                .fixedSize(horizontal: false, vertical: true)
                            Text(stop.address)
                                .zestFont(.rowStreet)
                                .foregroundColor(Theme.ink)
                                .lineLimit(2)
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
        .accessibilityLabel("\(stop.listName), \(stop.address). \(state.text.capitalized).")
        .accessibilityAddTraits(.isButton)
    }

    /// The small grey line: how far away, and the first service. "50 FT AWAY • MOWING".
    private var meta: String? {
        var parts: [String] = []
        if let distanceMeters {
            parts.append(Geo.distanceText(meters: distanceMeters))
        }
        if let service = stop.services.first, !service.isEmpty {
            parts.append(service)
        }
        return parts.isEmpty ? nil : parts.joined(separator: " • ")
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
            .frame(maxWidth: .infinity, minHeight: Theme.actionRowHeight)
            .background(tone.fill)
    }
}
