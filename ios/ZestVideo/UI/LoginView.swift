import SwiftUI
import ZestCore

/// One-time setup. Step one: the tech's mobile number (a texted code is the
/// way in; email is there for anyone who needs it). Step two: six big boxes
/// for the code. No password anywhere.
///
/// Neither text field is ever rewritten while it is being edited. Rewriting a
/// field as keys arrive loses keystrokes (fast typing, a paste, the phone's
/// own number suggestion), and a tech with a digit missing never gets a code.
/// The fields hold exactly what was put in; the grouped number under the
/// field and the six boxes are drawn from it, and it is read once, when sent.
///
/// **While a field is being typed into, nothing is pinned over the keyboard.**
/// The first build on a real phone showed why: SEND MY CODE and USE EMAIL
/// INSTEAD, pinned over the keypad, left a strip so short that half of the
/// field and the whole line under it were behind them. So each step has two
/// shapes, and which one is up depends only on whether its field has the
/// cursor (`contactFocused`, `codeFocused`):
/// - no cursor: the large title, the page, and the two buttons pinned at the
///   foot of the screen, as on every other screen;
/// - cursor in the field: no large title (the top bar says Sign In), the
///   question, the field and its help line at the top of the page, and the
///   same two buttons under them in the page, full size, where they scroll
///   with it. No button is pinned, not even one: on the smallest iPhone one
///   button and the keyboard leave less room than the question, the field,
///   the number read back and the help line need.
///
/// It goes by the cursor and not by the keyboard's own notices. The cursor
/// is known in the same moment the field is tapped, so the page has its
/// typing shape before the keyboard starts to slide up, in one change and
/// not two; and with a keyboard plugged in, where none shows on the screen,
/// the same page simply has spare room under it.
///
/// **A message must not be left under the keyboard, nor leave the way on
/// under it.** A message goes up between the help line and the buttons, so
/// it pushes the buttons down: on a 6.1-inch iPhone SEND MY CODE is then
/// wholly under the keyboard, and on the smallest iPhone half of the message
/// is. So:
/// - when a message goes up, the page moves just far enough to show all of it;
/// - a message about what is in the field ("Type all 10 digits of your
///   mobile number.") keeps the keyboard, because the number has to be
///   changed, and the first key takes the message down again, which brings
///   the buttons back up;
/// - a message from the server on the first step ("No signal. Please try
///   again.") puts the keyboard away. The number was fine and the way on is
///   SEND MY CODE again, so the step goes back to its shape with both
///   buttons whole at the foot of the screen.
///
/// **With the text at its two largest sizes the number is not read back
/// under the field while it is typed** (`numberPreview`). The question and
/// the help line each take two lines then, and with a third thing under the
/// field the help line ran under the keyboard on the smallest iPhone and
/// SEND MY CODE was wholly under it on a 6.1-inch one. The field itself
/// shows the digits, at 40 points.
struct LoginView: View {
    @EnvironmentObject private var app: AppModel
    @Environment(\.dynamicTypeSize) private var typeSize

    enum Step: Equatable {
        case contact
        case code
    }

    /// How the code is sent.
    private enum Mode {
        case mobile
        case email
    }

    @State private var step: Step
    @State private var mode: Mode = .mobile
    /// The field, exactly as typed: a mobile number in any form, or an email.
    @State private var contact: String
    /// Where the code was sent: ten plain digits, or the email. The code is
    /// checked against this, whatever the field is changed to afterwards.
    @State private var sentTo: String
    /// The code field, exactly as typed, pasted or filled in by the phone.
    @State private var code: String
    /// The six digits last sent to the server, so one code goes once.
    @State private var lastSubmittedCode: String?
    @State private var busy = false
    @State private var problem: String?
    @FocusState private var contactFocused: Bool
    @FocusState private var codeFocused: Bool
    private let autoFocus: Bool

