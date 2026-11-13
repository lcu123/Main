import XCTest

/// "Check free space before recording. Warn under 500 MB free and block
/// under 200 MB." These are the techs' own phones.
///
/// Every way into the camera passes the same check: THIS IS IT, RECORD on a
/// stop that is not on the list, RECORD AGAIN on the review screen, and
/// RECORD AGAIN on a stop's saved video. The tests say how much free space
/// the phone "has" (`freeMB`); the check that reads it is the app's real one.
final class StorageUITests: ZestUITestCase {
    private func expectFullPhone(_ from: String, file: StaticString = #filePath, line: UInt = #line) {
        waitForTitle("Your phone is full", file: file, line: line)
        XCTAssertTrue(hasText("There is no room to record a video."), file: file, line: line)
        XCTAssertFalse(app.buttons["record-button"].exists, "\(from): a full phone must not reach the camera.", file: file, line: line)
        XCTAssertFalse(button("RECORD ANYWAY").exists, "\(from): there is no \"anyway\" on a full phone.", file: file, line: line)
        // TRY AGAIN looks again; the phone is still full.
        tap(button("TRY AGAIN"), "TRY AGAIN", file: file, line: line)
        pause(1)
        XCTAssertEqual(screenTitle, "Your phone is full", "\(from): TRY AGAIN on a phone that is still full stays put.", file: file, line: line)
        XCTAssertFalse(app.buttons["record-button"].exists, file: file, line: line)
    }

    /// Under 200 MB, from the confirm screen: THIS IS IT lands on "Your phone
    /// is full" and there is no way on to the camera. The same for a stop
    /// that is not on the list.
    func testAFullPhoneNeverReachesTheCameraFromTheConfirmScreen() {
        freeMB = 150
        launchSignedIn()

        tap(stop(Self.smith), "the first stop")
        waitForTitle("Is this the stop?")
        tap(button("THIS IS IT"), "THIS IS IT")
        expectFullPhone("THIS IS IT")
        tap(button("BACK"), "BACK")
        waitForBar("My Route")

        // The arrow in the top bar goes back too.
        tap(stop(Self.smith), "the first stop again")
        waitForTitle("Is this the stop?")
        tap(button("THIS IS IT"), "THIS IS IT")
        waitForTitle("Your phone is full")
        tap(app.buttons["bar-back"], "the back arrow")
        waitForBar("My Route")

        tap(app.buttons["not-on-my-list"], "STOP NOT ON MY LIST")
        waitForTitle("Not on my list")
        tap(button("RECORD"), "RECORD")
        expectFullPhone("RECORD for a stop not on the list")
        tap(button("BACK"), "BACK")
        waitForBar("My Route")

        XCTAssertFalse(app.buttons["draft-card"].exists, "Nothing was recorded.")
        XCTAssertTrue(server.state().videos.isEmpty, "Nothing was sent.")
    }

    /// Under 200 MB, from both RECORD AGAIN buttons. The clip already made is
    /// not thrown away on the way to a camera that cannot be reached: it is
    /// still there to save, and the saved video is not replaced.
    func testAFullPhoneBlocksRecordAgainAndKeepsTheClipAlreadyMade() {
        // An hour to change a video: this test restarts the app and waits
        // on several screens before it looks for CHANGE VIDEO.
        server.set(["holdMinutes": 60])
        launchSignedIn()
        openRecorder(for: Self.smith)
        recordClip()
        waitForTitle("Save this video?")

        // The phone fills up (photos, another app) while the clip waits.
        kill()
        freeMB = 150
        relaunch()
        let card = app.buttons["draft-card"]
        waitFor(card, "the \"1 video not saved yet\" card", timeout: 60)
        tap(card, "the \"1 video not saved yet\" card")
        waitForTitle("Save this video?")

        tap(button("RECORD AGAIN"), "RECORD AGAIN")
        expectFullPhone("RECORD AGAIN on the review screen")
        tap(button("BACK"), "BACK")
        waitForTitle("Save this video?")
        XCTAssertTrue(hasText("Smith, Jane"), "The clip is still there, for the same stop.")
        XCTAssertTrue(server.state().videos.isEmpty)

        // It can still be saved: saving needs no new room.
        saveClip()
        waitForHeld()
        let saved = theOnlyVideo()
        XCTAssertEqual(saved.customerId, Self.smith)
        XCTAssertEqual(saved.completes, [200])

        // RECORD AGAIN on the saved video is stopped the same way, and the
        // saved video is left as it is.
        waitForLabel(stop(Self.smith), "the stop's card", contains: "Change Video")
        tap(stop(Self.smith), "the stop with a video")
        waitForBar("Video")
        tap(button("RECORD AGAIN"), "RECORD AGAIN")
        expectFullPhone("RECORD AGAIN on a saved video")
        tap(button("BACK"), "BACK")
        waitForBar("My Route")
        pause(2)
        let seen = server.state()
        XCTAssertEqual(seen.videos.count, 1, "No second video was made.")
        XCTAssertEqual(seen.videos.first?.state, "held", "The saved video was not replaced or deleted.")
        XCTAssertEqual(seen.videos.first?.deletes, 0)
        waitForLabel(stop(Self.smith), "the stop's card", contains: "Change Video")
        XCTAssertFalse(app.buttons["draft-card"].exists)
    }

    /// Under 500 MB: one warning, and RECORD ANYWAY reaches the camera, from
    /// the confirm screen and from RECORD AGAIN. BACK records nothing.
    func testAnAlmostFullPhoneWarnsAndRecordAnywayReachesTheCamera() {
        freeMB = 350
        launchSignedIn()

        tap(stop(Self.smith), "the first stop")
        waitForTitle("Is this the stop?")
        tap(button("THIS IS IT"), "THIS IS IT")
        waitForTitle("Your phone is almost full")
        XCTAssertTrue(hasText("You can still record this video."))
        XCTAssertFalse(app.buttons["record-button"].exists, "The warning comes before the camera.")
        tap(button("BACK"), "BACK")
        waitForBar("My Route")
        XCTAssertFalse(app.buttons["draft-card"].exists)

        tap(stop(Self.smith), "the first stop again")
        waitForTitle("Is this the stop?")
        tap(button("THIS IS IT"), "THIS IS IT")
        waitForTitle("Your phone is almost full")
        tap(button("RECORD ANYWAY"), "RECORD ANYWAY")
        waitFor(app.buttons["record-button"], "the red record button")
        XCTAssertTrue(hasText("Smith, Jane"), "The camera is for the stop that was picked.")
        recordClip()

        // RECORD AGAIN warns the same way; BACK keeps the clip.
        tap(button("RECORD AGAIN"), "RECORD AGAIN")
        waitForTitle("Your phone is almost full")
        tap(button("BACK"), "BACK")
        waitForTitle("Save this video?")
        tap(button("RECORD AGAIN"), "RECORD AGAIN")
        waitForTitle("Your phone is almost full")
        tap(button("RECORD ANYWAY"), "RECORD ANYWAY")
        waitFor(app.buttons["record-button"], "the record screen again")
        recordClip()
        saveClip()
        waitForHeld()

        let seen = server.state()
        XCTAssertEqual(seen.videos.count, 1, "The first clip was never sent; the second was.")
        XCTAssertEqual(seen.registerCalls(), 1)
        XCTAssertEqual(theOnlyVideo().customerId, Self.smith)
    }
}
