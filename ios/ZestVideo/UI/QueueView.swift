import SwiftUI
import ZestCore

/// "My videos": every video from today and where it is. It always tells the
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
        Screen(title: "My videos") {
            if rows.isEmpty {
                BodyText("No videos yet today.", bold: true)
            }
            ForEach(rows) { item in
                Button {
                    app.open(item: item)
                } label: {
                    VStack(alignment: .leading, spacing: 10) {
                        Text(item.customerName)
                            .zestFont(.rowName)
                            .foregroundColor(Theme.ink)
                            .lineLimit(2)
                            .minimumScaleFactor(0.7)
                            .multilineTextAlignment(.leading)
                        StatusPill(status: RowStatus(text: item.displayText, kind: kind(item)))
                    }
                    .frame(maxWidth: .infinity, minHeight: 96, alignment: .leading)
                    .padding(18)
                    .background(RoundedRectangle(cornerRadius: Theme.corner, style: .continuous).fill(Theme.card))
                    .overlay(RoundedRectangle(cornerRadius: Theme.corner, style: .continuous).strokeBorder(Theme.purple, lineWidth: 3))
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("\(item.customerName). \(item.displayText.capitalized).")
                .accessibilityAddTraits(.isButton)
            }

            BodyText("Zest Video is holding \(StorageGuard.sizeText(heldBytes)) on this phone.", color: Theme.softInk)
                .padding(.top, 6)
            if queue.items.waitingCount > 0 {
                BodyText("Waiting videos send by themselves when there is signal.", color: Theme.softInk)
            }

            // The small print, at the very end.
            VStack(alignment: .leading, spacing: 4) {
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
            .padding(.top, 26)
        } footer: {
            BigButton(title: "BACK", kind: .plain) {
                app.backToToday()
            }
        }
        .onAppear(perform: measure)
        .onChange(of: queue.items.waitingCount) { _, _ in
            measure()
        }
    }

    private var signOutQuestion: some View {
        Screen(title: "Sign out?") {
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
        TextLink(title: "Sign out", color: Theme.softInk) {
            confirmingSignOut = true
        }
    }

    private func kind(_ item: QueueItem) -> RowStatus.Kind {
        switch item.display {
        case .sent: return .sent
        case .blocked: return .problem
        case .waiting, .sending: return .waiting
        }
    }

    private func measure() {
        heldBytes = queue.heldBytes
    }
}
