import CoreLocation
import XCTest

/// The base of every UI test: a clean app, a clean fake server, and the
/// handful of steps the tests share.
///
/// The tests drive the real app in the simulator, launched in its UI-test
/// mode (see `UITestMode.swift` in the app): real sign-in, real stores on
/// disk, real upload manager, real navigation. Each test checks BOTH what is
/// on the screen and what the fake server saw.
///
/// When a test fails it prints the test's name, the reason, what was on the
/// screen and the server's request log, and saves a picture of the screen, so
/// a failure in CI can be read without running anything.
class ZestUITestCase: XCTestCase {
    let app = XCUIApplication()
    let server = FakeServer()
    let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")

    /// On the first customer's lawn (the fake server's stop 201).
    static let near = CLLocation(latitude: 38.67800, longitude: -121.17600)

    /// The fake server's customers.
    static let smith = 201      // "Smith, Jane", 123 Main St: checked in and out, where the phone is
    static let johnson = 202    // "Johnson, Robert", 125 Main St: next door, no check-in
    static let garcia = 203     // "Garcia, Maria", 48 Oak Ave: about 375 m away
    static let business = 204   // "Green Valley HOA": kilometres away
    static let locked = 205     // "Wilson, Tom": its video already went to the customer

    static let mobile = "9165550100"
    static let code = "123456"

    /// What the speech check "hears" unless a test says otherwise: right for stop 201.
    static let smithWords = "Hi this is Sam at the Smith home on Main Street"

    /// Failure pictures and notes are also written here, where CI picks them up.
    static let failureFolder = ProcessInfo.processInfo.environment["ZEST_FAILURE_DIR"] ?? "/tmp/zest-ui-failures"

    private var transcript: String?
    private var reported = false

    /// The phone asks each system question once per simulator: location at
    /// the first sign-in, notifications after the first saved video. The
    /// first time is given longer to show up.
    private static var answeredLocation = false
    private static var answeredNotifications = false

    /// "RecordUITests.testHappyPathRecordSaveSend"
    var shortName: String {
        let method = name.replacingOccurrences(of: "]", with: "").components(separatedBy: " ").last ?? name
        return "\(String(describing: type(of: self))).\(method)"
    }

    // MARK: Set up and tear down

    override func setUpWithError() throws {
        try super.setUpWithError()
        continueAfterFailure = false
        XCTAssertTrue(
            server.isUp,
            "The fake server is not answering on \(server.baseURL). Start it first: python3 ios/Scripts/fake_server.py"
        )
        // Three-second clips keep the tests quick.
        server.reset(test: shortName, config: ["maxSeconds": 3])
        setLocation(Self.near)
        // A system question ("Allow notifications?") must never hang a test.
        addUIInterruptionMonitor(withDescription: "A system question") { alert in
            ZestUITestCase.answer(alert)
        }
    }

    override func tearDownWithError() throws {
        app.terminate()
        try super.tearDownWithError()
    }

    /// Every failure: say which test, why, what was showing and what the
    /// server saw; keep a picture.
    override func record(_ issue: XCTIssue) {
        if !reported {
            reported = true
            let log = server.logText()
            let note = """
            ######## FAILED \(shortName)
            ######## \(issue.compactDescription)
            ######## On screen: \(screenSummary())
            ######## What the server saw:
            \(log)
            ########
            """
            print("\n" + note + "\n")

            let screenshot = XCUIScreen.main.screenshot()
            let picture = XCTAttachment(screenshot: screenshot)
            picture.name = "\(shortName) at the failure"
            picture.lifetime = .keepAlways
            add(picture)
            let text = XCTAttachment(string: note)
            text.name = "What the server saw"
            text.lifetime = .keepAlways
            add(text)

            let folder = URL(fileURLWithPath: Self.failureFolder, isDirectory: true)
            try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            try? screenshot.pngRepresentation.write(to: folder.appendingPathComponent(shortName + ".png"))
            try? Data(note.utf8).write(to: folder.appendingPathComponent(shortName + ".txt"))
        }
        super.record(issue)
    }

    // MARK: Launching, killing, backgrounding

