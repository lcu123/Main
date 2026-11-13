import XCTest
import ZestCore
@testable import ZestVideo

/// Where the videos live on the phone: private, out of iCloud backup, never
/// in Photos, and readable with the phone locked so uploads carry on.
final class FilesTests: XCTestCase {
    private func isExcludedFromBackup(_ url: URL) throws -> Bool {
        try url.resourceValues(forKeys: [.isExcludedFromBackupKey]).isExcludedFromBackup ?? false
    }

    /// The protection class a file or folder reports. `nil` where the system
    /// does not keep one: the simulator runs on a Mac disk with no data
    /// protection, so it may report nothing at all.
    private func protection(of url: URL) -> FileProtectionType? {
        let attributes = try? FileManager.default.attributesOfItem(atPath: url.path)
        let value = attributes?[.protectionKey]
        if let type = value as? FileProtectionType { return type }
        if let raw = value as? String { return FileProtectionType(rawValue: raw) }
        return nil
    }

    func testTheVideosFolderIsInsideTheAppsPrivateSupportFolder() {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        XCTAssertTrue(Files.videos.path.hasPrefix(support.path), "videos live in Application Support: \(Files.videos.path)")
        XCTAssertTrue(Files.root.path.hasPrefix(support.path))
        for visible in [FileManager.SearchPathDirectory.documentDirectory, .picturesDirectory, .moviesDirectory, .downloadsDirectory] {
            for folder in FileManager.default.urls(for: visible, in: .userDomainMask) {
                XCTAssertFalse(Files.videos.path.hasPrefix(folder.path), "videos must not be in \(folder.lastPathComponent)")
            }
        }
        XCTAssertFalse(Files.videos.path.contains("/tmp/"), "nor in a temporary folder iOS may empty")
        XCTAssertTrue(Files.exists(Files.videos), "the folder is there")
    }

    func testTheAppsFoldersAreLeftOutOfBackup() throws {
        XCTAssertTrue(try isExcludedFromBackup(Files.videos), "the videos folder must be excluded from iCloud and computer backups")
        XCTAssertTrue(try isExcludedFromBackup(Files.root), "so must the folder with the queue and the route")
    }

    func testAFolderMadeByTheAppIsPrivateFromTheStart() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("zest-files-\(UUID().uuidString)", isDirectory: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: folder) }
        XCTAssertFalse(Files.exists(folder))
        Files.prepare(folder)
        XCTAssertTrue(Files.exists(folder))
        XCTAssertTrue(try isExcludedFromBackup(folder), "prepare() must exclude the folder from backup")
        // Asking twice changes nothing.
        Files.prepare(folder)
        XCTAssertTrue(try isExcludedFromBackup(folder))
    }

    func testFilesAreProtectedUntilTheFirstUnlockAndNoLess() throws {
        XCTAssertEqual(Files.protection, .completeUntilFirstUserAuthentication,
                       "readable with the phone locked (uploads go on), unreadable before its first unlock")
        XCTAssertNotEqual(Files.protection, FileProtectionType.none)

        // A video file, as the recorder leaves it, then protected the way
        // the app does before it is queued.
        let file = Files.video("files-test-\(UUID().uuidString).mp4")
        addTeardownBlock { Files.remove(file) }
        try Data("not really a video".utf8).write(to: file)
        Files.protect(file)

        // On a phone every one of these reports its protection class. The
        // simulator may report none; what it must never report is a weaker
        // or a stricter class than the one the app asks for.
        for url in [Files.videos, Files.root, file] {
            if let reported = protection(of: url) {
                XCTAssertEqual(reported, Files.protection, "\(url.lastPathComponent) reports \(reported.rawValue)")
            }
        }
        #if !targetEnvironment(simulator)
        XCTAssertEqual(protection(of: Files.videos), Files.protection)
        XCTAssertEqual(protection(of: file), Files.protection)
        #endif
    }

    func testSmallFilesAreWrittenWhole() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("zest-files-\(UUID().uuidString)", isDirectory: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: folder) }
        Files.prepare(folder)
        let url = folder.appendingPathComponent("signals.json")
        Files.write(Signals(spoken: .match, gps: .unknown, checkIn: .mismatch), to: url)
        XCTAssertEqual(Files.read(Signals.self, from: url), Signals(spoken: .match, gps: .unknown, checkIn: .mismatch))
        XCTAssertNil(Files.read(Signals.self, from: folder.appendingPathComponent("missing.json")))
        XCTAssertEqual(Files.fileNames(in: folder), ["signals.json"], "no temporary file is left beside it")
    }

    /// The app's own Info.plist, as built: it asks for the camera and the
    /// microphone and never for the photo library.
    func testTheBuiltAppAsksForNoPhotoLibraryAccess() {
        let info = Bundle.main.infoDictionary ?? [:]
        XCTAssertNotNil(info["NSCameraUsageDescription"], "these tests run inside the app, so this is the app's Info.plist")
        XCTAssertNotNil(info["NSMicrophoneUsageDescription"])
        XCTAssertNil(info["NSPhotoLibraryUsageDescription"], "videos never go into Photos")
        XCTAssertNil(info["NSPhotoLibraryAddUsageDescription"], "videos never go into Photos")
        XCTAssertNil(info["UIFileSharingEnabled"], "the app's files are not shown in the Files app or to a computer")
        XCTAssertNil(info["LSSupportsOpeningDocumentsInPlace"])
    }
}
