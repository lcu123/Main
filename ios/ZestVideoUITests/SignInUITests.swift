import XCTest

/// Signing in and out: the mobile number, the texted code, the email way,
/// the links, and every button on those two screens.
final class SignInUITests: ZestUITestCase {
    /// SEND MY CODE with nothing typed, with half a number, and with too
    /// many digits: a plain message each time, and nothing is sent.
    func testEmptyShortAndLongNumbersAreRefused() {
        launch()
        let field = app.textFields["contact-field"]
        waitFor(field, "the mobile number field", timeout: 60)

        tap(button("SEND MY CODE"), "SEND MY CODE")
        waitForText("Type your mobile number first.")

        field.tap()
        field.typeText("916")
        tap(button("SEND MY CODE"), "SEND MY CODE")
        waitForText("Type all 10 digits of your mobile number.")

        // Ten more keys: thirteen digits in all. Never silently cut to ten.
        field.typeText("5550100999")
        XCTAssertEqual(field.value as? String, "9165550100999", "The field keeps every key that was typed.")
        // An edit takes the message down. While it is up it pushes SEND MY
        // CODE down the page, on most phones under the keyboard.
        XCTAssertFalse(hasText("Type all 10 digits"), "The message about the number must go once the number is changed.")
        tap(button("SEND MY CODE"), "SEND MY CODE")
        waitForText("That is more than 10 digits.")

        XCTAssertEqual(screenTitle, "Zest Video", "Still on the first sign-in step.")
        XCTAssertTrue(server.state().codesRequested.isEmpty, "No code may be asked for without a whole number.")
    }

