import XCTest

/// What the simulator does with the REAL background upload session.
///
/// Every other test uploads through an ordinary URLSession (see
/// `UITestMode.swift`), so that the queue, the retries and the recovery after
/// a kill are tested whatever the simulator's background transfer service
/// does. This one test turns the real background session on and reports what
/// happens. It is here to tell us, not to judge: if the upload never arrives
/// the test is SKIPPED, with the error the app recorded, and the run stays
/// green. If it does arrive, it has to be right.
///
/// Either way this is no proof for a real phone. Only a real phone shows iOS
/// carrying an upload on while the app is closed.
final class BackgroundUploadUITests: ZestUITestCase {
    func testBackgroundSessionUploadInTheSimulator() throws {
        launchSignedIn(backgroundUploads: true)
        recordAndSave(for: Self.smith)

        var arrived = false
        let deadline = Date().addingTimeInterval(75)
        while Date() < deadline {
            if server.state().videos.contains(where: { $0.isHeld }) {
                arrived = true
                break
            }
            pause(1)
        }

        let seen = server.state()
        let said = seen.notes.isEmpty ? "(the app made no upload task)" : seen.notes.joined(separator: " | ")
        let video = seen.videos.first.map { $0.summary } ?? "(no video was registered)"
        let verdict = arrived ? "ARRIVED" : "DID NOT ARRIVE"
        let report = "background session upload \(verdict). Server: \(video). The app said: \(said)"
        print("NOTE \(shortName): \(report)")
        saveNote(named: shortName + ".info", report)

        guard arrived else {
            throw XCTSkip("Informational only. In this simulator the \(report)")
        }
        let sent = theOnlyVideo()
        XCTAssertEqual(sent.customerId, Self.smith)
        XCTAssertEqual(sent.bytes, sent.sizeSent, "Every byte arrived: \(sent.summary)")
        XCTAssertEqual(sent.goodCompletes, 1, "Confirmed once: \(sent.summary)")
        XCTAssertTrue(seen.notes.contains { $0.contains("background session") }, "It really was the background session. The app said: \(said)")
        waitForLabel(element("route-header"), "the header", contains: "Sent 2 of 5", timeout: 40)
    }
}
