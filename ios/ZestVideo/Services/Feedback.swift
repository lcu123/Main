import AudioToolbox
import AVFoundation
import UIKit

/// Buzzes the tech can feel through gloves.
@MainActor
enum Haptics {
    /// Recording started or stopped.
    static func thump() {
        let generator = UIImpactFeedbackGenerator(style: .heavy)
        generator.prepare()
        generator.impactOccurred()
    }

    /// One of the last three seconds.
    static func tick() {
        UIImpactFeedbackGenerator(style: .rigid).impactOccurred()
    }

    static func success() {
        UINotificationFeedbackGenerator().notificationOccurred(.success)
    }

    static func warning() {
        UINotificationFeedbackGenerator().notificationOccurred(.warning)
    }
}

/// The phone's own start and stop recording sounds.
enum Sounds {
    static func beginRecording() {
        AudioServicesPlaySystemSound(1117)
    }

    static func endRecording() {
        AudioServicesPlaySystemSound(1118)
    }

    /// iOS silences haptics and system sounds while the microphone is live
    /// unless the app asks otherwise. Without this the tech would feel nothing
    /// when recording starts or during the last three seconds.
    static func allowDuringRecording() {
        try? AVAudioSession.sharedInstance().setAllowHapticsAndSystemSoundsDuringRecording(true)
    }

    /// After recording, send playback to the loud speaker, not the earpiece.
    static func preparePlayback() {
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playback, mode: .moviePlayback)
        try? session.setActive(true)
    }
}
