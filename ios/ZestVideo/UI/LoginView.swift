import SwiftUI

/// One-time setup. Step one: the phone number or email on the tech's record.
/// Step two: six big boxes for the code. No password anywhere.
struct LoginView: View {
    @EnvironmentObject private var app: AppModel

    enum Step: Equatable {
        case contact
        case code
    }

    @State private var step: Step
    @State private var contact: String
    @State private var code: String
    @State private var busy = false
    @State private var problem: String?
    @FocusState private var contactFocused: Bool
    @FocusState private var codeFocused: Bool

    /// The defaults are the real flow. Other values only come from `ScreenSeeds`.
    init(step: Step = .contact, contact: String = "", code: String = "") {
        _step = State(initialValue: step)
        _contact = State(initialValue: contact)
        _code = State(initialValue: code)
    }

    var body: some View {
        switch step {
        case .contact: contactStep
        case .code: codeStep
        }
    }

    // MARK: Step one

    private var contactStep: some View {
        Screen(title: "Zest Video") {
            BodyText("Type your phone number or your email.")
            TextField("Phone or email", text: $contact)
                .font(.system(size: 30, weight: .bold, design: .rounded))
                .keyboardType(.emailAddress)
                .textContentType(.username)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .submitLabel(.go)
                .focused($contactFocused)
                .onSubmit(sendCode)
                .padding(.horizontal, 16)
                .frame(minHeight: 84)
                .background(
                    RoundedRectangle(cornerRadius: Theme.corner, style: .continuous)
                        .strokeBorder(Theme.purple, lineWidth: 4)
                )
                .accessibilityLabel("Phone number or email")
            BodyText("We will send you a 6-digit code.", color: Theme.softInk)
            if let problem {
                Banner(text: problem, tone: .attention)
            }
        } footer: {
            BigButton(title: "SEND MY CODE", kind: .primary, isBusy: busy, action: sendCode)
        }
        .onAppear { contactFocused = true }
    }

    private func sendCode() {
        let trimmed = contact.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !busy else {
            if trimmed.isEmpty { problem = "Type your phone number or email first." }
            return
        }
        busy = true
        problem = nil
        Task {
            let failure = await app.requestCode(contact: trimmed)
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
        Screen(title: "Type your code") {
            BodyText("We sent a 6-digit code to your phone and your email.")
            codeBoxes
            if busy {
                BodyText("Checking…", bold: true)
            }
            if let problem {
                Banner(text: problem, tone: .attention)
            }
        } footer: {
            BigButton(title: "SEND A NEW CODE", kind: .plain, isBusy: busy) {
                problem = nil
                sendCode()
            }
            BigButton(title: "BACK", kind: .plain) {
                step = .contact
                problem = nil
            }
        }
        .onAppear { codeFocused = true }
    }

    /// Six boxes drawn over one hidden text field, so iOS can fill the code
    /// in from the text message by itself.
    private var codeBoxes: some View {
        ZStack {
            TextField("", text: $code)
                .keyboardType(.numberPad)
                .textContentType(.oneTimeCode)
                .focused($codeFocused)
                .foregroundColor(.clear)
                .tint(.clear)
                .frame(maxWidth: .infinity, minHeight: 88)
                .accessibilityLabel("6-digit code")
                .onChange(of: code) { _, newValue in
                    let digits = String(newValue.filter { $0.isNumber }.prefix(6))
                    if digits != newValue {
                        code = digits
                        return
                    }
                    if digits.count == 6 {
                        submitCode(digits)
                    }
                }
            HStack(spacing: 8) {
                ForEach(0..<6, id: \.self) { index in
                    codeBox(index)
                }
            }
            .allowsHitTesting(false)
            .accessibilityHidden(true)
        }
        .contentShape(Rectangle())
        .onTapGesture { codeFocused = true }
    }

    private func codeBox(_ index: Int) -> some View {
        let digits = Array(code)
        let filled = index < digits.count
        return Text(filled ? String(digits[index]) : "")
            .font(.system(size: 44, weight: .heavy, design: .rounded))
            .foregroundColor(Theme.ink)
            .frame(maxWidth: .infinity, minHeight: 88)
            .background(
                RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Theme.card)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .strokeBorder(index == digits.count ? Theme.brightPurple : Theme.purple, lineWidth: index == digits.count ? 5 : 3)
            )
    }

    private func submitCode(_ digits: String) {
        guard !busy else { return }
        busy = true
        problem = nil
        let trimmed = contact.trimmingCharacters(in: .whitespacesAndNewlines)
        Task {
            let failure = await app.verify(contact: trimmed, code: digits)
            busy = false
            if let failure {
                problem = failure
                code = ""
                codeFocused = true
            }
        }
    }
}
