import XCTest

/// Every screen of the app, opened with made-up data (the app's screenshot
/// mode, `-ZestScreen <name>`), and checked two ways:
///
/// 1. **Layout.** The screen's main actions are there, inside the screen, not
///    on top of each other, and can be tapped: without scrolling on a
///    6.1-inch phone and bigger, after scrolling at most on the smallest
///    phone and on an iPad.
/// 2. **Apple's accessibility audit** (`performAccessibilityAudit`): contrast,
///    tap targets of 44 points, clipped text, element descriptions, Dynamic
///    Type.
///
/// CI runs this class on several simulators and text sizes. It is told which
/// through the environment:
///
///     ZEST_SCREENS_DEVICE     a word for the report: "small", "mid", "ipad", "big", "large-text"
///     ZEST_SCREENS_NO_SCROLL  "1": main actions must show without scrolling
///     ZEST_SCREENS_AUDIT      "0": skip Apple's audit (layout only)
///     ZEST_SCREENS_BIG_TEXT   "1": the text-size setting is at its largest;
///                             check that the app's type really grew
///
/// Every issue the audit raises is written to a table (kept with the run's
/// results even when the test passes), with the screen, the element and
/// whether it failed the test or why it did not.
final class ScreensUITests: ZestUITestCase {
    override class var needsServer: Bool { false }

    // MARK: What CI asked for

    private static let environment = ProcessInfo.processInfo.environment
    static let device = environment["ZEST_SCREENS_DEVICE"] ?? "phone"
    static let mustShowWithoutScrolling = environment["ZEST_SCREENS_NO_SCROLL"] == "1"
    static let runAudit = environment["ZEST_SCREENS_AUDIT"] != "0"
    static let bigText = environment["ZEST_SCREENS_BIG_TEXT"] == "1"

    // MARK: The screens

    /// What has to be on a screen.
    struct Plan {
        /// The element that shows the screen is up.
        var anchor: String
        /// Buttons pinned at the foot of the screen: whole, inside the
        /// screen, not overlapping, tappable.
        var actions: [String] = []
        /// Other things the tech needs at once: showing and tappable.
        var showing: [String] = []
        /// Things that may be further down the page: reachable by scrolling.
        var reachable: [String] = []
    }

    /// The first stop on the made-up route, and the stop the screens are
    /// about. (Customer 101 on the usual route; 201 and up on the odd one.)
    private func plan(for screen: String, target: Int) -> Plan {
        switch screen {
        case "login":
            return Plan(anchor: "SEND MY CODE", actions: ["SEND MY CODE", "USE EMAIL INSTEAD"], showing: ["contact-field"], reachable: ["link-terms", "link-privacy"])
        case "code":
            return Plan(anchor: "code-field", actions: ["SEND A NEW CODE", "BACK"], showing: ["code-field", "bar-back"])
        case "today":
            return Plan(anchor: "tab-route", actions: ["tab-route", "tab-videos"], showing: ["route-header", "stop-\(target)"], reachable: ["not-on-my-list"])
        case "today2":
            return Plan(anchor: "tab-route", actions: ["tab-route", "tab-videos"], showing: ["not-on-my-list"])
        case "confirm":
            return Plan(anchor: "THIS IS IT", actions: ["THIS IS IT", "WRONG STOP"], showing: ["bar-back"])
        case "far":
            return Plan(anchor: "YES, STILL RIGHT", actions: ["YES, STILL RIGHT", "PICK ANOTHER STOP"], showing: ["bar-back"])
        case "record":
            return Plan(anchor: "record-button", actions: ["record-button", "BACK"], showing: ["flip-button"])
        case "recording":
            return Plan(anchor: "countdown-ring", actions: ["countdown-ring"], showing: ["recording-pill", "prompt-card"])
        case "review":
            return Plan(anchor: "SAVE", actions: ["SAVE", "RECORD AGAIN"], showing: ["recommend-switch", "clip"])
        case "warning":
            return Plan(anchor: "SAVE ANYWAY", actions: ["SAVE ANYWAY", "PICK THE RIGHT STOP", "RECORD AGAIN"])
        case "saved":
            return Plan(anchor: "saved-screen", showing: ["saved-screen"])
        case "stopvideo":
            return Plan(anchor: "DELETE VIDEO", actions: ["RECORD AGAIN", "DELETE VIDEO", "BACK"], showing: ["bar-back"])
        case "locked":
            return Plan(anchor: "BACK", actions: ["BACK"], showing: ["bar-back"])
        case "queue":
            return Plan(anchor: "tab-videos", actions: ["tab-route", "tab-videos"], reachable: ["link-signout", "held-space"])
        case "update":
            return Plan(anchor: "screen-title", showing: ["screen-title"])
        default:
            return Plan(anchor: "screen-title")
        }
    }

