import Foundation
import ZestCore

// Stand-ins for the two simulator-only helpers of the app that the stand-in
// recorder in CameraService.swift calls. (The real ones are in
// App/UITestMode.swift and Services/StandInClip.swift.)

enum StandInClip {
    @discardableResult
    static func write(to url: URL) -> Bool {
        (try? Data(repeating: 9, count: 1500).write(to: url)) != nil
    }
}

/// What a UI test would pass in the launch environment, set by the script.
enum UITestMode {
    enum CameraStop: Equatable {
        case onOpen
        case onTap
        case after(TimeInterval)
    }

    static var notes: [String] = []
    static var silentCamera = false
    static var interruption: (at: TimeInterval, lasting: TimeInterval, cut: RecordingCut)?
    static var cameraStops: CameraStop?
    static var cameraDeadSeconds: TimeInterval = 0
    static var cameraRestartSeconds: TimeInterval = 0

    static func note(_ text: String) {
        notes.append(text)
    }

    static func reset() {
        notes = []
        silentCamera = false
        interruption = nil
        cameraStops = nil
        cameraDeadSeconds = 0
        cameraRestartSeconds = 0
    }
}
