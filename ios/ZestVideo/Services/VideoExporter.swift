import AVFoundation
import Foundation

/// Turns the camera's .mov into the file that is uploaded: an .mp4 with
/// H.264 video and AAC audio, with its index at the front ("fast start") so
/// the customer's page starts playing at once in any browser.
///
/// This is a re-wrap, not a re-encode: it takes well under a second and
/// loses no quality. Nothing is ever loaded into memory.
enum VideoExporter {
    struct Output {
        let url: URL
        let seconds: Double
        let bytes: Int64
    }

    /// A 15-second clip should be about 5 MB. Past this the bitrate setting
    /// did not take, and the clip is re-encoded smaller.
    static let largeFileBytes: Int64 = 12 * 1024 * 1024

    enum Failure: Error, Equatable {
        /// The camera's file is empty, or holds less than half a second:
        /// there is no clip in it to keep.
        case nothingThere
        /// The file looks sound but could not be read or re-wrapped this
        /// time (the app was sent to the background in the middle, say).
        /// It is worth another try; the camera's file must be kept.
        case failed
    }

    static func makeMP4(from source: URL, to destination: URL) async -> Result<Output, Failure> {
        guard Files.size(of: source) > 0 else { return .failure(.nothingThere) }
        let asset = AVURLAsset(url: source)
        // A file that cannot be read at all right now is not the same as an
        // empty one: it may be readable the next time.
        guard let duration = try? await asset.load(.duration) else { return .failure(.failed) }
        let seconds = CMTimeGetSeconds(duration)
        guard seconds.isFinite, seconds >= 0.5 else { return .failure(.nothingThere) }

        Files.remove(destination)
        var done = await export(asset, preset: AVAssetExportPresetPassthrough, to: destination)

        if done, Files.size(of: destination) > largeFileBytes {
            let smaller = destination.deletingLastPathComponent()
                .appendingPathComponent("small-" + destination.lastPathComponent)
            if await export(asset, preset: AVAssetExportPreset1280x720, to: smaller),
               Files.size(of: smaller) > 0, Files.size(of: smaller) < Files.size(of: destination) {
                Files.remove(destination)
                try? FileManager.default.moveItem(at: smaller, to: destination)
            } else {
                Files.remove(smaller)
            }
        }
        if !done {
            done = await export(asset, preset: AVAssetExportPreset1280x720, to: destination)
        }

        let bytes = Files.size(of: destination)
        guard done, bytes > 0 else {
            Files.remove(destination)
            return .failure(.failed)
        }
        Files.protect(destination)
        return .success(Output(url: destination, seconds: seconds, bytes: bytes))
    }

    private static func export(_ asset: AVAsset, preset: String, to url: URL) async -> Bool {
        Files.remove(url)
        guard let session = AVAssetExportSession(asset: asset, presetName: preset),
              session.supportedFileTypes.contains(.mp4) else { return false }
        session.outputURL = url
        session.outputFileType = .mp4
        session.shouldOptimizeForNetworkUse = true
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            session.exportAsynchronously {
                continuation.resume()
            }
        }
        if session.status != .completed {
            Files.remove(url)
            return false
        }
        return true
    }
}
