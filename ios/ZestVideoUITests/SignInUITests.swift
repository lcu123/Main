import XCTest

/// Signing in and out: the mobile number, the texted code, the email way,
/// the links, and every button on those two screens.
final class SignInUITests: ZestUITestCase {
    /// SEND MY CODE with nothing typed, then with half a number: a plain
    /// message each time, and nothing is sent to the server.
    func testEmptyAndShortNumbersAreRefused() {
        launch()
        let field = app.textFields["contact-field"]
        waitFor(field, "the mobile number field", timeout: 60)

        tap(button("SEND MY CODE"), "SEND MY CODE")
        waitForText("Type your mobile number first.")

        field.tap()
        field.typeText("916")
        tap(button("SEND MY CODE"), "SEND MY CODE")
        waitForText("Type all 10 digits of your mobile number.")

        XCTAssertEqual(screenTitle, "Zest Video", "Still on the first sign-in step.")
        XCTAssertTrue(server.state().codesRequested.isEmpty, "No code may be asked for without a whole number.")
    }

    /// A wrong code says so and how many tries are left; the right code signs in.
    func testWrongCodeShowsMessageThenRightCodeSignsIn() {
        launch()
        typeMobileAndSend()
        XCTAssertEqual(server.state().codesRequested, [Self.mobile], "The server gets the ten plain digits.")
        waitForText("We texted a 6-digit code to your phone.")

        typeCode("000000")
        waitForText("That code is not right. 4 tries left.")
        XCTAssertEqual(screenTitle, "Type your code", "A wrong code stays on the code screen.")
        XCTAssertEqual(server.state().logins, 0)

        typeCode(Self.code)
        waitForRoute()
        XCTAssertEqual(barTitle, "My Route")
        XCTAssertTrue(routeHeader.hasPrefix("Sam."), "The header names the tech. It says: \(routeHeader)")
        XCTAssertEqual(server.state().logins, 1)
    }

    /// USE EMAIL INSTEAD sends the code to an email; BACK and USE MOBILE
    /// NUMBER INSTEAD lead back, and the mobile way still signs in.
    func testEmailInsteadAndBack() {
        launch()
        waitFor(app.textFields["contact-field"], "the mobile number field", timeout: 60)
        waitForText("Type your mobile number.")

        // A double tap changes to email once; it must not flip straight back.
        doubleTap(button("USE EMAIL INSTEAD"), "USE EMAIL INSTEAD")
        waitForText("Type your email.")
        pause(1)
        XCTAssertTrue(button("USE MOBILE NUMBER INSTEAD").exists, "A double tap must leave it on email.")
        let field = app.textFields["contact-field"]
        field.tap()
        field.typeText("sam@example.com")
        tap(button("SEND MY CODE"), "SEND MY CODE")
        waitForTitle("Type your code")
        waitForText("We emailed a 6-digit code.")
        XCTAssertEqual(server.state().codesRequested, ["sam@example.com"])

        tap(button("BACK"), "BACK")
        waitForTitle("Zest Video")
        tap(button("USE MOBILE NUMBER INSTEAD"), "USE MOBILE NUMBER INSTEAD")
        waitForText("Type your mobile number.")
        XCTAssertTrue(button("USE EMAIL INSTEAD").exists)

        signIn()
        XCTAssertEqual(server.state().codesRequested, ["sam@example.com", Self.mobile])
    }

    /// SEND A NEW CODE asks the server again; the back arrow and the big
    /// BACK button both return to the number, which is still there.
    func testSendANewCodeAndBack() {
        launch()
        typeMobileAndSend()

        tap(button("SEND A NEW CODE"), "SEND A NEW CODE")
        waitUntil("the server was asked for a second code") { server.state().codesRequested.count == 2 }
        XCTAssertEqual(screenTitle, "Type your code")

        tap(app.buttons["bar-back"], "the back arrow")
        waitForTitle("Zest Video")
        let typed = (app.textFields["contact-field"].value as? String) ?? ""
        XCTAssertEqual(typed.filter { $0.isNumber }, Self.mobile, "The number is still in the field after going back.")

        tap(button("SEND MY CODE"), "SEND MY CODE")
        waitForTitle("Type your code")
        tap(button("BACK"), "BACK")
        waitForTitle("Zest Video")
        XCTAssertEqual(server.state().codesRequested.count, 3)
        XCTAssertEqual(server.state().logins, 0)
    }

    /// Two quick taps on SEND MY CODE, with a slow server, ask for one code.
    func testSendMyCodeCannotBeDoubleTapped() {
        server.set(["requestCodeDelay": 2])
        launch()
        typeMobile()
        doubleTap(button("SEND MY CODE"), "SEND MY CODE")
        waitForTitle("Type your code", timeout: 25)
        pause(1.5)
        XCTAssertEqual(server.state().codesRequested.count, 1, "A double tap must send one code, not two.")
    }

    /// Terms and Privacy are on the sign-in screen and each opens Safari.
    func testTermsAndPrivacyOpen() {
        launch()
        waitFor(app.textFields["contact-field"], "the mobile number field", timeout: 60)
        waitForText("By signing in you agree to the Terms and Privacy Policy.")
        let safari = XCUIApplication(bundleIdentifier: "com.apple.mobilesafari")

        tap(app.buttons["link-terms"], "the Terms link")
        XCTAssertTrue(safari.wait(for: .runningForeground, timeout: 25), "Terms should open in Safari.")
        app.activate()
        XCTAssertTrue(app.wait(for: .runningForeground, timeout: 15))

        tap(app.buttons["link-privacy"], "the Privacy link")
        XCTAssertTrue(safari.wait(for: .runningForeground, timeout: 25), "Privacy should open in Safari.")

        // Not a failure if Safari was slow to load: just say what the server saw.
        let seen = server.state()
        print("NOTE Safari asked the server for /terms \(seen.calls("GET", exactly: "/terms")) time(s), /privacy \(seen.calls("GET", exactly: "/privacy")) time(s).")
        app.activate()
    }

    /// Sign out asks first. NO, STAY keeps the session; YES, SIGN OUT (even
    /// tapped twice) tells the server once, and a restart stays signed out.
    func testSignOutWithConfirm() {
        launchAndSignIn()
        tap(app.buttons["tab-videos"], "the My Videos tab")
        waitForBar("My Videos")
        waitForText("No videos yet today.")

        tap(app.buttons["link-signout"], "Sign out")
        waitForTitle("Sign out?")
        tap(button("NO, STAY"), "NO, STAY")
        waitForBar("My Videos")
        XCTAssertEqual(server.state().logouts, 0)

        tap(app.buttons["link-signout"], "Sign out")
        waitForTitle("Sign out?")
        doubleTap(button("YES, SIGN OUT"), "YES, SIGN OUT")
        waitFor(app.textFields["contact-field"], "the sign-in screen", timeout: 20)
        waitUntil("the server was told about the sign-out") { server.state().logouts >= 1 }
        pause(1)
        XCTAssertEqual(server.state().logouts, 1, "A double tap must sign out once.")

        relaunch()
        waitFor(app.textFields["contact-field"], "the sign-in screen after a restart", timeout: 60)
        XCTAssertFalse(app.buttons["tab-route"].exists, "Signed out must stay signed out after a restart.")
    }
}
