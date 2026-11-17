// swift-tools-version:5.9
import PackageDescription

// The app's camera service, run on Linux against a capture session a script
// steers. Not part of the app: nothing here is in the Xcode project. See
// run.sh, and "The camera stops with no word" in ios/README.md.
//
// LINUX ONLY. Two targets are stand-ins named after Apple's own frameworks
// (so that CameraService.swift compiles unchanged); on a Mac the real ones
// are in the way.
let package = Package(
    name: "CameraHarness",
    dependencies: [
        .package(path: "../ZestCore"),
    ],
    targets: [
        .target(name: "AVFoundation", path: "Sources/AVFoundation"),
        .target(name: "UIKit", path: "Sources/UIKit"),
        // CameraService.swift as a phone compiles it.
        .executableTarget(
            name: "Phone",
            dependencies: ["AVFoundation", "UIKit", .product(name: "ZestCore", package: "ZestCore")],
            path: "Sources/Phone"
        ),
        // The same file with its simulator-only code switched on (the
        // stand-in recorder the UI tests use).
        .executableTarget(
            name: "Simulator",
            dependencies: ["AVFoundation", "UIKit", .product(name: "ZestCore", package: "ZestCore")],
            path: "Sources/Simulator"
        ),
    ],
    swiftLanguageVersions: [.v5]
)
