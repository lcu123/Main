// swift-tools-version:5.9
import PackageDescription

// Pure logic for Zest Video: no UIKit, SwiftUI or AVFoundation, so it builds
// and tests on Linux as well as in the app.
let package = Package(
    name: "ZestCore",
    platforms: [.iOS(.v17), .macOS(.v13)],
    products: [
        .library(name: "ZestCore", targets: ["ZestCore"]),
    ],
    targets: [
        .target(name: "ZestCore"),
        .testTarget(name: "ZestCoreTests", dependencies: ["ZestCore"]),
    ]
)