    /// Launches the app in UI-test mode.
    /// - Parameters:
    ///   - reset: wipe everything the app has stored first (a clean install).
    ///     Without it the app starts with whatever it kept.
    ///   - transcript: the words the speech check "hears" in every clip.
    ///     `nil` means it hears nothing (the spoken key is "unknown").
    func launch(reset: Bool = true, transcript: String? = ZestUITestCase.smithWords) {
        if reset { self.transcript = transcript }
        var arguments = ["-ZestUITest", "1"]
        if reset {
            arguments += ["-ZestUITestReset", "1"]
        }
        app.launchArguments = arguments
        var environment = ["ZEST_TEST_SERVER": server.baseURL]
        if let words = self.transcript {
            environment["ZEST_TEST_TRANSCRIPT"] = words
        }
        app.launchEnvironment = environment
        app.launch()
    }

    /// Kills the app with no warning, the way a crash or a dead battery would.
    func kill() {
        app.terminate()
        _ = app.wait(for: .notRunning, timeout: 10)
    }

    /// Starts the app again WITHOUT wiping anything: what it kept is the point.
    func relaunch() {
        launch(reset: false)
    }

    /// Home button, a pause, then back into the app.
    func leaveAndComeBack() {
        XCUIDevice.shared.press(.home)
        pause(2)
        app.activate()
        _ = app.wait(for: .runningForeground, timeout: 15)
    }

    func setLocation(_ location: CLLocation) {
        XCUIDevice.shared.location = XCUILocation(location: location)
    }

    func pause(_ seconds: TimeInterval) {
        RunLoop.current.run(until: Date().addingTimeInterval(seconds))
    }

    // MARK: System questions

    private static func answer(_ alert: XCUIElement) -> Bool {
        for title in ["Allow While Using App", "Allow", "OK", "Allow Once", "Continue"] {
            let button = alert.buttons[title]
            if button.exists {
                button.tap()
                return true
            }
        }
        return false
    }

    /// Answers "Allow" to any system question that is up (location,
    /// notifications).
    /// - Parameter wait: how long to give a question to show up.
    func allowSystemQuestions(wait: TimeInterval = 1) {
        for round in 0..<3 {
            let alert = springboard.alerts.firstMatch
            let showing = (round == 0 && wait > 0) ? alert.waitForExistence(timeout: wait) : alert.exists
            guard showing else { return }
            if !ZestUITestCase.answer(alert) { return }
            pause(0.6)
        }
    }

    // MARK: Finding things

    /// Any element by its identifier.
    func element(_ identifier: String) -> XCUIElement {
        app.descendants(matching: .any).matching(identifier: identifier).firstMatch
    }

    /// A big button, by the words on it ("SAVE").
    func button(_ words: String) -> XCUIElement {
        app.buttons[words]
    }

    /// A stop's card on My Route (or on the pick-a-stop list).
    func stop(_ customerId: Int) -> XCUIElement {
        app.buttons["stop-\(customerId)"]
    }

    /// A video's row on My Videos.
    func videoRow(_ customerId: Int) -> XCUIElement {
        app.buttons["video-row-\(customerId)"]
    }

    /// What an element says, or "" when it is not there.
    func label(of item: XCUIElement) -> String {
        item.exists ? item.label : ""
    }

    /// The short name in the top bar: "My Route", "Review".
    var barTitle: String {
        label(of: element("bar-title"))
    }

    /// The big question at the top of the page: "Save this video?".
    var screenTitle: String {
        label(of: element("screen-title"))
    }

    /// The card at the top of My Route: "Sam. Sent 1 of 5. Waiting 0."
    var routeHeader: String {
        label(of: element("route-header"))
    }

    /// True when something on screen says these words.
    func hasText(_ part: String) -> Bool {
        app.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS[c] %@", part))
            .firstMatch.exists
    }

    /// A few words about what is showing, for failure messages.
    func screenSummary() -> String {
        guard app.state == .runningForeground else { return "(the app is not in front)" }
        let buttons = app.buttons.allElementsBoundByIndex.prefix(16).map { item -> String in
            item.identifier.isEmpty ? item.label : item.identifier
        }
        return "bar \"\(barTitle)\", title \"\(screenTitle)\", buttons \(buttons)"
    }

    // MARK: Waiting

    @discardableResult
    func waitFor(
        _ item: XCUIElement, _ what: String, timeout: TimeInterval = 20,
        file: StaticString = #filePath, line: UInt = #line
    ) -> XCUIElement {
        if !item.waitForExistence(timeout: timeout) {
            XCTFail("Did not find \(what) within \(Int(timeout)) s.", file: file, line: line)
        }
        return item
    }

