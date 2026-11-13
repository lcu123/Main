import XCTest
import ZestCore
#if canImport(UIKit)
import UIKit
#endif

/// Every screen of the app, opened with made-up data (the app's screenshot
/// mode, `-ZestScreen <name>`), and checked two ways:
///
/// 1. **Layout.** The screen's main actions are there, inside the screen, not
///    on top of each other, and can be tapped: without scrolling on a
///    6.1-inch phone and bigger, after scrolling at most on the smallest
///    phone and on an iPad.
/// 2. **The gloves rule.** Every main button is at least 76 points tall and
///    spans the screen from gutter to gutter; the record button and the ring
///    are at least 120 points across; and after sign-in no screen has
///    anything to type into, pick from or slide. The numbers are the app's
///    own (`GloveRule` in ZestCore, which `Theme` is built from).
/// 3. **Every page scrolls clear.** Where a page is taller than the room
///    between the top bar and the buttons or tabs, every line of it can be
///    brought whole into that room. With the largest text, the words that
///    have to be read are whole on the screen before any scrolling.
/// 4. **Apple's accessibility audit** (`performAccessibilityAudit`): contrast,
///    tap targets of 44 points, clipped text, element descriptions, Dynamic
///    Type. A row is judged where it can be seen whole: one that is half
///    under the tab bar when the audit flags it is scrolled into the clear
///    and audited again, and only what is still wrong then is a failure.
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
        case "team":
            return Plan(anchor: "RECORD VIDEO", actions: ["RECORD VIDEO", "BACK"], showing: ["bar-back"])
        case "queue":
            return Plan(anchor: "tab-videos", actions: ["tab-route", "tab-videos"], reachable: ["link-signout", "held-space"])
        case "update":
            return Plan(anchor: "screen-title", showing: ["screen-title"])
        case "lowspace":
            return Plan(anchor: "RECORD ANYWAY", actions: ["RECORD ANYWAY", "BACK"], showing: ["bar-back"])
        case "full":
            return Plan(anchor: "TRY AGAIN", actions: ["TRY AGAIN", "BACK"], showing: ["bar-back"])
        case "olddraft":
            return Plan(anchor: "DELETE VIDEO", actions: ["SAVE", "DELETE VIDEO"], showing: ["clip", "draft-filmed"])
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
    func testTeam() { check("team") }
    func testQueue() { check("queue") }
    func testUpdate() { check("update") }
    func testAlmostFull() { check("lowspace") }
    func testFull() { check("full") }
    func testOldDraft() { check("olddraft") }

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
            for screen in ["record", "recording", "review", "stopvideo", "team"] {
                problems += inspect(screen, odd: index, audits: [.textClipped])
            }
        }
        report(problems, for: "odd names")
    }

    // MARK: The check

    private func check(_ screen: String) {
        // Room for the audit not coming back once (see `auditProblems`).
        executionTimeAllowance = 420
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
        if Self.bigText, odd == nil {
            // Before anything is scrolled: what has to be read is in view.
            problems += firstViewProblems(on: name, screen: screen)
        }
        problems += layoutProblems(plan, on: name)
        if screen == "recording" {
            problems += promptProblems(on: name, usualRoute: odd == nil)
        }
        if screen == "team" {
            problems += teamProblems(on: name)
        }
        if screen == "olddraft" {
            // A clip from an earlier day is saved or deleted, never re-recorded.
            if button("RECORD AGAIN").exists {
                problems.append("\(name): RECORD AGAIN must not be offered for a clip from an earlier day.")
            }
            if !label(of: element("draft-filmed")).hasPrefix("Filmed ") {
                problems.append("\(name): the screen should say which day the clip was filmed; it says \"\(label(of: element("draft-filmed")))\".")
            }
        }
        if screen == "full", element("record-button").exists || button("RECORD ANYWAY").exists {
            problems.append("\(name): a full phone must offer no way to the camera.")
        }
        problems += glovesProblems(on: name, screen: screen)
        if Self.bigText {
            problems += textSizeProblems(on: name, screen: screen)
        }
        // Every line of the page can be brought whole into view. (This
        // leaves the page at its top, where the audit starts.)
        problems += scrollProblems(on: name)
        if Self.runAudit {
            problems += auditProblems(audits, on: name, screen: screen, anchor: plan.anchor)
            if screen == "review", odd == nil, app.state == .runningForeground {
                // The picture is taken with the switch at NO. YES is looked
                // at too: its white word and knob on the blue track.
                problems += switchOnProblems(on: name)
            }
        }
        return problems
    }

    /// Turns the "I recommended something" switch on and runs the contrast
    /// and clipped-text audits again, so both of its states are checked.
    private func switchOnProblems(on name: String) -> [String] {
        let toggle = app.buttons["recommend-switch"]
        guard toggle.exists else { return [] }   // reported as missing by the layout check
        if !Self.mustShowWithoutScrolling {
            bringIntoView(toggle)
        }
        toggle.tap()
        for _ in 0..<20 where (toggle.value as? String) != "Yes" {
            pause(0.25)
        }
        guard (toggle.value as? String) == "Yes" else {
            return ["\(name): tapping the \"I recommended something\" switch did not turn it on (it says \(String(describing: toggle.value)))."]
        }
        pause(0.5)
        return auditProblems([.contrast, .textClipped], on: "\(name) (switch at YES)", screen: "review", anchor: "SAVE")
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

    // MARK: A stop a teammate recorded

    /// The words are the new rule's (each tech sends his own, the customer
    /// gets both), and nothing on the screen can change the teammate's video.
    private func teamProblems(on name: String) -> [String] {
        var problems: [String] = []
        for words in ["Another tech already recorded this stop.", "You can record your own too. The customer gets both."] where !hasText(words) {
            problems.append("\(name): the screen should say \"\(words)\".")
        }
        for words in ["RECORD AGAIN", "DELETE VIDEO", "CALL OFFICE"] where button(words).exists {
            problems.append("\(name): \(words) must not be offered on a teammate's video.")
        }
        if hasText("takes its place") {
            problems.append("\(name): the screen still says a new video takes the other tech's place.")
        }
        return problems
    }

    // MARK: What can be seen whole

    /// The room a page scrolls in: between the top bar and the buttons or
    /// the tabs. `nil` on a screen with no scrolling page (the camera).
    private func pageRoom() -> CGRect? {
        let scroll = app.scrollViews.firstMatch
        guard scroll.exists else { return nil }
        let frame = scroll.frame
        return frame.height > 40 ? frame : nil
    }

    /// Every piece of the scrolling page (a text, or a card that reads as
    /// one), in reading order, with where it is now. Two with the same
    /// words are told apart by their order ("Sent ✓ #2").
    private func pageTexts() -> [(key: String, label: String, frame: CGRect)] {
        guard let tree = try? app.scrollViews.firstMatch.snapshot() else { return [] }
        var found: [(key: String, label: String, frame: CGRect)] = []
        var counts: [String: Int] = [:]
        collect(in: tree) { node in
            guard node.elementType == .staticText || node.elementType == .button else { return }
            let label = node.label
            guard !label.isEmpty, node.frame.width > 0, node.frame.height > 0 else { return }
            counts[label, default: 0] += 1
            found.append((key: "\(label) #\(counts[label] ?? 1)", label: label, frame: node.frame))
        }
        return found
    }

    /// True when the whole page is inside its room: there is nothing to
    /// scroll to.
    private func pageFits() -> Bool {
        guard let room = pageRoom() else { return true }
        let clear = room.insetBy(dx: -1.5, dy: -1.5)
        return pageTexts().allSatisfy { clear.contains($0.frame) }
    }

    /// Waits until the page has stopped moving.
    private func settle() {
        var last = pageTexts().map { $0.frame }
        for _ in 0..<8 {
            pause(0.35)
            let now = pageTexts().map { $0.frame }
            if now == last { return }
            last = now
        }
    }

    /// Moves the page by this many points: a positive amount shows what is
    /// further down. The finger goes down inside the room the page scrolls
    /// in, moves slowly and rests a moment before it lifts, so the page
    /// stops where it is put instead of coasting on.
    private func scrollPage(points: CGFloat) {
        guard let room = pageRoom(), abs(points) >= 1 else { return }
        let travel = max(-room.height * 0.6, min(room.height * 0.6, points))
        let startY = travel > 0 ? room.maxY - room.height * 0.15 : room.minY + room.height * 0.15
        let corner = app.coordinate(withNormalizedOffset: CGVector(dx: 0, dy: 0))
        let start = corner.withOffset(CGVector(dx: room.midX, dy: startY))
        let end = corner.withOffset(CGVector(dx: room.midX, dy: startY - travel))
        start.press(forDuration: 0.05, thenDragTo: end, withVelocity: .slow, thenHoldForDuration: 0.25)
    }

    /// Scrolls the page as far as it goes, to its top or to its end.
    private func scrollPage(toEnd: Bool) {
        guard let room = pageRoom() else { return }
        var last = pageTexts().map { $0.frame }
        for _ in 0..<14 {
            scrollPage(points: (toEnd ? 1 : -1) * room.height * 0.6)
            let now = pageTexts().map { $0.frame }
            if now == last { break }
            last = now
        }
        settle()
    }

    /// Moves the page until this element is whole inside the room it
    /// scrolls in. It stops when the element is there, or when scrolling no
    /// longer moves it (the page is at its end, or the element is part of
    /// the bars and not of the page at all).
    private func bringIntoPageRoom(_ item: XCUIElement) {
        for _ in 0..<12 {
            guard item.exists, let room = pageRoom() else { break }
            let frame = item.frame
            if room.insetBy(dx: -1.5, dy: -1.5).contains(frame) { break }
            if frame.maxY > room.maxY {
                scrollPage(points: frame.maxY - room.maxY + 28)     // further down
            } else if frame.minY < room.minY {
                scrollPage(points: -(room.minY - frame.minY + 28))  // further up
            } else {
                break
            }
            if item.exists, item.frame == frame { break }           // it did not move
        }
        settle()
    }

    private enum Sight {
        /// Whole inside the room the page scrolls in.
        case clear
        /// Across the edge of that room: part of it is under the top bar,
        /// the tab bar or the buttons.
        case cut
        /// All of it outside that room: one of the bars or pinned buttons,
        /// or a row scrolled right out of sight.
        case outside
    }

    private func sight(of item: XCUIElement) -> Sight {
        sight(ofFrame: item.frame)
    }

    private func sight(ofFrame frame: CGRect) -> Sight {
        let window = app.windows.firstMatch.frame.insetBy(dx: -1.5, dy: -1.5)
        guard let room = pageRoom() else { return window.contains(frame) ? .clear : .cut }
        if room.insetBy(dx: -1.5, dy: -1.5).contains(frame) { return .clear }
        let overlap = room.intersection(frame)
        return (!overlap.isNull && overlap.height > 2 && overlap.width > 2) ? .cut : .outside
    }

    /// The top bar, the tabs and the pinned buttons: never part of the page.
    /// (A big button is known by its words: "RECORD VIDEO" reads "Record Video".)
    private static func isPinned(identifier: String, label: String) -> Bool {
        if ["tab-route", "tab-videos", "bar-back", "bar-title"].contains(identifier) { return true }
        return !identifier.isEmpty && identifier == identifier.uppercased() && identifier.capitalized == label
    }

    // MARK: Every page scrolls clear

    /// Where this piece of the page is now. A piece is known by its words
    /// and its place among pieces with the same words ("Sent ✓ #2"): unlike
    /// an element handed out by the audit, that still finds it after the
    /// page has moved.
    private func frame(of key: String) -> CGRect? {
        pageTexts().first { $0.key == key }?.frame
    }

    /// Moves the page until this piece is whole inside the room it scrolls
    /// in (or, for a piece taller than the room, until its first line is at
    /// the top). Returns where the piece ended up; `nil` if it is gone.
    @discardableResult
    private func bringIntoPageRoom(key: String) -> CGRect? {
        var last: CGRect?
        for _ in 0..<12 {
            guard let room = pageRoom(), let spot = frame(of: key) else { return nil }
            if room.insetBy(dx: -1.5, dy: -1.5).contains(spot) { break }
            if let last, last == spot { break }                     // the page no longer moves
            last = spot
            if spot.height > room.height - 8 {
                let off = spot.minY - room.minY - 12                // its top, to the top of the room
                if abs(off) < 6 { break }
                scrollPage(points: off)
            } else if spot.maxY > room.maxY {
                scrollPage(points: spot.maxY - room.maxY + 28)      // further down
            } else if spot.minY < room.minY {
                scrollPage(points: -(room.minY - spot.minY + 28))   // further up
            } else {
                break
            }
        }
        settle()
        return frame(of: key)
    }

    /// Moves the page until the last line of this piece is in the room.
    /// (For a piece taller than the room, whose end is read in a second scroll.)
    @discardableResult
    private func bringEndIntoPageRoom(key: String) -> CGRect? {
        var last: CGRect?
        for _ in 0..<12 {
            guard let room = pageRoom(), let spot = frame(of: key) else { return nil }
            let over = spot.maxY - room.maxY
            if over <= 1.5, spot.maxY >= room.minY + 24 { break }
            if let last, last == spot { break }                     // the page no longer moves
            last = spot
            scrollPage(points: over > 1.5 ? over + 22 : -(room.minY + 24 - spot.maxY + 22))
        }
        settle()
        return frame(of: key)
    }

    /// Brings the last piece of the page whole into the room: the end of the
    /// page, reached the way a thumb reaches it, without pulling at a page
    /// that is already there.
    private func showEndOfPage() {
        for _ in 0..<14 {
            let texts = pageTexts()
            guard let room = pageRoom(), let last = texts.max(by: { $0.frame.maxY < $1.frame.maxY }) else { return }
            let over = last.frame.maxY - room.maxY
            if over <= -6 { break }                                 // whole, with a little air under it
            scrollPage(points: over + 22)
            if pageTexts().map({ $0.frame }) == texts.map({ $0.frame }) { break }   // the page no longer moves
        }
        settle()
    }

    /// A page taller than its room must scroll, and far enough that every
    /// line of it can be seen whole: nothing may stay half under the tab bar
    /// or behind the buttons. The page is walked from top to bottom and
    /// every piece has to be whole in the room at one position at least.
    ///
    /// A single piece taller than the room (a card with a long name at the
    /// largest text) can never be whole in it. That is accepted only if it
    /// can be read in two scrolls: its first line comes into view at one
    /// position and its last line at another. A piece with a part that can
    /// never be reached is a failure.
    private func scrollProblems(on name: String) -> [String] {
        // Nothing to do when the whole page is in view already.
        guard let room = pageRoom(), !pageFits() else { return [] }
        scrollPage(toEnd: false)
        let clear = room.insetBy(dx: -1.5, dy: -1.5)
        var texts = pageTexts()

        var seenWhole = Set<String>()
        var seenTop = Set<String>()
        var seenEnd = Set<String>()
        var known: [String: (label: String, height: CGFloat)] = [:]
        var moved = false
        func look() {
            for text in texts {
                known[text.key] = (text.label, text.frame.height)
                if clear.contains(text.frame) { seenWhole.insert(text.key) }
                // For a piece taller than the room: its first line in view,
                // and (at another position) its last.
                if text.frame.minY >= clear.minY, text.frame.minY <= room.maxY - 24 { seenTop.insert(text.key) }
                if text.frame.maxY <= clear.maxY, text.frame.maxY >= room.minY + 24 { seenEnd.insert(text.key) }
            }
        }
        for _ in 0..<16 {
            look()
            let before = texts.map { $0.frame }
            scrollPage(points: room.height * 0.45)
            settle()
            texts = pageTexts()
            if texts.map({ $0.frame }) == before { break }   // the end of the page
            moved = true
        }
        look()
        var problems: [String] = []
        if !moved {
            problems.append("\(name): the page is taller than the room above the buttons and does not scroll.")
        }
        // Whatever was never whole on the way down gets its own try: the
        // steps above can pass over a tall card.
        let missed = known.filter { !seenWhole.contains($0.key) }
        for (key, text) in missed.sorted(by: { $0.key < $1.key }).prefix(8) {
            let words = "\"\(text.label.prefix(60))\""
            if text.height > room.height - 8 {
                // Taller than the room: read in two scrolls, or not at all.
                var top = seenTop.contains(key), end = seenEnd.contains(key)
                if !top, let spot = bringIntoPageRoom(key: key), let now = pageRoom() {
                    top = spot.minY >= now.minY - 1.5 && spot.minY <= now.maxY - 24
                }
                if !end, let spot = bringEndIntoPageRoom(key: key), let now = pageRoom() {
                    end = spot.maxY <= now.maxY + 1.5 && spot.maxY >= now.minY + 24
                }
                if top && end { continue }
                let part = !top && !end ? "neither its first line nor its last comes" : (!top ? "its first line never comes" : "its last line never comes")
                problems.append("\(name): \(words) is taller than the room it scrolls in (\(Int(text.height)) points in \(Int(room.height))) and part of it can never be reached: \(part) into view.")
                continue
            }
            if let spot = bringIntoPageRoom(key: key), let now = pageRoom(), now.insetBy(dx: -1.5, dy: -1.5).contains(spot) {
                continue
            }
            if frame(of: key) == nil {
                // Its words changed while the page was being walked. The
                // clock holds still in these screens, so this should not be.
                problems.append("\(name): \(words) was on the page and then was not: its words changed while the page was being checked.")
                continue
            }
            problems.append("\(name): \(words) can never be seen whole: the page does not scroll it clear of the bar or the buttons.")
        }
        scrollPage(toEnd: false)
        return problems
    }

    /// With the largest text a page is often taller than its room. What the
    /// tech has to read must then be whole on the screen as it opens, before
    /// any scrolling: it is put first on these screens for that reason.
    private func firstViewProblems(on name: String, screen: String) -> [String] {
        let wanted: [String]
        switch screen {
        case "team": wanted = ["Another tech already recorded this stop.", "You can record your own too. The customer gets both."]
        case "stopvideo": wanted = ["You can change it for"]
        case "locked": wanted = ["This video already went to the customer."]
        case "warning": wanted = ["I did not hear this customer's name."]
        case "lowspace": wanted = ["You can still record this video."]
        case "full": wanted = ["There is no room to record a video."]
        default: return []
        }
        guard let room = pageRoom() else { return [] }
        let clear = room.insetBy(dx: -1.5, dy: -1.5)
        var problems: [String] = []
        for words in wanted {
            let text = app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", words)).firstMatch
            guard text.exists else {
                problems.append("\(name): the screen should say \"\(words)\".")
                continue
            }
            if !clear.contains(text.frame) {
                problems.append("\(name): \"\(words)\" is not whole on the screen as it opens (it is at y \(Int(text.frame.minY))...\(Int(text.frame.maxY)); the room above the buttons is y \(Int(room.minY))...\(Int(room.maxY))).")
            }
        }
        return problems
    }

    // MARK: The gloves rule

    /// The round controls on the camera, and the two tabs: main actions that
    /// are not full-width buttons.
    private static let roundControls: Set<String> = ["record-button", "countdown-ring"]
    private static let tabs: Set<String> = ["tab-route", "tab-videos"]

    /// "Main actions span the full width of the screen." Every main button
    /// is at least `GloveRule.minButtonHeight` tall and as wide as the
    /// screen less its two gutters. The record button and the ring are at
    /// least `GloveRule.minRoundControl` across. A tab is at least Apple's
    /// minimum (two share the width).
    private func sizeProblems(_ identifier: String, frame: CGRect, screenWidth: Double, on name: String) -> [String] {
        let width = Double(frame.width), height = Double(frame.height)
        let size = "\(Int(width)) x \(Int(height)) points"
        if Self.roundControls.contains(identifier) {
            let floor = GloveRule.minRoundControl
            if width < floor - 1 || height < floor - 1 {
                return ["\(name): \"\(identifier)\" is only \(size); it must be at least \(Int(floor)) across."]
            }
            return []
        }
        if Self.tabs.contains(identifier) {
            let floor = GloveRule.minTapTarget
            if width < floor || height < floor {
                return ["\(name): \"\(identifier)\" is only \(size); a gloved thumb needs \(Int(floor))."]
            }
            return []
        }
        var problems: [String] = []
        if height < GloveRule.minButtonHeight - 0.5 {
            problems.append("\(name): \"\(identifier)\" is only \(Int(height)) points tall; a main button is never under \(Int(GloveRule.minButtonHeight)).")
        }
        let wanted = GloveRule.actionWidth(screenWidth: screenWidth)
        if width < wanted - 2 {
            problems.append("\(name): \"\(identifier)\" is \(Int(width)) points wide; a main button spans the screen, \(Int(wanted)) points here.")
        }
        return problems
    }

    /// "No menus, no swipes, no typing after setup." After sign-in no screen
    /// may hold a text field, a picker, a slider, a stepper or a menu. (The
    /// two sign-in screens have the one field each that setup needs.)
    private func glovesProblems(on name: String, screen: String) -> [String] {
        guard let tree = try? app.snapshot() else {
            return ["\(name): the screen could not be read to check for text fields and menus."]
        }
        let typing: [XCUIElement.ElementType: String] = [
            .textField: "a text field", .secureTextField: "a password field", .textView: "a text box",
            .searchField: "a search box", .keyboard: "a keyboard",
        ]
        let fiddly: [XCUIElement.ElementType: String] = [
            .picker: "a picker", .pickerWheel: "a picker wheel", .datePicker: "a date picker", .slider: "a slider",
            .stepper: "a stepper", .segmentedControl: "a segmented control", .menu: "a menu", .menuButton: "a menu button",
            .popUpButton: "a pop-up menu", .comboBox: "a combo box", .pageIndicator: "pages to swipe through",
        ]
        let signIn = ["login", "code"].contains(screen)
        var found: [String: Int] = [:]
        collect(in: tree) { node in
            if let what = fiddly[node.elementType] {
                found[what, default: 0] += 1
            } else if !signIn, let what = typing[node.elementType] {
                found[what, default: 0] += 1
            }
        }
        return found.keys.sorted().map { what in
            signIn
                ? "\(name): the screen has \(what); nothing in the app is picked, slid or swiped."
                : "\(name): the screen has \(what); after sign-in there is nothing to type, pick, slide or swipe."
        }
    }

    private func collect(in node: XCUIElementSnapshot, _ visit: (XCUIElementSnapshot) -> Void) {
        visit(node)
        for child in node.children {
            collect(in: child, visit)
        }
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
            problems += sizeProblems(identifier, frame: frame, screenWidth: Double(window.width), on: name)
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

    /// One thing Apple's audit said about one element, as it stood when the
    /// audit looked.
    private struct Flag {
        /// The kind of issue and the element: the same issue about the same
        /// element in a later pass has the same key.
        let key: String
        let type: XCUIAccessibilityAuditType
        let item: XCUIElement
        let identifier: String
        let label: String
        let frame: CGRect
        /// One line of text (not a card or a button made of several).
        let isText: Bool
        /// "[contrast] "SENT ✓", 130 x 41 at 154,295: Contrast failed"
        let shown: String
        /// Its place in the scrolling page, when it is part of it.
        var pageKey: String?
    }

    /// Runs Apple's audit and returns what it found wrong.
    ///
    /// **An element is judged where it can be seen whole.** The audit looks
    /// at the screen as it is, and a row that is half under the tab bar is
    /// measured with the bar's pixels in it: that says nothing about the
    /// row. So such a row is not judged there and not excused either: it is
    /// scrolled into the clear and the audit is run again.
    ///
    /// **A contrast issue on a line of text is settled by the pixels.** The
    /// audit's "Contrast failed" gives no numbers. For a line that is whole
    /// on the screen the test takes a picture of just that line and reads
    /// the two colours off it (`PixelContrast`). 4.5:1 or better, inside the
    /// screen, with no other text over it: the line is listed as known, with
    /// the measured numbers. Anything else fails, with the numbers. The
    /// pixels that were read are kept, and `Scripts/measure_contrast.py`
    /// reads them again in CI.
    ///
    /// **The audit is not trusted to come back.** On some layouts it never
    /// does. The CI job closes the app when nothing has happened for a long
    /// while; the test then opens the screen again and tries once more, a
    /// little further along the page. A second time is a failure that says so.
    private func auditProblems(_ audits: XCUIAccessibilityAuditType, on name: String, screen: String, anchor: String) -> [String] {
        var problems: [String] = []
        var lines: [String] = []
        var judged = Set<String>()
        // Flagged while not whole in the room the page scrolls in.
        var waiting: [Flag] = []
        // Seen whole in that room during a pass that did not flag them.
        var fineWhenWhole = Set<String>()
        var stalls = 0
        var passes = 0

        func fail(_ text: String) {
            lines.append("FAILED  \(name): \(text)")
            problems.append("\(name): \(text)")
        }

        // One run of the audit over the screen as it stands. Returns what it
        // flagged; `nil` when the app was gone by the end of it.
        func pass() -> [Flag]? {
            var flags: [Flag] = []
            var nameless: [String] = []
            var thrown: String?
            // The Dynamic Type check is listed, never failed, and is the one
            // check that makes the app lay itself out again at other text
            // sizes. Once per screen, on the first pass, is enough.
            passes += 1
            let checks = passes == 1 ? audits : audits.subtracting(.dynamicType)
            do {
                try app.performAccessibilityAudit(for: checks) { issue in
                    let kind = Self.word(for: issue.auditType)
                    guard let item = issue.element, item.exists else {
                        nameless.append("[\(kind)] (no element given): \(issue.compactDescription)")
                        return true
                    }
                    let identifier = item.identifier
                    let label = item.label
                    let frame = item.frame
                    let shown = identifier.isEmpty ? "\"\(label.prefix(60))\"" : "\"\(identifier)\" (\"\(label.prefix(40))\")"
                    let what = "\(shown), \(Int(frame.width)) x \(Int(frame.height)) at \(Int(frame.minX)),\(Int(frame.minY))"
                    flags.append(Flag(
                        key: "\(kind)|\(identifier.isEmpty ? label : identifier)", type: issue.auditType, item: item,
                        identifier: identifier, label: label, frame: frame, isText: item.elementType == .staticText,
                        shown: "[\(kind)] \(what): \(issue.compactDescription)", pageKey: nil
                    ))
                    // Handled here: the test fails once, below, with the whole list.
                    return true
                }
            } catch {
                thrown = "\(error)"
            }
            // The app is gone: it crashed, or it was closed from outside
            // because the audit sat with nothing happening.
            guard app.wait(for: .runningForeground, timeout: 3) else { return nil }
            if let thrown {
                problems.append("\(name): Apple's accessibility audit could not run: \(thrown)")
            }
            if !nameless.isEmpty {
                // An issue with no element cannot be scrolled into view to
                // be judged. Where a line of the page is cut by the edge of
                // its room, that is what the audit is seeing (every line is
                // checked whole by `scrollProblems`); where nothing is cut,
                // it is a real one.
                let cut = self.cutTexts()
                for text in nameless {
                    if cut.isEmpty {
                        fail(text)
                    } else {
                        lines.append("cut     \(name): \(text)  (at this position the edge of the page cuts: \(cut.prefix(3).joined(separator: "; ")))")
                    }
                }
            }
            return flags
        }

        // Whole on the screen and flagged: this is the verdict on it.
        func judge(_ flag: Flag) {
            judged.insert(flag.key)
            waiting.removeAll { $0.key == flag.key }
            guard flag.type == .contrast else {
                fail(flag.shown)
                return
            }
            let verdict = self.contrastVerdict(flag, on: name)
            if verdict.cleared {
                lines.append("known   \(name): \(flag.shown)  (\(verdict.words))")
            } else {
                fail("\(flag.shown)  (\(verdict.words))")
            }
        }

        // Sorts what one pass flagged: known, listed, judged here, or to be
        // judged once it is whole on the screen.
        func sort(_ flags: [Flag]) {
            var texts: [(key: String, label: String, frame: CGRect)]?
            for flag in flags where !judged.contains(flag.key) {
                if let reason = self.knownReason(flag.type, screen: screen, identifier: flag.identifier) {
                    judged.insert(flag.key)
                    lines.append("known   \(name): \(flag.shown)  (\(reason))")
                } else if flag.type == .dynamicType {
                    // Listed, not failed. The app sizes its own type and
                    // scales it with the phone's setting (`ZestFont`);
                    // Apple's audit flags every text that is not one of
                    // its named styles. The large-text run checks that
                    // the type really grows.
                    judged.insert(flag.key)
                    lines.append("listed  \(name): \(flag.shown)")
                } else if Self.isPinned(identifier: flag.identifier, label: flag.label) || self.sight(ofFrame: flag.frame) == .clear {
                    judge(flag)
                } else if !waiting.contains(where: { $0.key == flag.key }), !fineWhenWhole.contains(flag.key) {
                    // Not whole in the room the page scrolls in: judged
                    // once it is. It is found again by its words.
                    if texts == nil { texts = self.pageTexts() }
                    var kept = flag
                    kept.pageKey = (texts ?? [])
                        .filter { $0.label == flag.label }
                        .min { abs($0.frame.minY - flag.frame.minY) < abs($1.frame.minY - flag.frame.minY) }?
                        .key
                    waiting.append(kept)
                }
            }
            // Whatever was waiting to be seen whole, is whole on the screen
            // now (its text is part of a row that is all inside the room)
            // and was not flagged in this pass, is fine.
            if let room = self.pageRoom(), !waiting.isEmpty {
                let flagged = Set(flags.map { $0.key })
                let clear = room.insetBy(dx: -1.5, dy: -1.5)
                let whole = (texts ?? self.pageTexts()).filter { clear.contains($0.frame) }.map { $0.label.lowercased() }
                for entry in waiting where !flagged.contains(entry.key) {
                    let words = entry.label.lowercased()
                    if !words.isEmpty, whole.contains(where: { $0.contains(words) }) {
                        fineWhenWhole.insert(entry.key)
                        lines.append("clean   \(name): fine where it is whole on the screen: \(entry.shown)")
                    }
                }
                waiting.removeAll { fineWhenWhole.contains($0.key) }
            }
        }

        // A pass that survives the audit not coming back once. `restore`
        // puts the page back where it was, a little off, after the screen
        // has been opened again. `nil`: it happened twice, or the screen
        // would not open again. Either way the reason is in `problems`.
        func passOrOpenAgain(at place: String, restore: () -> Void) -> [Flag]? {
            while true {
                if let flags = pass() { return flags }
                stalls += 1
                guard stalls < 2 else {
                    fail("Apple's audit did not finish at \(place). The app stopped twice while it ran (the CI job closes the app when nothing has happened for a long while; a crash looks the same from here), so nothing could be judged there.")
                    return nil
                }
                lines.append("stalled \(name): Apple's audit did not come back at \(place) and the app had stopped. The screen was opened again and the audit run once more, a little further along the page.")
                app.launch()
                guard element(anchor).waitForExistence(timeout: 40) else {
                    fail("after Apple's audit stalled at \(place) the screen could not be opened again.")
                    return nil
                }
                pause(1.0)
                restore()
            }
        }

        func place(of flag: Flag) -> CGRect? {
            if let key = flag.pageKey { return self.frame(of: key) }
            return flag.item.exists ? flag.item.frame : nil
        }
        func bring(_ flag: Flag) {
            if let key = flag.pageKey {
                self.bringIntoPageRoom(key: key)
            } else {
                self.bringIntoPageRoom(flag.item)
            }
        }

        // The same for the top of the page and for its end.
        positions: for end in [false, true] {
            if end {
                guard let room = pageRoom(), pageTexts().contains(where: { $0.frame.maxY > room.maxY + 1.5 }) else { break }
                showEndOfPage()
            }
            settle()
            let first = passOrOpenAgain(at: end ? "the end of the page" : "the top of the page") {
                // Not quite the same spot: a row that sat exactly on an
                // edge no longer does.
                if end {
                    self.showEndOfPage()
                    self.scrollPage(points: -44)
                } else if !self.pageFits() {
                    self.scrollPage(points: 44)
                }
                self.settle()
            }
            guard let first else { break positions }
            sort(first)

            // Each element that was not whole in the room when it was
            // flagged: into the clear, and the audit again.
            var rounds = 0
            while let next = waiting.first, rounds < 4 {
                rounds += 1
                let before = place(of: next)
                bring(next)
                let again = passOrOpenAgain(at: "the row \(next.shown.prefix(70))") {
                    bring(next)
                    self.settle()
                }
                guard let again else { break positions }
                sort(again)
                guard waiting.contains(where: { $0.key == next.key }) else { continue }   // judged in that pass
                waiting.removeAll { $0.key == next.key }
                guard let now = place(of: next) else {
                    lines.append("unseen  \(name): flagged while not whole on the screen, and gone when looked for again: \(next.shown)")
                    continue
                }
                let flaggedAgain = again.first { $0.key == next.key }
                let seen = sight(ofFrame: now)
                let stayedPut = before == now
                let tooTall = now.height > (pageRoom()?.height ?? .greatestFiniteMagnitude) - 8
                if seen == .clear || (seen == .outside && stayedPut) || tooTall {
                    // Whole in the room now; or a part of the bars that no
                    // scrolling moves; or taller than the room, which can
                    // never be whole in it: what this pass said stands.
                    if let flaggedAgain {
                        judge(flaggedAgain)
                    } else {
                        judged.insert(next.key)
                        lines.append("clean   \(name): fine once whole on the screen: \(next.shown)")
                    }
                } else {
                    judged.insert(next.key)
                    fail("could not be scrolled clear of the bars to be judged: \(next.shown)")
                }
            }
        }
        for left in waiting {
            lines.append("unseen  \(name): flagged while not whole on the screen, and not reached: \(left.shown)")
        }
        if lines.isEmpty {
            lines.append("clean   \(name): no issue of any kind")
        }
        appendNote(named: "accessibility-audit.\(Self.device).info", lines.joined(separator: "\n"))
        if app.state == .runningForeground, !pageFits() {
            scrollPage(toEnd: false)
        }
        return problems
    }

    // MARK: Contrast, from the pixels

    /// Reads the contrast of a flagged line of text off the screen itself.
    /// `cleared` only when the pixels read 4.5:1 or better and the line is
    /// whole on the screen with no other text over it. `words` says what was
    /// found either way.
    private func contrastVerdict(_ flag: Flag, on name: String) -> (cleared: Bool, words: String) {
        guard flag.isText else {
            return (false, "not one line of text, so it cannot be settled by measuring one colour against another")
        }
        guard flag.item.exists else {
            return (false, "it was gone before its pixels could be read")
        }
        let frame = flag.item.frame
        var faults: [String] = []

        // Inside the screen? A line that runs off the edge is a layout bug,
        // whatever its colours.
        let window = app.windows.firstMatch.frame
        if window.width > 0, !window.insetBy(dx: -0.5, dy: -0.5).contains(frame) {
            faults.append("it runs off the screen (\(Int(frame.minX))...\(Int(frame.maxX)) across, \(Int(frame.minY))...\(Int(frame.maxY)) down, on a screen \(Int(window.width)) x \(Int(window.height)))")
        }
        // Another line of text lying across it? (One wholly inside the
        // other is a row and its own words, not two lines colliding.)
        if let tree = try? app.snapshot() {
            var over: [String] = []
            collect(in: tree) { node in
                guard node.elementType == .staticText, !node.label.isEmpty, node.label != flag.label else { return }
                let other = node.frame
                let shared = other.intersection(frame)
                guard !shared.isNull, shared.width > 2, shared.height > 2 else { return }
                if other.insetBy(dx: -1, dy: -1).contains(frame) || frame.insetBy(dx: -1, dy: -1).contains(other) { return }
                over.append("\"\(node.label.prefix(30))\"")
            }
            if !over.isEmpty {
                faults.append("other text lies across it: \(over.prefix(2).joined(separator: ", "))")
            }
        }

        let reading = Self.read(flag.item)
        let saved = save(reading, flag: flag, cleared: faults.isEmpty && (reading?.reading.isEnough ?? false), on: name)
        guard let reading else {
            return (false, "its pixels could not be read" + (faults.isEmpty ? "" : "; " + faults.joined(separator: "; ")))
        }
        let measured = "measured on the pixels: \(reading.reading.summary)"
        if !reading.reading.isEnough {
            faults.insert("too faint: \(measured), under \(GloveRule.minContrast):1", at: 0)
        }
        if faults.isEmpty {
            return (true, "\(measured); whole on the screen, nothing over it\(saved.isEmpty ? "" : "; pixels kept as \(saved)")")
        }
        return (false, (reading.reading.isEnough ? measured + "; " : "") + faults.joined(separator: "; "))
    }

    private struct Pixels {
        let reading: PixelContrast.Reading
        let rgb: [UInt8]
        let width: Int
        let height: Int
        let png: Data
    }

    /// A picture of just this element, as red, green and blue per pixel,
    /// and the contrast read from it. `nil` when there is no picture, or
    /// it is one flat colour.
    private static func read(_ item: XCUIElement) -> Pixels? {
        #if canImport(UIKit)
        let shot = item.screenshot()
        guard let image = shot.image.cgImage else { return nil }
        let width = image.width, height = image.height
        guard width > 0, height > 0, width * height <= 8_000_000 else { return nil }
        var buffer = [UInt8](repeating: 0, count: width * height * 4)
        let space = CGColorSpace(name: CGColorSpace.sRGB) ?? CGColorSpaceCreateDeviceRGB()
        let drawn: Bool = buffer.withUnsafeMutableBytes { raw in
            guard let context = CGContext(
                data: raw.baseAddress, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width * 4,
                space: space, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
            ) else { return false }
            context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
            return true
        }
        guard drawn else { return nil }
        var rgb = [UInt8]()
        rgb.reserveCapacity(width * height * 3)
        var index = 0
        while index + 3 < buffer.count {
            rgb.append(buffer[index])
            rgb.append(buffer[index + 1])
            rgb.append(buffer[index + 2])
            index += 4
        }
        guard let reading = PixelContrast.read(rgb: rgb) else { return nil }
        return Pixels(reading: reading, rgb: rgb, width: width, height: height, png: shot.pngRepresentation)
        #else
        return nil
        #endif
    }

    /// Keeps the pixels that were read (a .ppm the measuring script reads,
    /// and a .png for a person), and adds a row to `contrast-readings.tsv`:
    /// verdict, screen, ratio, words, background, the .ppm's name, the line.
    /// Returns the picture's name, "" if nothing was kept.
    private func save(_ pixels: Pixels?, flag: Flag, cleared: Bool, on name: String) -> String {
        let folder = URL(fileURLWithPath: Self.failureFolder, isDirectory: true)
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        var file = ""
        var fields = [cleared ? "measured" : "FAILED", name, "", "", "", "", flag.shown]
        if let pixels {
            // A name no other reading has, in this run of the tests or an
            // earlier one into the same folder.
            let safe = name.map { $0.isLetter || $0.isNumber ? $0 : "-" }
            file = "contrast-\(Self.device)-\(String(safe))-\(UUID().uuidString.prefix(8).lowercased())"
            var ppm = Data("P6\n\(pixels.width) \(pixels.height)\n255\n".utf8)
            ppm.append(contentsOf: pixels.rgb)
            try? ppm.write(to: folder.appendingPathComponent(file + ".ppm"))
            try? pixels.png.write(to: folder.appendingPathComponent(file + ".png"))
            fields[2] = String(format: "%.2f", pixels.reading.ratio)
            fields[3] = PixelContrast.hexText(pixels.reading.words)
            fields[4] = PixelContrast.hexText(pixels.reading.background)
            fields[5] = file + ".ppm"
        }
        let row = fields.map { $0.replacingOccurrences(of: "\t", with: " ").replacingOccurrences(of: "\n", with: " ") }.joined(separator: "\t")
        let table = folder.appendingPathComponent("contrast-readings.tsv")
        let old = (try? String(contentsOf: table, encoding: .utf8)) ?? ""
        try? Data((old + row + "\n").utf8).write(to: table)
        return file.isEmpty ? "" : file + ".png"
    }

    /// The texts of the page that the edge of its room cuts right now.
    private func cutTexts() -> [String] {
        guard let room = pageRoom() else { return [] }
        let clear = room.insetBy(dx: -1.5, dy: -1.5)
        return pageTexts().compactMap { text in
            let overlap = room.intersection(text.frame)
            let cut = !clear.contains(text.frame) && !overlap.isNull && overlap.height > 2
            return cut ? "\"\(text.label.prefix(30))\"" : nil
        }
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
