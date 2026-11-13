import Foundation
import Speech

/// Turns the clip's speech into text **on the phone**, so the spoken key
/// works with no signal and the audio never leaves the device for this.
///
/// If the phone cannot do on-device recognition the answer is `nil`, which
/// the two-keys rule treats as "unknown", never as a mismatch.
enum SpeechChecker {
    /// Collects the best text so far and answers exactly once.
    private final class Box: @unchecked Sendable {
        private let lock = NSLock()
        private var continuation: CheckedContinuation<String?, Never>?
        private var latest: String?

        init(_ continuation: CheckedContinuation<String?, Never>) {
            self.continuation = continuation
        }

        func update(_ text: String) {
            lock.lock()
            latest = text
            lock.unlock()
        }

        /// Answers with the given text, or the best partial text heard so far.
        func finish(_ text: String? = nil) {
            lock.lock()
            let pending = continuation
            continuation = nil
            let answer = text ?? latest
            lock.unlock()
            pending?.resume(returning: answer)
        }
    }

    static func isAuthorized() async -> Bool {
        switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized:
            return true
        case .notDetermined:
            return await withCheckedContinuation { continuation in
                SFSpeechRecognizer.requestAuthorization { status in
                    continuation.resume(returning: status == .authorized)
                }
            }
        default:
            return false
        }
    }

    /// - Parameters:
    ///   - hints: today's first names, last names and street names, so they are spelled as on the route.
    ///   - timeout: give up (keeping any partial text) after this long.
    static func transcribe(fileURL: URL, hints: [String], timeout: TimeInterval = 6) async -> String? {
        #if targetEnvironment(simulator)
        if UITestMode.isOn {
            // The words a UI test says were spoken, or nothing ("unknown").
            return UITestMode.transcript
        }
        #endif
        guard await isAuthorized() else { return nil }
        guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US")),
              recognizer.isAvailable, recognizer.supportsOnDeviceRecognition else { return nil }

        let request = SFSpeechURLRecognitionRequest(url: fileURL)
        request.requiresOnDeviceRecognition = true
        request.shouldReportPartialResults = true
        request.contextualStrings = hints
        request.taskHint = .dictation

        return await withCheckedContinuation { (continuation: CheckedContinuation<String?, Never>) in
            let box = Box(continuation)
            let task = recognizer.recognitionTask(with: request) { result, error in
                if let result {
                    box.update(result.bestTranscription.formattedString)
                    if result.isFinal { box.finish() }
                } else if error != nil {
                    box.finish()
                }
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + timeout) {
                task.cancel()
                box.finish()
            }
        }
    }
}