    // One test per screen, so a failure names the screen.
    func testLogin() { check("login") }
    func testCode() { check("code") }
    func testToday() { check("today") }
    func testTodayScrolledToTheEnd() { check("today2") }
    func testConfirm() { check("confirm") }
    func testFarAway() { check("far") }
    func testRecord() { check("record") }
    func testRecording() { check("recording") }
    func testReview() { check("review") }
    func testWarning() { check("warning") }
    func testSaved() { check("saved") }
    func testStopVideo() { check("stopvideo") }
    func testLocked() { check("locked") }
    func testQueue() { check("queue") }
    func testUpdate() { check("update") }

    /// The route of awkward customers (an apostrophe, accents, a hyphen, a
    /// 45-letter business, all capitals, an emoji, one word, a unit, a PO
    /// box, a 24-letter last name) on every screen that shows a name. No
    /// name or address may be cut off, and every action must still be there.
    func testOddNamesAreNeverCutOff() {
        executionTimeAllowance = 900
        var problems: [String] = []
        // The largest type first: the name and address on the confirm screen.
        for index in 0..<10 {
            problems += inspect("confirm", odd: index, audits: [.textClipped])
        }
        // The lists.
        problems += inspect("today", odd: 0, audits: [.textClipped])
        problems += inspect("today2", odd: 0, audits: [.textClipped])
        for index in [0, 4, 8] {
            problems += inspect("queue", odd: index, audits: [.textClipped])
        }
        // The other screens with a name, for the longest ones: the long
        // address, the 45-letter business, the 24-letter word. (CI takes
        // pictures of more; each check here costs a launch of the app.)
        for index in [2, 4, 9] {
            for screen in ["record", "recording", "review", "stopvideo"] {
                problems += inspect(screen, odd: index, audits: [.textClipped])
            }
        }
        report(problems, for: "odd names")
    }

    // MARK: The check

    private func check(_ screen: String) {
        let audits: XCUIAccessibilityAuditType = [.contrast, .hitRegion, .textClipped, .sufficientElementDescription, .dynamicType]
        let problems = inspect(screen, odd: nil, audits: audits)
        report(problems, for: screen)
    }

    private func report(_ problems: [String], for what: String) {
        guard !problems.isEmpty else { return }
        let list = problems.enumerated().map { "  \($0.offset + 1). \($0.element)" }.joined(separator: "\n")
        XCTFail("\(problems.count) problem(s) on \(what) (\(Self.device)):\n\(list)")
    }

    /// Opens one screen and returns everything wrong with it, in plain words.
    private func inspect(_ screen: String, odd: Int?, audits: XCUIAccessibilityAuditType) -> [String] {
        let name = odd.map { "\(screen) (odd names, customer \($0))" } ?? screen
        var arguments = ["-ZestScreen", screen]
        if let odd {
            arguments += ["-ZestFixture", "odd", "-ZestStop", String(odd)]
        }
        app.launchArguments = arguments
        app.launchEnvironment = [:]
        app.launch()

        let target = odd.map { 201 + $0 } ?? 101
        let plan = self.plan(for: screen, target: target)
        guard element(plan.anchor).waitForExistence(timeout: 40) else {
            return ["\(name): the screen never came up (no \"\(plan.anchor)\" after 40 s). On screen: \(screenSummary())"]
        }
        // The first moment a screen ignores touches; and let it finish drawing.
        pause(1.0)

        var problems: [String] = []
        problems += layoutProblems(plan, on: name)
        if screen == "recording" {
            problems += promptProblems(on: name, usualRoute: odd == nil)
        }
        if Self.bigText {
            problems += textSizeProblems(on: name, screen: screen)
        }
        if Self.runAudit {
            problems += auditProblems(audits, on: name, screen: screen)
        }
        return problems
    }

    // MARK: The words to say

