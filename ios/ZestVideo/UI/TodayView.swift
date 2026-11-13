import SwiftUI
import ZestCore

/// Today's stops. The app's best guess sits on top (checked in, or nearest),
/// then the next two, then the rest of the route.
struct TodayView: View {
    @EnvironmentObject private var app: AppModel
    @EnvironmentObject private var queue: QueueStore
    @EnvironmentObject private var location: LocationService

    var body: some View {
        let ranked = app.ranked
        Screen(title: greeting, startAtBottom: app.seeds.scrollToEnd) {
            banners

            if ranked.all.isEmpty {
                emptyState
            } else {
                if !ranked.suggested.isEmpty {
                    sectionLabel("TAP YOUR STOP")
                    ForEach(Array(ranked.suggested.enumerated()), id: \.element.id) { index, entry in
                        StopCard(
                            entry: entry,
                            status: app.rowStatus(for: entry.stop),
                            highlighted: index == 0,
                            hint: index == 0 ? hint(for: ranked.reason) : nil
                        ) {
                            app.open(stop: entry.stop)
                        }
                    }
                }
                if !ranked.rest.isEmpty {
                    sectionLabel("REST OF TODAY")
                    ForEach(ranked.rest) { entry in
                        StopCard(entry: entry, status: app.rowStatus(for: entry.stop), highlighted: false, hint: nil) {
                            app.open(stop: entry.stop)
                        }
                    }
                }
            }

            BigButton(title: "MY VIDEOS", kind: .plain) {
                app.showQueue()
            }
            .padding(.top, 8)
        } footer: {
            BigButton(title: "NOT ON MY LIST", kind: .plain) {
                app.openNotOnMyList()
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

    private var greeting: String {
        let name = app.tech?.firstName ?? ""
        return name.isEmpty ? "Today" : "Hi \(name)"
    }

    private func hint(for reason: StopRanker.Reason) -> String {
        switch reason {
        case .checkedIn: return "YOU ARE CHECKED IN HERE"
        case .nearest: return "CLOSEST STOP"
        case .routeOrder: return "NEXT ON YOUR LIST"
        }
    }

    @ViewBuilder
    private var banners: some View {
        if app.draft != nil {
            Button {
                app.openReview()
            } label: {
                VStack(alignment: .leading, spacing: 6) {
                    Text("1 video not saved yet")
                        .zestFont(.rowName)
                    Text("TAP HERE TO FINISH")
                        .zestFont(.bodyBold)
                }
                .foregroundColor(Theme.black)
                .frame(maxWidth: .infinity, minHeight: Theme.buttonHeight, alignment: .leading)
                .padding(16)
                .background(RoundedRectangle(cornerRadius: Theme.corner, style: .continuous).fill(Theme.yellow))
                .overlay(RoundedRectangle(cornerRadius: Theme.corner, style: .continuous).strokeBorder(Theme.black, lineWidth: 4))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("1 video not saved yet. Tap to finish.")
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
                    tone: .info
                )
            }
            .buttonStyle(.plain)
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
            BodyText("You can still record. Tap NOT ON MY LIST.")
        } else {
            BodyText("No stops on your list today.", bold: true)
            BodyText("To record a stop anyway, tap NOT ON MY LIST.")
        }
    }

    private func sectionLabel(_ text: String) -> some View {
        Text(text)
            .zestFont(.section)
            .foregroundColor(Theme.purple)
            .padding(.top, 6)
            .accessibilityAddTraits(.isHeader)
    }
}

/// One stop: a whole-width card that is one big button.
struct StopCard: View {
    let entry: StopRanker.RankedStop
    let status: RowStatus
    let highlighted: Bool
    let hint: String?
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 8) {
                if let hint {
                    Text(hint)
                        .zestFont(.section)
                        .foregroundColor(highlighted ? Theme.yellow : Theme.purple)
                }
                Text(entry.stop.name)
                    .zestFont(highlighted ? .title : .rowName)
                    .foregroundColor(highlighted ? Theme.white : Theme.ink)
                    .lineLimit(2)
                    .minimumScaleFactor(0.7)
                    .multilineTextAlignment(.leading)
                Text(entry.stop.address)
                    .zestFont(.rowStreet)
                    .foregroundColor(highlighted ? Theme.white : Theme.ink)
                    .lineLimit(2)
                    .minimumScaleFactor(0.7)
                    .multilineTextAlignment(.leading)
                // Side by side when there is room, one under the other when not.
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 12) {
                        StatusPill(status: status)
                        distance
                    }
                    VStack(alignment: .leading, spacing: 6) {
                        StatusPill(status: status)
                        distance
                    }
                }
            }
            .frame(maxWidth: .infinity, minHeight: 96, alignment: .leading)
            .padding(18)
            .background(
                RoundedRectangle(cornerRadius: Theme.corner, style: .continuous)
                    .fill(highlighted ? Theme.purple : Theme.card)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Theme.corner, style: .continuous)
                    .strokeBorder(highlighted ? Theme.deepViolet : Theme.purple, lineWidth: highlighted ? 5 : 3)
            )
            .contentShape(RoundedRectangle(cornerRadius: Theme.corner, style: .continuous))
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(entry.stop.name), \(entry.stop.address). \(status.text.capitalized).")
        .accessibilityAddTraits(.isButton)
    }

    @ViewBuilder
    private var distance: some View {
        if let meters = entry.distanceMeters {
            Text(Geo.distanceText(meters: meters))
                .zestFont(.distance)
                .foregroundColor(highlighted ? Theme.white : Theme.softInk)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        }
    }
}

/// A video's state in words on a colored pill: never a bare icon.
struct StatusPill: View {
    let status: RowStatus

    var body: some View {
        Text(status.text)
            .zestFont(.pill)
            .foregroundColor(status.kind == .problem ? Theme.white : Theme.black)
            .lineLimit(1)
            .minimumScaleFactor(0.7)
            .padding(.horizontal, 14)
            .padding(.vertical, 6)
            .background(Capsule().fill(fill))
            .overlay(Capsule().strokeBorder(Theme.black, lineWidth: 2))
    }

    private var fill: Color {
        switch status.kind {
        case .none: return Theme.white
        case .waiting: return Theme.yellow
        case .sent: return Theme.lime
        case .problem: return Theme.black
        }
    }
}
