import Foundation

// Not UIKit at all: what the two halves of the harness share. It lives in
// this module because CameraService.swift imports it, and so finds the
// stand-ins for the two helpers of the app that it calls (Files, Sounds).

/// The app's `Files`, as far as the camera service uses it.
public enum Files {
    public static func size(of url: URL) -> Int64 {
        let attributes = try? FileManager.default.attributesOfItem(atPath: url.path)
        return (attributes?[.size] as? NSNumber)?.int64Value ?? 0
    }

    public static func remove(_ url: URL) {
        try? FileManager.default.removeItem(at: url)
    }
}

/// The app's `Sounds`, as far as the camera service uses it.
public enum Sounds {
    public static func allowDuringRecording() {}
}

/// A small script runner. Everything runs on the main thread, as the app's
/// screens do; `wait` and `soon` keep the main queue turning meanwhile.
public enum Script {
    public private(set) static var failures = 0
    public private(set) static var scenarios = 0

    public static func begin(_ name: String) {
        scenarios += 1
        print("== \(scenarios). \(name)")
    }

    public static func wait(_ seconds: TimeInterval) {
        RunLoop.main.run(until: Date().addingTimeInterval(seconds))
    }

    /// Must be true now.
    public static func expect(_ ok: Bool, _ what: @autoclosure () -> String, line: Int = #line) {
        if !ok {
            failures += 1
            print("   FAIL (line \(line)): \(what())")
        }
    }

    /// Must become true within `seconds` (a slow machine only makes it later).
    public static func soon(
        _ what: @autoclosure () -> String, within seconds: TimeInterval = 5, line: Int = #line, _ condition: () -> Bool
    ) {
        let deadline = Date().addingTimeInterval(seconds)
        while !condition(), Date() < deadline {
            wait(0.02)
        }
        expect(condition(), "not within \(seconds) s: \(what())", line: line)
    }

    /// Must stay true for `seconds`.
    public static func stays(
        _ what: @autoclosure () -> String, for seconds: TimeInterval, line: Int = #line, _ condition: () -> Bool
    ) {
        let deadline = Date().addingTimeInterval(seconds)
        while Date() < deadline {
            if !condition() {
                expect(false, "did not stay so for \(seconds) s: \(what())", line: line)
                return
            }
            wait(0.02)
        }
    }

    public static func clip() -> URL {
        URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("camera-harness-\(UUID().uuidString).mov")
    }

    public static func finish(_ name: String) -> Never {
        if failures == 0 {
            print("ALL \(scenarios) \(name) SCENARIOS PASSED")
            exit(0)
        }
        print("\(failures) FAILURE(S) in the \(name) scenarios")
        exit(1)
    }
}
