import SwiftUI
import ZestCore

/// The big ring that drains away while recording, with the seconds left as a
/// huge number in its see-through middle.
///
/// It starts full and empties clockwise over exactly `maxSeconds`. It is
/// yellow on a dark track and turns red for the last three seconds. Time is
/// worked out from the moment the camera really started writing, on every
/// frame, so the ring can never drift from the recording.
struct CountdownRing: View {
    /// When the file output began recording.
    let startedAt: Date
    let maxSeconds: Double
    var diameter: CGFloat = 230
    var lineWidth: CGFloat = 18
    /// Hold the ring still at this many seconds in. Only set in screenshot mode.
    var frozenElapsed: TimeInterval?

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        // Thirty redraws a second reads as smooth; Reduce Motion gets a calmer four.
        TimelineView(.periodic(from: startedAt, by: reduceMotion ? 0.25 : 1.0 / 30.0)) { context in
            let elapsed = max(0, frozenElapsed ?? context.date.timeIntervalSince(startedAt))
            RingFace(
                fractionLeft: Countdown.fractionLeft(elapsed: elapsed, maxSeconds: maxSeconds),
                secondsLeft: Countdown.secondsLeft(elapsed: elapsed, maxSeconds: maxSeconds),
                finalStretch: Countdown.isFinalStretch(elapsed: elapsed, maxSeconds: maxSeconds),
                diameter: diameter,
                lineWidth: lineWidth
            )
        }
        .frame(width: diameter, height: diameter)
    }
}

/// One frame of the ring. Split out so the haptic tick can hang off the
/// whole-second value changing.
private struct RingFace: View {
    let fractionLeft: Double
    let secondsLeft: Int
    let finalStretch: Bool
    let diameter: CGFloat
    let lineWidth: CGFloat

    var body: some View {
        ZStack {
            // Soft dark disc so the number reads over any picture.
            Circle()
                .fill(Color.black.opacity(0.28))

            // The track: what has already drained away.
            Circle()
                .strokeBorder(Color.black.opacity(0.55), lineWidth: lineWidth)

            // The time left. The gap opens at 12 o'clock and grows clockwise.
            Circle()
                .inset(by: lineWidth / 2)
                .trim(from: CGFloat(1 - fractionLeft), to: 1)
                .stroke(
                    finalStretch ? Theme.recordRed : Theme.yellow,
                    style: StrokeStyle(lineWidth: lineWidth, lineCap: .butt)
                )
                .rotationEffect(.degrees(-90))

            // A thin white edge keeps the ring visible against a bright sky.
            Circle()
                .strokeBorder(Color.white.opacity(0.9), lineWidth: 2)

            Text("\(secondsLeft)")
                .font(.system(size: 96, weight: .heavy, design: .rounded))
                .monospacedDigit()
                .foregroundColor(.white)
                .shadow(color: .black.opacity(0.9), radius: 6, x: 0, y: 2)
                .minimumScaleFactor(0.5)
                .lineLimit(1)
        }
        .frame(width: diameter, height: diameter)
        .onChange(of: secondsLeft) { _, left in
            // A light tick on each of the last three seconds.
            if left >= 1 && left <= 3 {
                Haptics.tick()
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Recording. \(secondsLeft) seconds left.")
        .accessibilityAddTraits(.updatesFrequently)
    }
}
