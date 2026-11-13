import XCTest
@testable import ZestCore

/// Rules from the brief that are about what the app's source does NOT
/// contain. They are checked by reading the files, so a later edit cannot
/// slip one in unseen. (Runs on Linux with the rest of the logic tests.)
final class AppSourceRulesTests: XCTestCase {
    /// `ios/`, found from this file: ios/ZestCore/Tests/ZestCoreTests/<this>.
    private var iosFolder: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    }

    private func text(_ path: String) throws -> String {
        try String(contentsOf: iosFolder.appendingPathComponent(path), encoding: .utf8)
    }

    /// Every Swift file of the app itself and of its logic package.
    private func appSources() throws -> [(name: String, text: String)] {
        var found: [(String, String)] = []
        for folder in ["ZestVideo", "ZestCore/Sources"] {
            let root = iosFolder.appendingPathComponent(folder)
            guard let walker = FileManager.default.enumerator(at: root, includingPropertiesForKeys: nil) else { continue }
            for case let url as URL in walker where url.pathExtension == "swift" {
                found.append((url.lastPathComponent, try String(contentsOf: url, encoding: .utf8)))
            }
        }
        return found
    }

    func testTheAppSourcesAreWhereThisTestLooks() throws {
        let sources = try appSources()
        XCTAssertGreaterThan(sources.count, 30, "the app's Swift files were not found from \(iosFolder.path)")
        XCTAssertTrue(sources.contains { $0.name == "Files.swift" })
        XCTAssertTrue(sources.contains { $0.name == "CameraService.swift" })
        XCTAssertTrue(try text("project.yml").contains("NSCameraUsageDescription"), "project.yml holds the app's permission texts")
    }

    /// "Videos never go into Photos": the app asks for no photo-library
    /// permission and has no code that could write there.
    func testTheAppNeverTouchesThePhotoLibrary() throws {
        let project = try text("project.yml")
        for key in ["NSPhotoLibraryUsageDescription", "NSPhotoLibraryAddUsageDescription"] {
            XCTAssertFalse(project.contains(key), "project.yml must not ask for the photo library (\(key))")
        }
        let forbidden = [
            "import Photos", "import PhotosUI", "PHPhotoLibrary", "PHAssetCreationRequest", "PHAssetChangeRequest",
            "UISaveVideoAtPathToSavedPhotosAlbum", "UIImageWriteToSavedPhotosAlbum", "UIVideoAtPathIsCompatibleWithSavedPhotosAlbum",
            "PHPickerViewController", "UIImagePickerController", "PhotosPicker",
        ]
        for source in try appSources() {
            for word in forbidden {
                XCTAssertFalse(source.text.contains(word), "\(source.name) must not use \(word): videos never go into Photos")
            }
        }
    }

    /// The videos folder is private, left out of iCloud backup, and readable
    /// after the first unlock so uploads go on with the phone locked. (That
    /// the folder really carries these on a device is checked by the app's
    /// own tests on the simulator: FilesTests.)
    func testTheVideosFolderIsSetUpPrivateAndOutOfBackup() throws {
        let files = try text("ZestVideo/Services/Files.swift")
        XCTAssertTrue(files.contains("applicationSupportDirectory"), "videos live in Application Support")
        XCTAssertFalse(files.contains("documentDirectory"), "never in Documents, which the Files app and iTunes can show")
        XCTAssertTrue(files.contains("isExcludedFromBackup = true"))
        XCTAssertTrue(files.contains("FileProtectionType.completeUntilFirstUserAuthentication"))
        XCTAssertFalse(files.contains("FileProtectionType.none"))
    }

    // MARK: Bright sun: the app's own colours

    /// Every colour in `Theme.swift`, by name, as 0xRRGGBB.
    private func themeColors() throws -> [String: UInt32] {
        var colors: [String: UInt32] = ["white": 0xFFFFFF, "black": 0x000000, "card": 0xFFFFFF]
        for line in try text("ZestVideo/UI/Theme.swift").split(separator: "\n") {
            // "    static let blue = Color(hex: 0x217DE7)"
            let parts = line.trimmingCharacters(in: .whitespaces).components(separatedBy: " = Color(hex: 0x")
            guard parts.count == 2, parts[0].hasPrefix("static let ") else { continue }
            let name = String(parts[0].dropFirst("static let ".count))
            if let value = UInt32(parts[1].prefix(6), radix: 16) {
                colors[name] = value
            }
        }
        return colors
    }

    func testTheContrastSums() {
        XCTAssertEqual(GloveRule.contrast(0x000000, 0xFFFFFF), 21, accuracy: 0.001)
        XCTAssertEqual(GloveRule.contrast(0xFFFFFF, 0x000000), 21, accuracy: 0.001, "the order does not matter")
        XCTAssertEqual(GloveRule.contrast(0x217DE7, 0x217DE7), 1, accuracy: 0.001)
        XCTAssertEqual(GloveRule.contrast(0x777777, 0xFFFFFF), 4.48, accuracy: 0.01, "the well-known just-too-pale grey")
        XCTAssertEqual(GloveRule.luminance(hex: 0xFFFFFF), 1, accuracy: 0.0001)
        XCTAssertEqual(GloveRule.luminance(hex: 0x000000), 0, accuracy: 0.0001)
    }

    /// Words on what they sit on, for every pair the screens use. A later
    /// change of colour that makes one hard to read in the sun fails here,
    /// before it is on a phone.
    func testEveryPairOfColoursTheScreensUseCanBeReadInBrightSun() throws {
        let colors = try themeColors()
        XCTAssertGreaterThan(colors.count, 15, "Theme.swift's colours were not found: \(colors.keys.sorted())")
        func ratio(_ words: String, on ground: String, file: StaticString = #filePath, line: UInt = #line) -> Double {
            guard let a = colors[words], let b = colors[ground] else {
                XCTFail("Theme.swift has no colour called \(colors[words] == nil ? words : ground)", file: file, line: line)
                return 0
            }
            return GloveRule.contrast(a, b)
        }
        // Ordinary words.
        let ordinary: [(String, String)] = [
            ("ink", "card"), ("ink", "page"), ("titleInk", "page"), ("titleInk", "card"), ("softInk", "card"), ("softInk", "page"),
            ("ink", "paleAmber"), ("ink", "amber"), ("darkGreen", "paleGreen"), ("white", "green"), ("white", "ink"),
            ("cyan", "paleCyan"), ("white", "navy"),
            // The YES / NO switch: the word and the knob are white on both tracks.
            ("white", "switchOn"), ("white", "switchOff"),
        ]
        for (words, ground) in ordinary {
            XCTAssertGreaterThanOrEqual(
                ratio(words, on: ground), GloveRule.minContrast,
                "\(words) on \(ground) is \(String(format: "%.2f", ratio(words, on: ground))) to 1"
            )
        }
        // Large bold words only: buttons, action words, the RECORDING sign.
        let large: [(String, String)] = [("white", "blue"), ("blue", "card"), ("white", "bluePressed"), ("white", "recordRed"), ("blue", "paleCyan")]
        for (words, ground) in large {
            XCTAssertGreaterThanOrEqual(
                ratio(words, on: ground), GloveRule.minLargeTextContrast,
                "\(words) on \(ground) is \(String(format: "%.2f", ratio(words, on: ground))) to 1"
            )
        }
        // The two tracks of the switch can be told from the card they sit on.
        XCTAssertGreaterThanOrEqual(ratio("switchOn", on: "card"), GloveRule.minContrast)
        XCTAssertGreaterThanOrEqual(ratio("switchOff", on: "card"), GloveRule.minContrast)
    }

    /// The switch is drawn with those colours and no others.
    func testTheSwitchUsesItsOwnTwoTracksAndWhiteWords() throws {
        let review = try text("ZestVideo/UI/ReviewView.swift")
        XCTAssertTrue(review.contains("isOn ? Theme.switchOn : Theme.switchOff"), "the YES / NO switch must use the two tracks made for it")
        XCTAssertFalse(review.contains("Theme.hairline)"), "the pale grey track failed Apple's contrast audit")
    }

    /// No third-party code, and no sharing of a video out of the app.
    func testNoShareSheetAndNoThirdPartyImports() throws {
        let allowed: Set<String> = [
            "Foundation", "SwiftUI", "UIKit", "AVFoundation", "AVKit", "CoreLocation", "Speech", "UserNotifications",
            "Security", "os", "Combine", "ZestCore", "FoundationNetworking", "Darwin", "CoreMedia", "AudioToolbox",
            "CoreGraphics", "MediaPlayer", "Network", "CoreHaptics",
        ]
        for source in try appSources() {
            XCTAssertFalse(source.text.contains("UIActivityViewController"), "\(source.name): a video is never shared out of the app")
            XCTAssertFalse(source.text.contains("ShareLink("), "\(source.name): a video is never shared out of the app")
            for line in source.text.split(separator: "\n") {
                let trimmed = line.trimmingCharacters(in: .whitespaces)
                guard trimmed.hasPrefix("import ") else { continue }
                let module = trimmed.dropFirst("import ".count).split(separator: " ").first.map(String.init) ?? ""
                XCTAssertTrue(allowed.contains(module), "\(source.name) imports \(module): Apple frameworks and ZestCore only")
            }
        }
    }
}
