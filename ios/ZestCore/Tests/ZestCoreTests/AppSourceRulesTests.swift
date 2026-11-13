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
        // Words of every size: 4.5 to 1 or better, which is the mark for
        // small print. The app's words are large, but they are read by old
        // eyes in bright sun, so none is let off with the easier mark.
        let words: [(String, String)] = [
            ("ink", "card"), ("ink", "page"), ("titleInk", "page"), ("titleInk", "card"),
            // Small grey labels ("0.6 MILES AWAY") and second-rank text.
            ("softInk", "card"), ("softInk", "page"), ("softInk", "paleAmber"), ("softInk", "paleGreen"), ("softInk", "paleCyan"),
            // Blue words: action words on a card, a white button's words, links.
            ("blueInk", "card"), ("blueInk", "page"), ("blueInk", "paleCyan"), ("blueInk", "paleAmber"),
            // The chips and the coloured rows: "SENT ✓", "WAITING", "NOT SENT".
            ("darkGreen", "paleGreen"), ("ink", "paleAmber"), ("ink", "amber"), ("white", "ink"), ("white", "green"),
            ("cyan", "paleCyan"), ("navy", "card"),
            // The YES / NO switch: the word and the knob are white on both tracks.
            ("white", "switchOn"), ("white", "switchOff"),
        ]
        for (text, ground) in words {
            XCTAssertGreaterThanOrEqual(
                ratio(text, on: ground), GloveRule.minContrast,
                "\(text) on \(ground) is \(String(format: "%.2f", ratio(text, on: ground))) to 1"
            )
        }
        // The grey labels and the chips have room to spare.
        XCTAssertGreaterThanOrEqual(ratio("softInk", on: "card"), 7, "small grey labels on a white card")
        XCTAssertGreaterThanOrEqual(ratio("darkGreen", on: "paleGreen"), 7, "SENT on its green chip")
        XCTAssertGreaterThanOrEqual(ratio("ink", on: "paleAmber"), 7, "WAITING on its amber chip")
        // White words on a coloured fill, always large and bold: the main
        // button, the RECORDING sign. (The main button's blue is the one
        // the techs know from their route app; its words are 30-point bold.)
        let large: [(String, String)] = [("white", "blue"), ("white", "bluePressed"), ("white", "recordRed")]
        for (text, ground) in large {
            XCTAssertGreaterThanOrEqual(
                ratio(text, on: ground), GloveRule.minLargeTextContrast,
                "\(text) on \(ground) is \(String(format: "%.2f", ratio(text, on: ground))) to 1"
            )
        }
        // The two tracks of the switch can be told from the card they sit on.
        XCTAssertGreaterThanOrEqual(ratio("switchOn", on: "card"), GloveRule.minContrast)
        XCTAssertGreaterThanOrEqual(ratio("switchOff", on: "card"), GloveRule.minContrast)
    }

    /// `Theme.blue` is a fill. Words in blue use `Theme.blueInk`, the deeper
    /// shade that can be read on white: no screen may colour words with the
    /// fill blue again.
    func testBlueWordsUseTheDeeperBlue() throws {
        var checked = 0
        for folder in ["ZestVideo/UI"] {
            let root = iosFolder.appendingPathComponent(folder)
            for name in try FileManager.default.contentsOfDirectory(atPath: root.path) where name.hasSuffix(".swift") {
                let source = try String(contentsOf: root.appendingPathComponent(name), encoding: .utf8)
                checked += 1
                XCTAssertFalse(source.contains("foregroundColor(Theme.blue)"), "\(name): blue words must use Theme.blueInk")
                XCTAssertFalse(source.contains("foregroundStyle(Theme.blue)"), "\(name): blue words must use Theme.blueInk")
                XCTAssertFalse(source.contains("Color = Theme.blue\n"), "\(name): a default text colour must be Theme.blueInk")
            }
        }
        XCTAssertGreaterThan(checked, 10)
        let controls = try text("ZestVideo/UI/Controls.swift")
        XCTAssertTrue(controls.contains("case .plain: return Theme.blueInk"), "a white button's words")
        XCTAssertTrue(controls.contains("case .action: return Theme.blueInk"), "action words on a card")
    }

    /// The switch is drawn with those colours and no others.
    func testTheSwitchUsesItsOwnTwoTracksAndWhiteWords() throws {
        let review = try text("ZestVideo/UI/ReviewView.swift")
        XCTAssertTrue(review.contains("isOn ? Theme.switchOn : Theme.switchOff"), "the YES / NO switch must use the two tracks made for it")
        XCTAssertFalse(review.contains("Theme.hairline)"), "the pale grey track failed Apple's contrast audit")
    }

    /// The three screens that count down the minutes left read the clock
    /// through `frozenNow`, so a picture of them holds still. (A test that
    /// walked a page for two minutes once lost a card whose words went from
    /// "7 MIN LEFT" to "6 MIN LEFT" under it.) In a real build `frozenNow`
    /// is always nil and the real clock is read.
    func testTheScreensThatCountDownHoldStillForAPicture() throws {
        for file in ["TodayView.swift", "QueueView.swift", "StopVideoView.swift"] {
            let source = try text("ZestVideo/UI/\(file)")
            XCTAssertTrue(source.contains("TimelineView("), "\(file) counts down by itself")
            XCTAssertTrue(source.contains("app.seeds.frozenNow ?? context.date"), "\(file) must read the clock through frozenNow")
            let direct = source.components(separatedBy: "\n").filter { $0.contains("now: context.date") }
            XCTAssertEqual(direct, [], "\(file) reads the clock directly: \(direct)")
        }
        let mode = try text("ZestVideo/App/ScreenshotMode.swift")
        XCTAssertTrue(mode.contains("shot.seeds.frozenNow = now"), "screenshot mode must hold the clock at the moment its made-up day was built around")
        let model = try text("ZestVideo/App/AppModel.swift")
        XCTAssertTrue(model.contains("var frozenNow: Date?"), "outside screenshot mode there is no frozen moment")
        XCTAssertFalse(model.contains("frozenNow ="), "only screenshot mode may set it")
    }

    /// What comes from a customer's record (a name, a street, a town) is
    /// drawn the same way on every screen: at its full size, on as many
    /// lines as it needs (`customerText()`). It is never held to a number of
    /// lines of its own, because that is what squeezes it: held to one
    /// line, a 20-letter town was drawn at about 80% on the smallest iPhone;
    /// held to two, a 45-letter business name at about 90% on the review
    /// screen. One more letter than fits at the smallest size allowed, and
    /// the line ends in "…".
    func testEverythingFromTheCustomersRecordIsDrawnAtFullSizeOnAsManyLinesAsItNeeds() throws {
        let places: [(file: String, texts: [String])] = [
            ("ZestVideo/UI/TodayView.swift", ["Text(stop.listName)", "Text(stop.address)", "Text(stop.cityLine)"]),
            ("ZestVideo/UI/QueueView.swift", ["Text(item.customerName)"]),
            ("ZestVideo/UI/ReviewView.swift", ["Text(draft.target.name)"]),
            ("ZestVideo/UI/RecordView.swift", ["Text(target.name)", "Text(target.address)"]),
            // The big card on the confirm, video and warning screens.
            ("ZestVideo/UI/Controls.swift", ["Text(name)", "Text(address)", "Text(cityLine)"]),
        ]
        for place in places {
            let lines = try text(place.file).components(separatedBy: "\n")
            for wanted in place.texts {
                let hits = lines.indices.filter { lines[$0].trimmingCharacters(in: .whitespaces) == wanted }
                XCTAssertEqual(hits.count, 1, "\(wanted) should be drawn in one place in \(place.file)")
                for index in hits {
                    // Its modifiers: the lines that follow, up to the next view.
                    var modifiers: [String] = []
                    for next in lines[(index + 1)...] {
                        let trimmed = next.trimmingCharacters(in: .whitespaces)
                        guard trimmed.hasPrefix(".") else { break }
                        modifiers.append(trimmed)
                    }
                    XCTAssertTrue(modifiers.contains(".customerText()"), "\(wanted) in \(place.file) must be drawn with customerText()")
                    XCTAssertEqual(modifiers.filter { $0.hasPrefix(".lineLimit(") }, [], "\(wanted) in \(place.file) is held to a number of lines")
                    XCTAssertEqual(modifiers.filter { $0.hasPrefix(".minimumScaleFactor(") }, [], "\(wanted) in \(place.file) has a squeeze of its own")
                }
            }
        }

        // And `customerText()` is what it says.
        let controls = try text("ZestVideo/UI/Controls.swift")
        let start = try XCTUnwrap(controls.range(of: "struct CustomerText: ViewModifier {"))
        let body = String(controls[start.upperBound...].prefix(400))
        XCTAssertTrue(body.contains(".lineLimit(nil)"), "no limit on lines")
        XCTAssertTrue(body.contains(".fixedSize(horizontal: false, vertical: true)"), "free to grow down the page")
        XCTAssertTrue(body.contains(".minimumScaleFactor(Theme.longWordShrink)"), "only an over-long word is shrunk, and only so far")
        // That far is not far: three quarters at the least.
        let theme = try text("ZestVideo/UI/Theme.swift")
        let found = try XCTUnwrap(theme.range(of: "static let longWordShrink: CGFloat = "))
        let number = theme[found.upperBound...].prefix { $0.isNumber || $0 == "." }
        XCTAssertGreaterThanOrEqual(try XCTUnwrap(Double(number)), 0.75)
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