    /// The defaults are the real flow. Other values only come from `ScreenSeeds`.
    /// - Parameters:
    ///   - contact: ten digits of a mobile number to start with.
    ///   - asTyped: put them in the field as a tech types them, ten plain
    ///     digits, so that the number is read back under the field as it is
    ///     for him. Otherwise the field starts with the number grouped, and
    ///     nothing is read back.
    init(
        step: Step = .contact, contact: String = "", code: String = "", autoFocus: Bool = true,
        asTyped: Bool = false
    ) {
        let digits = PhoneFormat.digits(from: contact)
        _step = State(initialValue: step)
        _contact = State(initialValue: asTyped ? digits : PhoneFormat.display(digits))
        _sentTo = State(initialValue: digits)
        _code = State(initialValue: code)
        self.autoFocus = autoFocus
    }

    var body: some View {
        // The reader is how a step puts its page back at its top (`showTop`).
        ScrollViewReader { pages in
            Group {
                switch step {
                case .contact: contactStep
                case .code: codeStep
                }
            }
            // The cursor has arrived in a field: show the question, the
            // whole field and its help line, wherever the page was.
            .onChange(of: contactFocused) { _, focused in
                if focused { showTop(pages) }
            }
            .onChange(of: codeFocused) { _, focused in
                if focused { showTop(pages) }
            }
            // USE EMAIL INSTEAD (and back) is reached by scrolling on a small
            // phone; the new, empty field is at the top.
            .onChange(of: mode) { _, _ in
                showTop(pages)
            }
            // A message has gone up: move the page just far enough to show
            // all of it. On the smallest iPhone half of it would be under
            // the keyboard. (No anchor: a message that is whole in view
            // already moves nothing.)
            .onChange(of: problem) { _, message in
                if message != nil {
                    pages.scrollTo(ScreenAnchor.message)
                }
            }
        }
        // The page moves under a thumb without the keyboard going away: the
        // shape of a step changes with the cursor and with nothing else.
        .scrollDismissesKeyboard(.never)
    }

    /// Puts the page back at its top: the question, the field and the line
    /// under it are the first things on it.
    private func showTop(_ pages: ScrollViewProxy) {
        pages.scrollTo(ScreenAnchor.pageTop, anchor: .top)
    }

    /// The number grouped for reading back: "(916) 555-0100". Shown under
    /// the field when it says more than the field itself does.
    ///
    /// Not while the field is typed into with the text at its two largest
    /// sizes: there is then no room over the keyboard for this line as well
    /// as the help line and SEND MY CODE (see the note at the top).
    private var numberPreview: String {
        guard mode == .mobile else { return "" }
        if contactFocused, LargeType.isOn(typeSize) { return "" }
        let grouped = PhoneFormat.preview(contact)
        return grouped == contact ? "" : grouped
    }

    // MARK: Step one

    private var contactStep: some View {
        Screen(bar: "Sign In", title: "Zest Video", typing: contactFocused) {
            BodyText(mode == .mobile ? "Type your mobile number." : "Type your email.", bold: true)
            field
            if !numberPreview.isEmpty {
                // The field is left exactly as typed; this line reads it back.
                Text(numberPreview)
                    .zestFont(.rowName)
                    .foregroundColor(Theme.ink)
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
                    .accessibilityLabel("Your number: \(numberPreview)")
                    .accessibilityIdentifier("contact-preview")
            }
            BodyText(mode == .mobile ? "We will text you a 6-digit code." : "We will email you a 6-digit code.", color: Theme.softInk)
            if let problem {
                Banner(text: problem, tone: .attention)
                    .id(ScreenAnchor.message)
            }
            if contactFocused {
                // Being typed into: the buttons are here, under the help
                // line, and not pinned over the keyboard (see the top).
                VStack(spacing: 14) {
                    contactButtons
                }
                .padding(.top, 10)
            }
            VStack(alignment: .leading, spacing: 0) {
                BodyText("By signing in you agree to the Terms and Privacy Policy.", color: Theme.softInk)
                LegalLinks()
            }
            .padding(.top, 10)
        } footer: {
            contactButtons
        }
        .onAppear {
            if autoFocus { contactFocused = true }
        }
    }

