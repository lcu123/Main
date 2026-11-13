import XCTest
@testable import ZestVideo

/// The settings of the session that carries videos off a real phone. Each
/// one is a promise from the brief; a later edit must not flip one unseen.
/// (On the main actor, like the upload manager itself.)
final class UploadSessionTests: XCTestCase {
    @MainActor
    private func configuration() -> URLSessionConfiguration {
        // (A configuration only: no session is made, so nothing is taken
        // away from the app's own.)
        UploadManager.backgroundConfiguration(identifier: "com.zestlawnpest.video.tests.\(UUID().uuidString)")
    }

    @MainActor
    func testItIsABackgroundSessionWithTheIdentifierAsked() {
        let made = UploadManager.backgroundConfiguration(identifier: "com.zestlawnpest.video.tests.identifier")
        XCTAssertEqual(made.identifier, "com.zestlawnpest.video.tests.identifier",
                       "a background session: iOS carries the upload on while the app is closed")
        XCTAssertEqual(UploadManager.sessionIdentifier, "com.zestlawnpest.video.uploads",
                       "the name iOS knows the uploads by must not change between versions, or uploads in flight are orphaned")
    }

    @MainActor
    func testUploadsStartNowNotWhenIOSFindsItConvenient() {
        XCTAssertFalse(configuration().isDiscretionary, "discretionary uploads wait for Wi-Fi and a charger")
    }

    @MainActor
    func testUploadsGoOverCellularIncludingLowDataMode() {
        let made = configuration()
        XCTAssertTrue(made.allowsCellularAccess, "the techs are on cellular all day")
        XCTAssertTrue(made.allowsExpensiveNetworkAccess, "cellular and hotspots count as expensive")
        XCTAssertTrue(made.allowsConstrainedNetworkAccess, "Low Data Mode must not hold videos back")
    }

    @MainActor
    func testNoSignalIsWaitedOutNotFailed() {
        let made = configuration()
        XCTAssertTrue(made.waitsForConnectivity, "with no signal the upload waits instead of failing")
        XCTAssertEqual(made.timeoutIntervalForResource, 24 * 60 * 60, "for up to a day")
    }

    @MainActor
    func testIOSWakesTheAppWhenAnUploadFinishes() {
        XCTAssertTrue(configuration().sessionSendsLaunchEvents, "so the server is told the file arrived, and the phone's copy is deleted")
    }

    @MainActor
    func testAtMostTwoUploadsAtATime() {
        XCTAssertEqual(configuration().httpMaximumConnectionsPerHost, 2)
    }
}