    /// The server will not send a code ("The server had a problem."). The
    /// number was fine, so the way on is SEND MY CODE again; and with a
    /// message on the page that button is under the keyboard on most
    /// phones. So the keyboard is put away: the message is whole on the
    /// page, both buttons are back at the foot of the screen with nothing
    /// to slide, and the next tap sends the same number.
    func testAfterAServerMessageSendMyCodeIsBackInReach() {
        launch()
        typeMobile()
        // Every call is refused until the switch is put back.
        server.set(["failNext": 100_000])
        tap(button("SEND MY CODE"), "SEND MY CODE")
        waitForText("The server had a problem.")
        waitUntil("the keyboard was put away after the server's message", timeout: 10) {
            !app.keyboards.firstMatch.exists
        }
        // Let the page finish changing shape.
        pause(1)

        let window = app.windows.firstMatch.frame
        let page = app.scrollViews.firstMatch.frame
        let send = button("SEND MY CODE")
        let message = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "The server had a problem.")).firstMatch
        XCTAssertTrue(send.exists, "SEND MY CODE is still there after the server's message.")
        XCTAssertTrue(message.exists, "The server's message is on the page.")
        let sendFrame = send.frame, messageFrame = message.frame
        let places = "The page is at y \(Int(page.minY))...\(Int(page.maxY)), the message at y \(Int(messageFrame.minY))...\(Int(messageFrame.maxY)), SEND MY CODE at y \(Int(sendFrame.minY))...\(Int(sendFrame.maxY)), on a screen \(Int(window.height)) tall."
        XCTAssertTrue(
            page.insetBy(dx: -1.5, dy: -1.5).contains(messageFrame),
            "The server's message must be whole on the page, between the top bar and the buttons. \(places)"
        )
        XCTAssertTrue(
            sendFrame.minY >= page.maxY - 1.5 && window.insetBy(dx: -1.5, dy: -1.5).contains(sendFrame) && send.isHittable,
            "After a message from the server SEND MY CODE must be back at the foot of the screen, whole and tappable, with nothing to slide. \(places)"
        )
        let typed = (app.textFields["contact-field"].value as? String) ?? ""
        XCTAssertEqual(typed, Self.mobile, "The number is still in the field.")
        XCTAssertTrue(server.state().codesRequested.isEmpty, "The server refused: no code is on its way.")

        server.set(["failNext": 0])
        tap(send, "SEND MY CODE")
        waitForTitle("Type your code")
        XCTAssertEqual(server.state().codesRequested, [Self.mobile], "The same number goes again, once.")
    }

    /// The bug the first run of these tests found: the field used to regroup
    /// its own text as keys arrived, and lost digits. Every key is sent as
    /// its own event with no pause, for the number and for the code. The
    /// server must get exactly the ten digits, and check the code once.
    func testNumberAndCodeTypedOneKeyAtATimeArriveWhole() {
        launch()
        typeMobile(oneKeyAtATime: true)
        waitForLabel(element("contact-preview"), "the number read back under the field", contains: "(916) 555-0100", timeout: 10)
        tap(button("SEND MY CODE"), "SEND MY CODE")
        waitForTitle("Type your code")
        XCTAssertEqual(server.state().codesRequested, [Self.mobile], "The server gets all ten digits, in order.")

        typeCode(Self.code, oneKeyAtATime: true)
        waitForRoute()
        pause(1)
        let seen = server.state()
        XCTAssertEqual(seen.logins, 1)
        XCTAssertEqual(verifyCalls, 1, "Six digits typed fast are checked exactly once.")
        let sent = seen.requests.first { ($0["path"] as? String) == "/v1/auth/verify" }?["body"] as? [String: Any]
        XCTAssertEqual(sent?["code"] as? String, Self.code, "The server gets exactly the six digits that were typed.")
        XCTAssertEqual(sent?["contact"] as? String, Self.mobile)
    }

    /// What the owner's own phone showed and no test had looked at: the two
    /// sign-in steps WITH THE KEYBOARD UP, which is how a tech always has
    /// them. Two big buttons pinned over the keyboard left a strip so short
    /// that half of the field and the whole line under it were behind them.
    ///
    /// On each step the question, the whole field (or all six code boxes)
    /// and the line under it must be above the keyboard and under no
    /// button: on step one with nothing typed, on step one with a number
    /// typed (the number read back is then on the page too), and on step
    /// two. The buttons must still be there, full size, and tappable.
    ///
    /// Skipped, in capitals in the log, when the simulator shows no
    /// keyboard: there is then nothing to look at.
    func testNothingIsUnderTheKeyboardOrAButtonWhileTyping() throws {
        launch()
        let field = app.textFields["contact-field"]
        waitFor(field, "the mobile number field", timeout: 60)
        // The app puts the cursor in the field by itself: no tap here.
        var keyboard = try keyboardOrSkip(for: field, on: "the first sign-in step")

        let question = ("the question \"Type your mobile number.\"", textLine("Type your mobile number."))
        let help = ("the help line \"We will text you a 6-digit code.\"", textLine("We will text you a 6-digit code."))
        var problems = typingProblems(
            on: "step one, nothing typed",
            seen: [question, ("the mobile number field", field), help],
            buttons: [], keyboard: keyboard
        )

        // With a number in the field the page is a line longer.
        typeMobile()
        let readBack = element("contact-preview")
        waitForLabel(readBack, "the number read back under the field", contains: "(916) 555-0100", timeout: 10)
        // (The phone's suggestion bar may have come or gone with the first key.)
        keyboard = keyboardOnScreen(wait: 4) ?? keyboard
        problems += typingProblems(
            on: "step one, the number typed",
            seen: [question, ("the mobile number field", field), ("the number read back under it", readBack), help],
            buttons: ["SEND MY CODE", "USE EMAIL INSTEAD"], keyboard: keyboard
        )

        tap(button("SEND MY CODE"), "SEND MY CODE")
        waitForTitle("Type your code")
        let boxes = app.textFields["code-field"]
        waitFor(boxes, "the code boxes")
        let pad: CGRect
        do {
            pad = try keyboardOrSkip(for: boxes, on: "the code step")
        } catch {
            // Step one was looked at: what it showed is not lost in a skip.
            if !problems.isEmpty {
                XCTFail(Self.numbered(problems, "on the first sign-in step with the keyboard up"))
            }
            throw error
        }
        problems += typingProblems(
            on: "step two, the code",
            seen: [
                ("the question \"We texted a 6-digit code to your phone.\"", textLine("We texted a 6-digit code to your phone.")),
                ("the six code boxes", boxes),
                ("the help line \"No code? Ask the office to check your mobile number.\"", textLine("No code? Ask the office to check your mobile number.")),
            ],
            buttons: ["SEND A NEW CODE", "BACK"], keyboard: pad
        )
        if !problems.isEmpty {
            XCTFail(Self.numbered(problems, "on the sign-in steps with the keyboard up"))
        }

        // Moving the page to reach a button pressed nothing on the way: one
        // code was asked for, by the one tap on SEND MY CODE.
        XCTAssertEqual(screenTitle, "Type your code", "Still on the code step.")
        XCTAssertEqual(server.state().codesRequested, [Self.mobile], "Scrolling the page with the keyboard up must not press a button.")
        XCTAssertEqual(server.state().logins, 0)
    }

    private static func numbered(_ problems: [String], _ place: String) -> String {
        let list = problems.enumerated().map { "  \($0.offset + 1). \($0.element)" }.joined(separator: "\n")
        return "\(problems.count) problem(s) \(place):\n\(list)"
    }

    /// A number with the country code in front (as the phone's own number
    /// suggestion gives it) goes to the server as ten digits.
    func testCountryCodeInFrontIsSentAsTenDigits() {
        launch()
        typeMobile("1" + Self.mobile)
        waitForLabel(element("contact-preview"), "the number read back under the field", contains: "(916) 555-0100", timeout: 10)
        tap(button("SEND MY CODE"), "SEND MY CODE")
        waitForTitle("Type your code")
        XCTAssertEqual(server.state().codesRequested, [Self.mobile])

        // The code is checked against the number it was sent to.
        typeCode(Self.code)
        waitForRoute()
        XCTAssertEqual(server.state().logins, 1)
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
        XCTAssertEqual(verifyCalls, 2, "One check for the wrong code and one for the right one: no more.")
    }

    /// Right after signing in, with a slow server: the list says it is
    /// loading, the tech can still start a video, and the stops then show
    /// by themselves. (Here, not with the route tests, because it needs the
    /// moment just after the code is typed.)
    func testRouteSaysItIsLoadingRightAfterSignIn() {
        server.set(["todayDelay": 12])
        launch()
        typeMobileAndSend()
        typeCode(Self.code)

        waitFor(element("loading-route"), "the \"Getting your stops…\" line", timeout: 20)
        XCTAssertFalse(stop(Self.smith).exists, "No stops can show before the server has answered.")
        XCTAssertTrue(app.buttons["not-on-my-list"].exists, "STOP NOT ON MY LIST is there while the list loads.")

        answerLocationQuestion()
        waitFor(stop(Self.smith), "the stops, once the server answered", timeout: 40)
        XCTAssertFalse(element("loading-route").exists, "The loading line goes once the stops are in.")
        XCTAssertFalse(hasText("No signal"), "A slow answer is not \"no signal\".")
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
        launch()
        signIn()
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
