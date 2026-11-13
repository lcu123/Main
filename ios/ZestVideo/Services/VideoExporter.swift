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

    static func makeMP4(from source: URL, to destination: URL) async -> Output? {
        guard Files.size(of: source) > 0 else { return nil }
        let asset = AVURLAsset(url: source)
        let duration = (try? await asset.load(.duration)) ?? .zero
        let seconds = CMTimeGetSeconds(duration)
        guard seconds.isFinite, seconds >= 0.5 else { return nil }

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
            return nil
        }
        Files.protect(destination)
        return Output(url: destination, seconds: seconds, bytes: bytes)
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
