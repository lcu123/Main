import SwiftUI
import ZestCore

/// One-time setup. Step one: the tech's mobile number (a texted code is the
/// way in; email is there for anyone who needs it). Step two: six big boxes
/// for the code. No password anywhere.
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
    /// What is in the field: a grouped number "(916) 555-0100", or an email.
    @State private var contact: String
    @State private var code: String
    @State private var busy = false
    @State private var problem: String?
    @FocusState private var contactFocused: Bool
    @FocusState private var codeFocused: Bool
    private let autoFocus: Bool

    /// The defaults are the real flow. Other values only come from `ScreenSeeds`.
    /// - Parameter contact: ten digits of a mobile number to start with.
    init(step: Step = .contact, contact: String = "", code: String = "", autoFocus: Bool = true) {
        _step = State(initialValue: step)
        _contact = State(initialValue: PhoneFormat.display(PhoneFormat.digits(from: contact)))
        _code = State(initialValue: code)
        self.autoFocus = autoFocus
    }

    var body: some View {
        switch step {
        case .contact: contactStep
        case .code: codeStep
        }
    }

    /// What goes to the server: ten plain digits, or the email as typed.
    private var contactValue: String {
        switch mode {
        case .mobile: return PhoneFormat.digits(from: contact)
        case .email: return contact.trimmingCharacters(in: .whitespacesAndNewlines)
        }
    }

    // MARK: Step one

    private var contactStep: some View {
        Screen(bar: "Sign In", title: "Zest Video") {
            BodyText(mode == .mobile ? "Type your mobile number." : "Type your email.", bold: true)
            field
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
        .onChange(of: contact) { _, newValue in
            // Group the digits as they are typed: (916) 555-0100.
            guard mode == .mobile else { return }
            let grouped = PhoneFormat.display(PhoneFormat.digits(from: newValue))
            if grouped != newValue {
                contact = grouped
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
        let value = contactValue
        switch mode {
        case .mobile:
            guard PhoneFormat.isComplete(value) else {
                problem = value.isEmpty ? "Type your mobile number first." : "Type all 10 digits of your mobile number."
                return
            }
        case .email:
            guard value.contains("@"), value.contains(".") else {
                problem = value.isEmpty ? "Type your email first." : "That does not look like an email. Please check it."
                return
            }
        }
        busy = true
        problem = nil
        Task {
            let failure = await app.requestCode(contact: value)
            busy = false
            if let failure {
                problem = failure
            } else {
                code = ""
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
                    let digits = String(newValue.filter { $0.isASCII && $0.isNumber }.prefix(6))
                    if digits != newValue {
                        code = digits
                        return
                    }
                    if digits.count == 6 {
                        submitCode(digits)
                    }
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

    private func codeBox(_ index: Int, height: CGFloat) -> some View {
        let digits = Array(code)
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
        let value = contactValue
        Task {
            let failure = await app.verify(contact: value, code: digits)
            busy = false
            if let failure {
                problem = failure
                code = ""
                codeFocused = true
            }
        }
    }
}