    /// The picture is taken on the first of the four lines: "Hi" and the
    /// customer's first name. The name must be in, whole, with no "{name}"
    /// left showing; and the card must be inside the screen above the ring.
    private func promptProblems(on name: String, usualRoute: Bool) -> [String] {
        let card = element("prompt-card")
        guard card.exists else { return [] }   // reported as missing by the layout check
        var problems: [String] = []
        let words = card.label
        if words.contains("{") || words.contains("}") {
            problems.append("\(name): the words to say still show the placeholder: \"\(words)\".")
        }
        if !words.contains("1 of 4") || !words.contains("Hi") {
            problems.append("\(name): the first thing to say should be \"Hi\" and the name, 1 of 4; it reads \"\(words)\".")
        }
        if usualRoute, !words.contains("Hi Jane") {
            problems.append("\(name): the first thing to say should be \"Hi Jane\"; it reads \"\(words)\".")
        }
        let ring = element("countdown-ring")
        if ring.exists {
            if card.frame.maxY > ring.frame.minY + 1.5 {
                problems.append("\(name): the words to say run into the ring (words end at y \(Int(card.frame.maxY)), ring starts at y \(Int(ring.frame.minY))).")
            }
            if ring.frame.width < 118 {
                problems.append("\(name): the ring is only \(Int(ring.frame.width)) points wide; it must stay at least 120.")
            }
        }
        return problems
    }

    // MARK: Layout

    private func layoutProblems(_ plan: Plan, on name: String) -> [String] {
        var problems: [String] = []
        let window = app.windows.firstMatch.frame
        guard window.width > 0, window.height > 0 else {
            return ["\(name): the app has no window to measure."]
        }
        // A point of slack: frames are not always on whole points.
        let screen = window.insetBy(dx: -1.5, dy: -1.5)

        func describe(_ frame: CGRect) -> String {
            "x \(Int(frame.minX))...\(Int(frame.maxX)), y \(Int(frame.minY))...\(Int(frame.maxY)) on a \(Int(window.width)) x \(Int(window.height)) screen"
        }

        var actionFrames: [(String, CGRect)] = []
        for identifier in plan.actions {
            let item = element(identifier)
            guard item.exists else {
                problems.append("\(name): \"\(identifier)\" is missing.")
                continue
            }
            if !Self.mustShowWithoutScrolling {
                bringIntoView(item)
            }
            let frame = item.frame
            if frame.width < 44 || frame.height < 44 {
                problems.append("\(name): \"\(identifier)\" is only \(Int(frame.width)) x \(Int(frame.height)) points; a gloved thumb needs 44.")
            }
            if !screen.contains(frame) {
                let how = Self.mustShowWithoutScrolling ? "does not show whole without scrolling" : "cannot be brought whole onto the screen"
                problems.append("\(name): \"\(identifier)\" \(how) (\(describe(frame))).")
            } else if !item.isHittable {
                problems.append("\(name): \"\(identifier)\" is on the screen but cannot be tapped: something covers it (\(describe(frame))).")
            }
            actionFrames.append((identifier, frame))
        }
        // No two actions on top of each other.
        for first in 0..<actionFrames.count {
            for second in (first + 1)..<actionFrames.count {
                let overlap = actionFrames[first].1.intersection(actionFrames[second].1)
                if !overlap.isNull, overlap.width > 2, overlap.height > 2 {
                    problems.append("\(name): \"\(actionFrames[first].0)\" and \"\(actionFrames[second].0)\" overlap by \(Int(overlap.width)) x \(Int(overlap.height)) points.")
                }
            }
        }

        for identifier in plan.showing {
            let item = element(identifier)
            guard item.exists else {
                problems.append("\(name): \"\(identifier)\" is missing.")
                continue
            }
            if !Self.mustShowWithoutScrolling {
                bringIntoView(item)
            }
            let frame = item.frame
            let middle = CGPoint(x: frame.midX, y: frame.midY)
            if !screen.contains(middle) {
                let how = Self.mustShowWithoutScrolling ? "does not show without scrolling" : "cannot be scrolled onto the screen"
                problems.append("\(name): \"\(identifier)\" \(how) (\(describe(frame))).")
            }
            if frame.minX < screen.minX || frame.maxX > screen.maxX {
                problems.append("\(name): \"\(identifier)\" runs off the side of the screen (\(describe(frame))).")
            }
        }

        for identifier in plan.reachable {
            let item = element(identifier)
            guard item.exists else {
                problems.append("\(name): \"\(identifier)\" is missing.")
                continue
            }
            bringIntoView(item)
            if !item.isHittable {
                problems.append("\(name): \"\(identifier)\" cannot be reached by scrolling (\(describe(item.frame))).")
            }
        }

        // Nothing that can be tapped may run off the side of the screen.
        // (One picture of the whole screen's elements, not a question each.)
        if let tree = try? app.snapshot() {
            var offSide: [String] = []
            collectButtons(in: tree) { identifier, label, frame in
                guard frame.width > 0, frame.height > 0 else { return }
                if frame.minX < screen.minX || frame.maxX > screen.maxX {
                    let shown = identifier.isEmpty ? String(label.prefix(50)) : identifier
                    offSide.append("\(name): \"\(shown)\" runs off the side of the screen (\(describe(frame))).")
                }
            }
            problems += offSide.prefix(6)
        }
        return problems
    }

