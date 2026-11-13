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
struct LoginView: View {
    @EnvironmentObject private var app: AppModel

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
    /// - Parameter contact: ten digits of a mobile number to start with.
    init(step: Step = .contact, contact: String = "", code: String = "", autoFocus: Bool = true) {
        let digits = PhoneFormat.digits(from: contact)
        _step = State(initialValue: step)
        _contact = State(initialValue: PhoneFormat.display(digits))
        _sentTo = State(initialValue: digits)
        _code = State(initialValue: code)
        self.autoFocus = autoFocus
    }

    var body: some View {
        switch step {
        case .contact: contactStep
        case .code: codeStep
        }
    }

    /// The number grouped for reading back: "(916) 555-0100". Shown under
    /// the field when it says more than the field itself does.
    private var numberPreview: String {
        guard mode == .mobile else { return "" }
        let grouped = PhoneFormat.preview(contact)
        return grouped == contact ? "" : grouped
    }

    // MARK: Step one

    private var contactStep: some View {
        Screen(bar: "Sign In", title: "Zest Video") {
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
            }
            VStack(alignment: .leading, spacing: 0) {
                BodyText("By signing in you agree to the Terms and Privacy Policy.", color: Theme.softInk)
                LegalLinks()
            }
            .padding(.top, 10)
        } footer: {
            BigButton(title: "SEND MY CODE", kind: .primary, isBusy: busy, action: sendCode)
            BigButton(title: mode == .mobile ? "USE EMAIL INSTEAD" : "USE MOBILE NUMBER INSTEAD", kind: .plain) {
                flipMode()
            }
        }
        .onAppear {
            if autoFocus { contactFocused = true }
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
        // No `.onChange` that rewrites `contact`: see the note at the top.
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
                // A whole code typed while this was in the air still goes.
                if step == .code { codeChanged(code) }
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
        Screen(bar: "Sign In", onBack: backToContact, title: "Type your code") {
            BodyText(mode == .mobile ? "We texted a 6-digit code to your phone." : "We emailed a 6-digit code.", bold: true)
            codeBoxes
            if busy {
                BodyText("Checking…", bold: true)
            }
            if let problem {
                Banner(text: problem, tone: .attention)
            }
            BodyText(
                mode == .mobile
                    ? "No code? Ask the office to check your mobile number."
                    : "No code? Ask the office to check your email.",
                color: Theme.softInk
            )
        } footer: {
            BigButton(title: "SEND A NEW CODE", kind: .plain, isBusy: busy) {
                problem = nil
                sendCode()
            }
            BigButton(title: "BACK", kind: .plain, action: backToContact)
        }
        .onAppear {
            if autoFocus { codeFocused = true }
        }
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
