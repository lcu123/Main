import SwiftUI

/// The app's one kind of button: full width, at least 76 pt tall, words in
/// heavy 28 pt type. Big enough to hit with a gloved thumb.
struct BigButton: View {
    enum Kind {
        /// Purple with white words: the main action.
        case primary
        /// Lime with black words: yes / go / saved.
        case go
        /// Yellow with black words: needs attention.
        case attention
        /// White with a purple outline and black words: the other choice.
        case plain
        /// Black with white words: delete and other serious actions.
        case dark
    }

    let title: String
    var kind: Kind = .primary
    var isBusy: Bool = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 14) {
                if isBusy {
                    ProgressView()
                        .progressViewStyle(.circular)
                        .tint(BigButtonStyle.foreground(kind))
                        .scaleEffect(1.4)
                        .accessibilityHidden(true)
                }
                Text(title)
                    .font(Theme.button)
                    .multilineTextAlignment(.center)
                    .lineLimit(2)
                    .minimumScaleFactor(0.6)
            }
            .frame(maxWidth: .infinity, minHeight: Theme.buttonHeight)
            .padding(.horizontal, 16)
            .padding(.vertical, 6)
        }
        .buttonStyle(BigButtonStyle(kind: kind))
        .disabled(isBusy)
        .accessibilityLabel(title.capitalized)
    }
}

struct BigButtonStyle: ButtonStyle {
    let kind: BigButton.Kind

    static func foreground(_ kind: BigButton.Kind) -> Color {
        switch kind {
        case .primary, .dark: return Theme.white
        case .go, .attention, .plain: return Theme.black
        }
    }

    private func fill(pressed: Bool) -> Color {
        switch kind {
        case .primary: return pressed ? Theme.deepViolet : Theme.purple
        case .go: return pressed ? Theme.lime.opacity(0.7) : Theme.lime
        case .attention: return pressed ? Theme.yellow.opacity(0.7) : Theme.yellow
        case .plain: return pressed ? Theme.card : Theme.white
        case .dark: return pressed ? Theme.softInk : Theme.black
        }
    }

    private var border: Color {
        switch kind {
        case .primary: return Theme.deepViolet
        case .go, .attention, .dark: return Theme.black
        case .plain: return Theme.purple
        }
    }

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundColor(BigButtonStyle.foreground(kind))
            .background(
                RoundedRectangle(cornerRadius: Theme.corner, style: .continuous)
                    .fill(fill(pressed: configuration.isPressed))
            )
            .overlay(
                RoundedRectangle(cornerRadius: Theme.corner, style: .continuous)
                    .strokeBorder(border, lineWidth: 4)
            )
            .contentShape(RoundedRectangle(cornerRadius: Theme.corner, style: .continuous))
    }
}

/// A screen: a big title, content that can scroll if the text size is turned
/// up, and the buttons pinned at the bottom where a thumb reaches them.
struct Screen<Content: View, Footer: View>: View {
    var title: String?
    /// Open scrolled to the end. Only ever true in screenshot mode.
    var startAtBottom = false
    @ViewBuilder var content: () -> Content
    @ViewBuilder var footer: () -> Footer

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if let title {
                        Text(title)
                            .font(Theme.title)
                            .foregroundColor(Theme.ink)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityAddTraits(.isHeader)
                    }
                    content()
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, Theme.gutter)
                .padding(.top, 20)
                .padding(.bottom, 16)
            }
            .defaultScrollAnchor(startAtBottom ? .bottom : nil)
            VStack(spacing: 14) {
                footer()
            }
            .padding(.horizontal, Theme.gutter)
            .padding(.top, 12)
            .padding(.bottom, 14)
            .background(Theme.background)
        }
        .background(Theme.background.ignoresSafeArea())
    }
}

/// Plain 22 pt text.
struct BodyText: View {
    let text: String
    var bold = false
    var color: Color = Theme.ink

    init(_ text: String, bold: Bool = false, color: Color = Theme.ink) {
        self.text = text
        self.bold = bold
        self.color = color
    }

    var body: some View {
        Text(text)
            .font(bold ? Theme.bodyBold : Theme.body)
            .foregroundColor(color)
            .fixedSize(horizontal: false, vertical: true)
    }
}

/// A full-width colored strip with a short message, e.g. "No signal."
struct Banner: View {
    enum Tone {
        case attention
        case info
        case good
    }

    let text: String
    var tone: Tone = .info

    var body: some View {
        Text(text)
            .font(Theme.bodyBold)
            .foregroundColor(Theme.black)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
            .background(
                RoundedRectangle(cornerRadius: Theme.corner, style: .continuous).fill(fill)
            )
    }

    private var fill: Color {
        switch tone {
        case .attention: return Theme.yellow
        case .info: return Theme.card
        case .good: return Theme.lime
        }
    }
}

/// The customer's name and address in very large type.
struct StopHeadline: View {
    let name: String
    let address: String
    var cityLine: String = ""
    var onDark = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(name)
                .font(.system(size: 40, weight: .heavy, design: .rounded))
                .foregroundColor(onDark ? Theme.white : Theme.ink)
                .lineLimit(3)
                .minimumScaleFactor(0.6)
                .fixedSize(horizontal: false, vertical: true)
            if !address.isEmpty {
                Text(address)
                    .font(.system(size: 32, weight: .bold, design: .rounded))
                    .foregroundColor(onDark ? Theme.white : Theme.ink)
                    .lineLimit(3)
                    .minimumScaleFactor(0.6)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !cityLine.isEmpty {
                Text(cityLine)
                    .font(Theme.body)
                    .foregroundColor(onDark ? Theme.white : Theme.softInk)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}