    /// Polls until `condition` is true. Fails with `what` if it never is.
    func waitUntil(
        _ what: String, timeout: TimeInterval = 30,
        file: StaticString = #filePath, line: UInt = #line, _ condition: () -> Bool
    ) {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return }
            pause(0.4)
        }
        if condition() { return }
        XCTFail("Waited \(Int(timeout)) s but this never happened: \(what).", file: file, line: line)
    }

    func waitForBar(_ title: String, timeout: TimeInterval = 20, file: StaticString = #filePath, line: UInt = #line) {
        waitUntil("the top bar says \"\(title)\"", timeout: timeout, file: file, line: line) { barTitle == title }
    }

    func waitForTitle(_ title: String, timeout: TimeInterval = 20, file: StaticString = #filePath, line: UInt = #line) {
        waitUntil("the screen asks \"\(title)\"", timeout: timeout, file: file, line: line) { screenTitle == title }
    }

    func waitForText(_ part: String, timeout: TimeInterval = 15, file: StaticString = #filePath, line: UInt = #line) {
        waitUntil("the screen says \"\(part)\"", timeout: timeout, file: file, line: line) { hasText(part) }
    }

    /// Waits until an element's words contain `part`.
    func waitForLabel(
        _ item: XCUIElement, _ what: String, contains part: String, timeout: TimeInterval = 30,
        file: StaticString = #filePath, line: UInt = #line
    ) {
        waitUntil("\(what) says \"\(part)\"", timeout: timeout, file: file, line: line) {
            label(of: item).localizedCaseInsensitiveContains(part)
        }
    }

    /// Waits until the server holds exactly `count` whole, confirmed videos.
    func waitForHeld(
        count: Int = 1, timeout: TimeInterval = 60, file: StaticString = #filePath, line: UInt = #line
    ) {
        waitUntil("the server holds \(count) whole, confirmed video(s)", timeout: timeout, file: file, line: line) {
            server.state().videos.filter { $0.isHeld }.count == count
        }
    }

    /// The phone knows where it is: My Route has put the stop it is standing
    /// on first.
    func waitForLocation(file: StaticString = #filePath, line: UInt = #line) {
        waitForLabel(stop(Self.smith), "the first stop", contains: "Closest stop", timeout: 45, file: file, line: line)
    }

    // MARK: Tapping

    /// Waits for it, scrolls it into view if it is off screen, and taps it.
    func tap(
        _ item: XCUIElement, _ what: String, timeout: TimeInterval = 20,
        file: StaticString = #filePath, line: UInt = #line
    ) {
        waitFor(item, what, timeout: timeout, file: file, line: line)
        waitUntilTappable(item)
        bringIntoView(item)
        item.tap()
    }

    /// Two quick taps, as a gloved thumb does by mistake.
    func doubleTap(
        _ item: XCUIElement, _ what: String, timeout: TimeInterval = 20,
        file: StaticString = #filePath, line: UInt = #line
    ) {
        waitFor(item, what, timeout: timeout, file: file, line: line)
        waitUntilTappable(item)
        bringIntoView(item)
        item.doubleTap()
    }

    /// A screen that has just appeared ignores touches for 0.4 s (so the
    /// second tap of a double tap cannot answer it). Give it that moment.
    func waitUntilTappable(_ item: XCUIElement) {
        pause(0.45)
        let deadline = Date().addingTimeInterval(1.5)
        while Date() < deadline, item.exists, !item.isHittable {
            pause(0.1)
        }
        if item.exists && !item.isHittable {
            // Still not: a system question may be sitting over the app.
            allowSystemQuestions(wait: 0)
        }
    }

    /// After a video is saved (or the reminders offer is answered) the phone
    /// may ask about notifications. Say yes.
    func answerFirstSaveQuestion() {
        allowSystemQuestions(wait: Self.answeredNotifications ? 0 : 3)
        Self.answeredNotifications = true
    }

    /// Scrolls the page until the item can be tapped. Does nothing when it
    /// already can be.
    func bringIntoView(_ item: XCUIElement) {
        let screen = app.windows.firstMatch.frame
        guard screen.height > 0 else { return }
        var tries = 0
        while tries < 14, item.exists, !item.isHittable {
            let middle = item.frame.midY
            if middle > screen.height * 0.5 {
                drag(fromY: 0.50, toY: 0.28)    // it is further down: move the page up
            } else if middle < screen.height * 0.2 {
                drag(fromY: 0.28, toY: 0.50)    // it is further up: move the page down
            } else {
                return                          // on screen; something else is in the way
            }
            tries += 1
        }
    }

    private func drag(fromY: CGFloat, toY: CGFloat) {
        let start = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: fromY))
        let end = app.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: toY))
        start.press(forDuration: 0.05, thenDragTo: end)
    }

    // MARK: The steps the tests share

    /// A clean app, signed in, on My Route.
    func launchAndSignIn(
        transcript: String? = ZestUITestCase.smithWords, file: StaticString = #filePath, line: UInt = #line
    ) {
        launch(transcript: transcript)
        signIn(file: file, line: line)
    }

    /// Signs in with the mobile number and the right code, to My Route.
    func signIn(file: StaticString = #filePath, line: UInt = #line) {
        typeMobileAndSend(file: file, line: line)
        typeCode(Self.code, file: file, line: line)
        waitForRoute(file: file, line: line)
    }

    /// Types the mobile number (without sending it).
    func typeMobile(file: StaticString = #filePath, line: UInt = #line) {
        let field = app.textFields["contact-field"]
        waitFor(field, "the mobile number field", timeout: 60, file: file, line: line)
        waitUntilTappable(field)
        field.tap()
        field.typeText(Self.mobile)
        let typed = (field.value as? String) ?? ""
        XCTAssertEqual(
            typed.filter { $0.isNumber }, Self.mobile,
            "The mobile number field holds \"\(typed)\" after typing \(Self.mobile).", file: file, line: line
        )
    }

    func typeMobileAndSend(file: StaticString = #filePath, line: UInt = #line) {
        typeMobile(file: file, line: line)
        tap(button("SEND MY CODE"), "SEND MY CODE", file: file, line: line)
        waitForTitle("Type your code", file: file, line: line)
    }

    func typeCode(_ digits: String, file: StaticString = #filePath, line: UInt = #line) {
        let field = app.textFields["code-field"]
        waitFor(field, "the code boxes", file: file, line: line)
        waitUntilTappable(field)
        field.tap()
        field.typeText(digits)
    }

    /// My Route is showing, with its stops loaded.
    func waitForRoute(file: StaticString = #filePath, line: UInt = #line) {
        waitFor(app.buttons["tab-route"], "My Route (its tab bar)", timeout: 40, file: file, line: line)
        answerLocationQuestion()
        waitFor(stop(Self.smith), "the stops on My Route", timeout: 40, file: file, line: line)
    }

    /// Signing in is when the phone asks about location. Say yes.
    func answerLocationQuestion() {
        allowSystemQuestions(wait: Self.answeredLocation ? 0 : 2.5)
        Self.answeredLocation = true
    }

    /// From My Route: tap a stop, say THIS IS IT, land on the record screen.
    func openRecorder(for customerId: Int, file: StaticString = #filePath, line: UInt = #line) {
        tap(stop(customerId), "the card for stop \(customerId)", file: file, line: line)
        waitForTitle("Is this the stop?", file: file, line: line)
        tap(button("THIS IS IT"), "THIS IS IT", file: file, line: line)
        waitFor(app.buttons["record-button"], "the red record button", file: file, line: line)
    }

    /// On the record screen: record a clip that stops by itself, through to
    /// "Save this video?".
    func recordClip(file: StaticString = #filePath, line: UInt = #line) {
        tap(app.buttons["record-button"], "the red record button", file: file, line: line)
        waitForTitle("Save this video?", timeout: 40, file: file, line: line)
    }

    /// On the review screen: SAVE, through "Saved", back to My Route.
    func saveClip(file: StaticString = #filePath, line: UInt = #line) {
        tap(button("SAVE"), "SAVE", file: file, line: line)
        waitFor(app.buttons["tab-route"], "My Route after SAVE", timeout: 30, file: file, line: line)
        answerFirstSaveQuestion()
    }

    /// Record and save one video for a stop, start to finish.
    func recordAndSave(for customerId: Int, file: StaticString = #filePath, line: UInt = #line) {
        openRecorder(for: customerId, file: file, line: line)
        recordClip(file: file, line: line)
        saveClip(file: file, line: line)
    }

    /// The one video the server should know of. Fails if there is not exactly one.
    func theOnlyVideo(file: StaticString = #filePath, line: UInt = #line) -> ServerVideo {
        let videos = server.state().videos
        XCTAssertEqual(videos.count, 1, "The server should know of exactly one video.", file: file, line: line)
        return videos.first ?? ServerVideo(raw: [:])
    }
}
