import SwiftUI
import ZestCore

/// "My Videos": every video from today and where it is. It always tells the
/// truth: SENT only once the server has the whole file.
struct QueueView: View {
    @EnvironmentObject private var app: AppModel
    @EnvironmentObject private var queue: QueueStore

    @State private var heldBytes: Int64 = 0
    @State private var confirmingSignOut = false

    var body: some View {
        if confirmingSignOut {
            signOutQuestion
        } else {
            list
        }
    }

    private var rows: [QueueItem] {
        queue.items
            .filter { $0.phase != .deleting && $0.phase != .removed }
            .sorted { $0.recordedAt > $1.recordedAt }
    }

    private var list: some View {
        Screen(bar: "My Videos", tab: .videos) {
            // The rows say how many minutes are left to change a video.
            TimelineView(.periodic(from: Date(), by: 20)) { context in
                VStack(alignment: .leading, spacing: 18) {
                    // Deleting the app deletes what is still on the phone.
                    if let warning = KeepApp.warning(videosOnPhone: app.videosOnPhoneCount) {
                        Banner(text: warning, tone: .attention)
                            .accessibilityElement(children: .combine)
                            .accessibilityIdentifier("keep-app-warning")
                    }
                    if rows.isEmpty {
                        Banner(text: "No videos yet today.", tone: .info)
                    }
                    ForEach(rows) { item in
                        row(item, state: app.rowState(for: item, now: context.date))
                    }
                    smallPrint
                }
            }
        }
        .onAppear(perform: measure)
        .onChange(of: queue.items.waitingCount) { _, _ in
            measure()
        }
    }

    /// One video: the same kind of card as a stop on My Route.
    private func row(_ item: QueueItem, state: RowState) -> some View {
        Button {
            app.open(item: item)
        } label: {
            Card {
                VStack(spacing: 0) {
                    HStack(alignment: .center, spacing: 16) {
                        StopBadge(badge: state.badge)
                        VStack(alignment: .leading, spacing: 8) {
                            Text(item.customerName)
                                .zestFont(.rowName)
                                .foregroundColor(Theme.ink)
                                .lineLimit(3)
                                .minimumScaleFactor(0.7)
                                .multilineTextAlignment(.leading)
                                .fixedSize(horizontal: false, vertical: true)
                            // A video saved late says which day it was filmed.
                            if let filmed = app.filmedText(for: item) {
                                Text(filmed)
                                    .zestFont(.rowStreet)
                                    .foregroundColor(Theme.softInk)
                                    .multilineTextAlignment(.leading)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            // Where the video is, in words, whatever the row below offers.
                            StatusChip(text: item.displayText, tone: tone(item))
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
        .accessibilityLabel("\(item.customerName). \(app.filmedText(for: item).map { $0 + ". " } ?? "")\(item.displayText.capitalized). \(state.text.capitalized).")
        .accessibilityAddTraits(.isButton)
        .accessibilityIdentifier("video-row-" + (item.notOnMyList ? "offlist" : String(item.customerId ?? 0)))
    }

    /// The small print, on its own card at the very end.
    private var smallPrint: some View {
        Card {
            VStack(alignment: .leading, spacing: 10) {
                SmallLabel("This phone")
                BodyText("Zest Video is holding \(StorageGuard.sizeText(heldBytes)) on this phone.")
                    .accessibilityIdentifier("held-space")
                if queue.items.waitingCount > 0 {
                    BodyText("Waiting videos send by themselves when there is signal.", color: Theme.softInk)
                }
                Hairline()
                    .padding(.vertical, 4)
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 28) {
                        signOutLink
                        LegalLinks()
                    }
                    VStack(alignment: .leading, spacing: 0) {
                        signOutLink
                        LegalLinks()
                    }
                }
                BodyText("To delete your data, sign out and call the office.", color: Theme.softInk)
            }
            .padding(18)
        }
        .padding(.top, 8)
    }

    private var signOutQuestion: some View {
        Screen(bar: "Sign Out", title: "Sign out?") {
            BodyText("You will need a new code to sign in again.")
            if queue.items.waitingCount > 0 {
                Banner(text: "Videos still waiting will send after you sign in again.", tone: .attention)
            }
        } footer: {
            BigButton(title: "YES, SIGN OUT", kind: .dark) {
                app.signOut()
            }
            BigButton(title: "NO, STAY", kind: .plain) {
                confirmingSignOut = false
            }
        }
    }

    private var signOutLink: some View {
        TextLink(title: "Sign out", color: Theme.ink, identifier: "link-signout") {
            confirmingSignOut = true
        }
    }

    private func tone(_ item: QueueItem) -> StateTone {
        switch item.display {
        case .sent: return .good
        case .blocked: return .problem
        case .waiting, .sending: return .waiting
        }
    }

    private func measure() {
        heldBytes = queue.heldBytes
    }
}
