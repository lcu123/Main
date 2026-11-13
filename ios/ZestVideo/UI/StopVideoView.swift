import SwiftUI
import ZestCore

/// A stop that already has a video.
///
/// For ten minutes after recording the tech can record again or delete it.
/// After that it has gone to the customer and only the office can change it.
struct StopVideoView: View {
    @EnvironmentObject private var app: AppModel
    @EnvironmentObject private var queue: QueueStore

    @State private var confirmingDelete = false
    @State private var busy = false

    var body: some View {
        // Re-read every few seconds so the minutes left count down by themselves.
        TimelineView(.periodic(from: Date(), by: 5)) { context in
            if let focus = app.focus {
                content(app.summary(for: focus, now: context.date), now: context.date)
            } else {
                gone
            }
        }
    }

    @ViewBuilder
    private func content(_ summary: VideoSummary, now: Date) -> some View {
        switch summary.mode {
        case .changeable(let closesAt):
            if confirmingDelete {
                deleteQuestion(summary)
            } else {
                changeable(summary, secondsLeft: max(0, Int(closesAt.timeIntervalSince(now).rounded(.up))))
            }
        case .otherTech:
            Screen(title: summary.name) {
                address(summary)
                BodyText("Another tech already recorded this stop.", bold: true)
                BodyText("If you record it again, your video takes its place.")
            } footer: {
                BigButton(title: "RECORD AGAIN", kind: .primary) { app.recordAgain(summary) }
                back
            }
        case .waiting:
            Screen(title: summary.name) {
                address(summary)
                StatusPill(status: RowStatus(text: summary.statusText, kind: .waiting))
                BodyText("This video is waiting to send.", bold: true)
                BodyText("It sends by itself when there is signal. You do not need to do anything.")
            } footer: {
                back
            }
        case .locked:
            locked(summary, message: UploadManager.lockedMessage)
        case .blocked(let message):
            Screen(title: summary.name) {
                address(summary)
                Banner(text: message, tone: .attention)
            } footer: {
                if app.canCallOffice {
                    BigButton(title: "CALL OFFICE", kind: .primary) { app.callOffice() }
                }
                BigButton(title: "OK", kind: .plain) { app.dismissBlocked(summary) }
            }
        case .gone:
            gone
        }
    }

    // MARK: Inside the ten minutes

    private func changeable(_ summary: VideoSummary, secondsLeft: Int) -> some View {
        Screen(title: summary.name) {
            address(summary)
            StatusPill(status: RowStatus(
                text: summary.statusText,
                kind: summary.statusText.hasPrefix("SENT") ? .sent : .waiting
            ))
            BodyText(HoldWindow.text(secondsLeft: secondsLeft), bold: true)
            BodyText("After that it goes to the customer.")
        } footer: {
            BigButton(title: "RECORD AGAIN", kind: .primary) { app.recordAgain(summary) }
            BigButton(title: "DELETE VIDEO", kind: .dark) { confirmingDelete = true }
            back
        }
    }

    private func deleteQuestion(_ summary: VideoSummary) -> some View {
        Screen(title: "Delete this video?") {
            StopHeadline(name: summary.name, address: summary.address)
            BodyText("The customer will not get a video for this visit.")
        } footer: {
            BigButton(title: "YES, DELETE", kind: .dark, isBusy: busy) {
                busy = true
                Task {
                    await app.deleteVideo(summary)
                    busy = false
                }
            }
            BigButton(title: "NO, KEEP IT", kind: .plain) { confirmingDelete = false }
        }
    }

    // MARK: After the ten minutes

    private func locked(_ summary: VideoSummary, message: String) -> some View {
        Screen(title: summary.name) {
            address(summary)
            StatusPill(status: RowStatus(text: "SENT ✓", kind: .sent))
            BodyText(message, bold: true)
        } footer: {
            if app.canCallOffice {
                BigButton(title: "CALL OFFICE", kind: .primary) { app.callOffice() }
            }
            back
        }
    }

    private var gone: some View {
        Screen(title: "No video here") {
            BodyText("This stop has no video now.")
        } footer: {
            back
        }
    }

    @ViewBuilder
    private func address(_ summary: VideoSummary) -> some View {
        if !summary.address.isEmpty {
            Text(summary.address)
                .font(Theme.heading)
                .foregroundColor(Theme.ink)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var back: some View {
        BigButton(title: "BACK", kind: .plain) { app.backToToday() }
    }
}
