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
            Screen(bar: "Video", onBack: goBack) {
                headline(summary, chip: nil)
                BodyText("Another tech already recorded this stop.", bold: true)
                BodyText("If you record it again, your video takes its place.")
            } footer: {
                BigButton(title: "RECORD AGAIN", kind: .primary) { app.recordAgain(summary) }
                back
            }
        case .waiting:
            Screen(bar: "Video", onBack: goBack) {
                headline(summary, chip: StatusChip(text: summary.statusText, tone: .waiting))
                BodyText("This video is waiting to send.", bold: true)
                BodyText("It sends by itself when there is signal. You do not need to do anything.")
            } footer: {
                back
            }
        case .locked:
            locked(summary, message: UploadManager.lockedMessage)
        case .blocked(let message):
            Screen(bar: "Video", onBack: goBack) {
                headline(summary, chip: nil)
                Banner(text: message, tone: .attention)
                officeNumber
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
        Screen(bar: "Video", onBack: goBack) {
            headline(summary, chip: StatusChip(
                text: summary.statusText, tone: summary.statusText.hasPrefix("SENT") ? .good : .waiting
            ))
            // One sentence, so all of it shows above the three buttons.
            BodyText(HoldWindow.text(secondsLeft: secondsLeft) + " Then it goes to the customer.", bold: true)
        } footer: {
            BigButton(title: "RECORD AGAIN", kind: .primary) { app.recordAgain(summary) }
            BigButton(title: "DELETE VIDEO", kind: .dark) { confirmingDelete = true }
            back
        }
    }

    private func deleteQuestion(_ summary: VideoSummary) -> some View {
        Screen(bar: "Delete Video", title: "Delete this video?") {
            StopHeadline(name: summary.name, address: summary.address, style: .tight)
            BodyText("The customer will not get a video for this visit.")
        } footer: {
            BigButton(title: "YES, DELETE", kind: .dark, isBusy: busy) {
                guard !busy else { return }
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
        Screen(bar: "Video", onBack: goBack) {
            headline(summary, chip: StatusChip(text: "SENT ✓", tone: .good))
            BodyText(message, bold: true)
            officeNumber
        } footer: {
            if app.canCallOffice {
                BigButton(title: "CALL OFFICE", kind: .primary) { app.callOffice() }
            }
            back
        }
    }

    private var gone: some View {
        Screen(bar: "Video", onBack: goBack, title: "No video here") {
            BodyText("This stop has no video now.")
        } footer: {
            back
        }
    }

    /// The name and street on their card, in the same large type as the
    /// confirm screen but with less padding, and the video's state on the
    /// card itself: these screens have up to three buttons below.
    private func headline(_ summary: VideoSummary, chip: StatusChip?) -> some View {
        StopHeadline(name: summary.name, address: summary.address, style: .tight, chip: chip)
    }

    /// On a device that cannot place a call, the number in words instead.
    @ViewBuilder
    private var officeNumber: some View {
        if let text = app.officePhoneText {
            BodyText(text, bold: true)
        }
    }

    /// The arrow in the top bar and the big BACK button do the same thing.
    private func goBack() {
        app.backToToday()
    }

    private var back: some View {
        BigButton(title: "BACK", kind: .plain, action: goBack)
    }
}
