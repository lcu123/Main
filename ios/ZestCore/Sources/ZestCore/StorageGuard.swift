import Foundation

/// Keeps the app from filling a tech's own phone.
public enum StorageGuard {
    public enum Level: Equatable, Sendable {
        case ok
        /// Under 500 MB free: warn, still allow recording.
        case warn
        /// Under 200 MB free: do not record.
        case block
    }

    public static let warnBytes: Int64 = 500 * 1024 * 1024
    public static let blockBytes: Int64 = 200 * 1024 * 1024

    /// `freeBytes` of `nil` means the phone would not say; never block on that.
    public static func level(freeBytes: Int64?) -> Level {
        guard let freeBytes else { return .ok }
        if freeBytes < blockBytes { return .block }
        if freeBytes < warnBytes { return .warn }
        return .ok
    }

    /// "12 MB", "1.4 GB", "0 MB".
    public static func sizeText(_ bytes: Int64) -> String {
        let megabytes = Double(max(bytes, 0)) / (1024 * 1024)
        if megabytes >= 1024 {
            return String(format: "%.1f GB", megabytes / 1024)
        }
        if megabytes > 0 && megabytes < 1 {
            return "1 MB"
        }
        return "\(Int(megabytes.rounded())) MB"
    }
}

/// Clock maths for the record screen: the ring, the big number, the prompts.
public enum Countdown {
    /// 1.0 when recording starts, 0.0 when time is up.
    public static func fractionLeft(elapsed: TimeInterval, maxSeconds: Double) -> Double {
        guard maxSeconds > 0 else { return 0 }
        return min(1, max(0, 1 - elapsed / maxSeconds))
    }

    /// The big number: whole seconds left, rounded up, so it reads 15 at the
    /// start and 1 during the last second.
    public static func secondsLeft(elapsed: TimeInterval, maxSeconds: Double) -> Int {
        let left = maxSeconds - max(0, elapsed)
        return max(0, Int(left.rounded(.up)))
    }

    /// The ring turns red for the last three seconds.
    public static func isFinalStretch(elapsed: TimeInterval, maxSeconds: Double) -> Bool {
        let left = maxSeconds - elapsed
        return left <= 3 && left > 0
    }

    /// Which prompt is live: each gets an even share of the clip.
    public static func promptIndex(elapsed: TimeInterval, maxSeconds: Double, count: Int) -> Int {
        guard count > 0, maxSeconds > 0 else { return 0 }
        let share = maxSeconds / Double(count)
        let index = Int(max(0, elapsed) / share)
        return min(count - 1, max(0, index))
    }

    /// Tapping the ring ends the clip early only after this long, so a double
    /// tap in gloves cannot cut it to nothing.
    public static let earliestStopSeconds: TimeInterval = 3
}
