import Foundation

/// Where the app keeps things on the phone.
///
/// Everything lives in the app's private Application Support folder: never in
/// Photos, left out of iCloud backup, and protected until the phone's first
/// unlock (plain "complete" protection would stop uploads while it is locked).
enum Files {
    /// `Application Support/ZestVideo`.
    static let root: URL = {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSTemporaryDirectory())
        return prepare(base.appendingPathComponent("ZestVideo", isDirectory: true))
    }()

    /// `Application Support/ZestVideo/Videos`: clips waiting to send.
    static let videos: URL = prepare(root.appendingPathComponent("Videos", isDirectory: true))

    /// How the app's folders and video files are protected: unreadable
    /// until the phone has been unlocked once after a restart, readable from
    /// then on, so an upload can carry on with the phone locked in a pocket.
    static let protection = FileProtectionType.completeUntilFirstUserAuthentication

    /// Creates a private folder: excluded from backup, readable while locked.
    @discardableResult
    static func prepare(_ directory: URL) -> URL {
        let manager = FileManager.default
        try? manager.createDirectory(at: directory, withIntermediateDirectories: true)
        try? manager.setAttributes(
            [.protectionKey: protection],
            ofItemAtPath: directory.path
        )
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        var mutable = directory
        try? mutable.setResourceValues(values)
        return directory
    }

    static func video(_ fileName: String) -> URL {
        videos.appendingPathComponent(fileName, isDirectory: false)
    }

    static func exists(_ url: URL) -> Bool {
        FileManager.default.fileExists(atPath: url.path)
    }

    static func size(of url: URL) -> Int64 {
        let values = try? url.resourceValues(forKeys: [.fileSizeKey])
        return Int64(values?.fileSize ?? 0)
    }

    static func remove(_ url: URL) {
        try? FileManager.default.removeItem(at: url)
    }

    /// Lets the upload service read the file while the phone is locked.
    static func protect(_ url: URL) {
        try? FileManager.default.setAttributes(
            [.protectionKey: protection],
            ofItemAtPath: url.path
        )
    }

    /// Total size of everything in a folder: what the app is holding.
    static func totalBytes(in directory: URL) -> Int64 {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: directory.path)) ?? []
        return names.reduce(Int64(0)) { $0 + size(of: directory.appendingPathComponent($1)) }
    }

    static func fileNames(in directory: URL) -> [String] {
        (try? FileManager.default.contentsOfDirectory(atPath: directory.path)) ?? []
    }

    // MARK: Small JSON files

    static func write<T: Encodable>(_ value: T, to url: URL) {
        guard let data = try? ZestJSONBridge.encoder.encode(value) else { return }
        try? data.write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    }

    static func read<T: Decodable>(_ type: T.Type, from url: URL) -> T? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? ZestJSONBridge.decoder.decode(type, from: data)
    }
}
