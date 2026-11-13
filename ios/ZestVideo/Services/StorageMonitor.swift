import Foundation
import ZestCore

/// Free space on the phone. These are the techs' own phones, so the app
/// checks before every recording and never fills one up.
enum StorageMonitor {
    static func freeBytes() -> Int64? {
        #if targetEnvironment(simulator)
        // UI tests only: the free space the test said the phone has.
        if let pretend = UITestMode.freeBytes { return pretend }
        #endif
        let home = URL(fileURLWithPath: NSHomeDirectory())
        let values = try? home.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
        return values?.volumeAvailableCapacityForImportantUsage
    }

    static func level() -> StorageGuard.Level {
        StorageGuard.level(freeBytes: freeBytes())
    }
}