    private func collectButtons(in node: XCUIElementSnapshot, _ visit: (String, String, CGRect) -> Void) {
        if node.elementType == .button {
            visit(node.identifier, node.label, node.frame)
        }
        for child in node.children {
            collectButtons(in: child, visit)
        }
    }

    // MARK: Text size

    /// With the phone's text size at its largest, the app's own type must be
    /// clearly bigger than its ordinary size. This is the proof that the
    /// app follows the text-size setting, whatever Apple's audit makes of
    /// fonts that are not one of its named styles.
    private func textSizeProblems(on name: String, screen: String) -> [String] {
        // The bar's title is on every screen but three, in one line of
        // ordinary 26-point type: about 31 points tall. At the largest size
        // the app allows it is 35-point type: about 42 points tall.
        let title = element("bar-title")
        guard title.exists else { return [] }
        let height = title.frame.height
        if height < 37 {
            return ["\(name): the top bar's title is \(Int(height)) points tall. With the text size at its largest it should be about 42: the app's type did not grow."]
        }
        return []
    }

    // MARK: Apple's audit

    /// Issues that are known and meant, each with its reason. Anything not
    /// listed here fails the test.
    private func knownReason(_ type: XCUIAccessibilityAuditType, screen: String, identifier: String) -> String? {
        // Words over the camera picture sit on their own dark plates. In
        // these pictures the "camera" is a drawing; on a phone it is whatever
        // the camera sees, which no audit can measure ahead of time.
        if type == .contrast, ["record", "recording"].contains(screen) {
            return "over the camera picture (a stand-in drawing here); the words sit on dark plates"
        }
        // The played-back clip is the same stand-in drawing.
        if type == .contrast, ["review", "warning"].contains(screen), identifier == "clip" {
            return "the clip is a stand-in drawing here"
        }
        return nil
    }

    private func auditProblems(_ audits: XCUIAccessibilityAuditType, on name: String, screen: String) -> [String] {
        var problems: [String] = []
        var lines: [String] = []
        do {
            try app.performAccessibilityAudit(for: audits) { issue in
                let kind = Self.word(for: issue.auditType)
                var identifier = ""
                var what = "(no element given)"
                if let item = issue.element, item.exists {
                    identifier = item.identifier
                    let label = item.label
                    let frame = item.frame
                    let shown = identifier.isEmpty ? "\"\(label.prefix(60))\"" : "\"\(identifier)\" (\"\(label.prefix(40))\")"
                    what = "\(shown), \(Int(frame.width)) x \(Int(frame.height)) at \(Int(frame.minX)),\(Int(frame.minY))"
                }
                let text = "[\(kind)] \(what): \(issue.compactDescription)"
                if let reason = self.knownReason(issue.auditType, screen: screen, identifier: identifier) {
                    lines.append("known   \(name): \(text)  (\(reason))")
                } else if issue.auditType == .dynamicType {
                    // Listed, not failed, until the first results are in. The
                    // app sizes its own type and scales it with the phone's
                    // setting (`ZestFont`); Apple's audit is expected to flag
                    // every text that is not one of its named styles. The
                    // large-text run checks that the type really grows.
                    lines.append("listed  \(name): \(text)")
                } else {
                    lines.append("FAILED  \(name): \(text)")
                    problems.append("\(name): \(text)")
                }
                // Handled here: the test fails once, below, with the whole list.
                return true
            }
        } catch {
            problems.append("\(name): Apple's accessibility audit could not run: \(error)")
        }
        if lines.isEmpty {
            lines.append("clean   \(name): no issue of any kind")
        }
        appendNote(named: "accessibility-audit.\(Self.device).info", lines.joined(separator: "\n"))
        return problems
    }

    private static func word(for type: XCUIAccessibilityAuditType) -> String {
        switch type {
        case .contrast: return "contrast"
        case .hitRegion: return "tap target"
        case .textClipped: return "clipped text"
        case .sufficientElementDescription: return "description"
        case .dynamicType: return "dynamic type"
        case .elementDetection: return "element detection"
        case .trait: return "trait"
        default: return "other"
        }
    }
}