    /// SEND MY CODE and the other way in: pinned at the foot of the screen,
    /// or in the page while the field is being typed into.
    @ViewBuilder
    private var contactButtons: some View {
        BigButton(title: "SEND MY CODE", kind: .primary, isBusy: busy) {
            // The second tap of a double tap on the button under this one
            // must not land here: that tap changes the way in and the page
            // goes back to its top, which can bring this button under the
            // same thumb.
            guard TapGate.pass() else { return }
            sendCode()
        }
        BigButton(title: mode == .mobile ? "USE EMAIL INSTEAD" : "USE MOBILE NUMBER INSTEAD", kind: .plain) {
            flipMode()
        }
    }

    @ViewBuilder
    private var field: some View {
        Group {
            switch mode {
            case .mobile:
                TextField("Mobile number", text: $contact)
                    .keyboardType(.phonePad)
                    .textContentType(.telephoneNumber)
                    .accessibilityLabel("Mobile number")
                    .accessibilityIdentifier("contact-field")
            case .email:
                TextField("Email", text: $contact)
                    .keyboardType(.emailAddress)
                    .textContentType(.emailAddress)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .submitLabel(.go)
                    .onSubmit(sendCode)
                    .accessibilityLabel("Email")
                    .accessibilityIdentifier("contact-field")
            }
        }
        .zestFont(.field)
        .foregroundColor(Theme.ink)
        .minimumScaleFactor(0.6)
        .focused($contactFocused)
        .padding(.horizontal, 16)
        .frame(minHeight: Theme.fieldHeight)
        .background(
            RoundedRectangle(cornerRadius: Theme.corner, style: .continuous)
                .fill(Theme.card)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Theme.corner, style: .continuous)
                .strokeBorder(Theme.blue, lineWidth: 3)
                .allowsHitTesting(false)
        )
        .contentShape(Rectangle())
        .onTapGesture { contactFocused = true }
        // An edit answers the message: take it down, so that the buttons it
        // pushed under the keyboard come back up. (This only reads
        // `contact`. Nothing rewrites it: see the note at the top.)
        .onChange(of: contact) { _, _ in
            if problem != nil {
                problem = nil
            }
        }
    }

    private func flipMode() {
        // One press, one change: a double tap must not flip it straight back.
        guard TapGate.pass() else { return }
        mode = (mode == .mobile) ? .email : .mobile
        contact = ""
        problem = nil
        contactFocused = true
    }

    private func sendCode() {
        guard !busy else { return }
        // The field is read here, once: "+1 (916) 555-0100", "1-916-555-0100"
        // and "916.555.0100" all go to the server as 9165550100.
        let value: String
        switch mode {
        case .mobile:
            switch PhoneFormat.check(contact) {
            case .ok(let digits):
                value = digits
            case .empty:
                problem = "Type your mobile number first."
                return
            case .tooShort:
                problem = "Type all 10 digits of your mobile number."
                return
            case .tooLong:
                problem = "That is more than 10 digits. Please check your mobile number."
                return
            }
        case .email:
            let typed = contact.trimmingCharacters(in: .whitespacesAndNewlines)
            guard typed.contains("@"), typed.contains(".") else {
                problem = typed.isEmpty ? "Type your email first." : "That does not look like an email. Please check it."
                return
            }
            value = typed
        }
        busy = true
        problem = nil
        Task {
            let failure = await app.requestCode(contact: value)
            busy = false
            if let failure {
                problem = failure
                if step == .code {
                    // A whole code typed while this was in the air still goes.
                    codeChanged(code)
                } else {
                    // The number was fine; the server or the signal was not.
                    // The way on is SEND MY CODE again, and with a message
                    // on the page that button is under the keyboard. So the
                    // keyboard goes, and both buttons are back at the foot
                    // of the screen. A tap on the field brings it back.
                    contactFocused = false
                }
            } else {
                sentTo = value
                code = ""
                lastSubmittedCode = nil
                step = .code
            }
        }
    }

    // MARK: Step two

    private var codeStep: some View {
        Screen(bar: "Sign In", onBack: backToContact, title: "Type your code", typing: codeFocused) {
            BodyText(mode == .mobile ? "We texted a 6-digit code to your phone." : "We emailed a 6-digit code.", bold: true)
            codeBoxes
            if busy {
                BodyText("Checking…", bold: true)
            }
            if let problem {
                Banner(text: problem, tone: .attention)
                    .id(ScreenAnchor.message)
            }
            BodyText(
                mode == .mobile
                    ? "No code? Ask the office to check your mobile number."
                    : "No code? Ask the office to check your email.",
                color: Theme.softInk
            )
            if codeFocused {
                // Being typed into: the buttons are here, under the help
                // line, and not pinned over the keyboard (see the top).
                VStack(spacing: 14) {
                    codeButtons
                }
                .padding(.top, 10)
            }
        } footer: {
            codeButtons
        }
        .onAppear {
            if autoFocus { codeFocused = true }
        }
    }

    /// SEND A NEW CODE and BACK: pinned at the foot of the screen, or in the
    /// page while the code is being typed.
    @ViewBuilder
    private var codeButtons: some View {
        BigButton(title: "SEND A NEW CODE", kind: .plain, isBusy: busy) {
            problem = nil
            sendCode()
        }
        BigButton(title: "BACK", kind: .plain, action: backToContact)
    }

    /// The arrow in the top bar and the big BACK button do the same thing.
    private func backToContact() {
        step = .contact
        problem = nil
    }

    /// Six boxes drawn over one hidden text field, so iOS can fill the code
    /// in from the text message by itself. The boxes are as large as fit six
    /// across the screen.
    private var codeBoxes: some View {
        let spacing: CGFloat = Theme.screenWidth < 360 ? 6 : 8
        let boxWidth = (Theme.screenWidth - 2 * Theme.gutter - 5 * spacing) / 6
        let boxHeight = min(120, max(76, boxWidth * 1.5))
        return ZStack {
            TextField("", text: $code)
                .keyboardType(.numberPad)
                .textContentType(.oneTimeCode)
                .focused($codeFocused)
                .foregroundColor(.clear)
                .tint(.clear)
                .frame(maxWidth: .infinity, minHeight: boxHeight)
                .accessibilityLabel("6-digit code")
                .accessibilityIdentifier("code-field")
                .onChange(of: code) { _, newValue in
                    codeChanged(newValue)
                }
            HStack(spacing: spacing) {
                ForEach(0..<6, id: \.self) { index in
                    codeBox(index, height: boxHeight)
                }
            }
            .allowsHitTesting(false)
            .accessibilityHidden(true)
        }
        .contentShape(Rectangle())
        .onTapGesture { codeFocused = true }
    }

    /// The field changed: typed, pasted, or filled in all at once. Its text
    /// is not touched; a whole code is sent, once.
    private func codeChanged(_ text: String) {
        guard !busy, let digits = CodeEntry.shouldSubmit(text: text, lastSubmitted: lastSubmittedCode) else { return }
        submitCode(digits)
    }

    private func codeBox(_ index: Int, height: CGFloat) -> some View {
        // The first six digits of whatever is in the field.
        let digits = Array(CodeEntry.digits(from: code))
        let filled = index < digits.count
        let isNext = index == digits.count
        return Text(filled ? String(digits[index]) : "")
            .zestFont(.codeDigit)
            .foregroundColor(Theme.ink)
            .lineLimit(1)
            .minimumScaleFactor(0.5)
            .frame(maxWidth: .infinity, minHeight: height)
            // White boxes with a grey edge; the one being filled has a blue edge.
            .background(
                RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Theme.card)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .strokeBorder(isNext ? Theme.blue : Theme.hairline, lineWidth: isNext ? 4 : 2)
            )
    }

    private func submitCode(_ digits: String) {
        guard !busy else { return }
        busy = true
        problem = nil
        lastSubmittedCode = digits
        let value = sentTo
        Task {
            let failure = await app.verify(contact: value, code: digits)
            busy = false
            if let failure {
                problem = failure
                // Start the next try from empty boxes. The check is over, so
                // no key is in the air to be lost.
                code = ""
                lastSubmittedCode = nil
                codeFocused = true
            }
        }
    }
}
