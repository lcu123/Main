import SwiftUI

/// The app's one kind of button: full width, 88 pt tall on a big phone
/// (never under 76), words in heavy 30 pt type. Big enough to hit with a
/// gloved thumb.
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
                    .zestFont(.button)
                    .multilineTextAlignment(.center)
                    .lineLimit(2)
                    .minimumScaleFactor(0.6)
            }
            .frame(maxWidth: .infinity, minHeight: Theme.buttonHeight)
            .padding(.horizontal, 14)
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

/// A screen: a big title, content that scrolls when it has to, and the
/// buttons pinned at the bottom where a thumb reaches them.
///
/// - The scrolling part never runs up under the clock and the camera island:
///   a strip sits between it and the top of the screen.
/// - The scrolling part ends with spare room, and a line marks the edge above
///   the buttons, so the last thing on the page is never half-hidden.
/// - On a very short screen the buttons join the scrolling part, so nothing
///   is squeezed out.
struct Screen<Content: View, Footer: View>: View {
    var title: String?
    /// Open scrolled to the end. Only ever true in screenshot mode.
    var startAtBottom = false
    @ViewBuilder var content: () -> Content
    @ViewBuilder var footer: () -> Footer

    var body: some View {
        VStack(spacing: 0) {
            // With this strip above it the scroll view stops at the safe
            // area instead of sliding its content under the status bar; the
            // strip's own background paints that area solid.
            Color.clear
                .frame(height: 1)
                .background(Theme.background.ignoresSafeArea(edges: .top))
                .zIndex(1)

            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if let title {
                        Text(title)
                            .zestFont(.title)
                            .foregroundColor(Theme.ink)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityAddTraits(.isHeader)
                    }
                    content()
                    if Theme.compactHeight {
                        VStack(spacing: 14) {
                            footer()
                        }
                        .padding(.top, 10)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, Theme.gutter)
                .padding(.top, 14)
                // Spare room, so the last line clears the edge when scrolled.
                .padding(.bottom, 32)
            }
            .defaultScrollAnchor(startAtBottom ? .bottom : nil)

            if Theme.compactHeight {
                Color.clear.frame(height: 1)
            } else {
                VStack(spacing: 14) {
                    footer()
                }
                .padding(.horizontal, Theme.gutter)
                .padding(.top, 14)
                .padding(.bottom, 14)
                .frame(maxWidth: .infinity)
                .background(Theme.background.ignoresSafeArea(edges: .bottom))
                // A clear edge: what is above this line scrolls.
                .overlay(alignment: .top) {
                    Rectangle()
                        .fill(Theme.card)
                        .frame(height: 3)
                }
            }
        }
        .background(Theme.background.ignoresSafeArea())
    }
}

/// Plain text at the app's ordinary size (26 pt).
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
            .zestFont(bold ? .bodyBold : .body)
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
            .zestFont(.bodyBold)
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

/// The customer's name and street, as large as they go: this is how the tech
/// knows he has the right house.
struct StopHeadline: View {
    let name: String
    let address: String
    var cityLine: String = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(name)
                .zestFont(.heroName)
                .foregroundColor(Theme.ink)
                .lineLimit(3)
                .minimumScaleFactor(0.6)
                .fixedSize(horizontal: false, vertical: true)
            if !address.isEmpty {
                Text(address)
                    .zestFont(.heroStreet)
                    .foregroundColor(Theme.ink)
                    .lineLimit(3)
                    .minimumScaleFactor(0.6)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !cityLine.isEmpty {
                Text(cityLine)
                    .zestFont(.body)
                    .foregroundColor(Theme.softInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

/// An underlined word that does something. At least 48 pt tall to tap.
struct TextLink: View {
    let title: String
    var color: Color = Theme.purple
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .underline()
                .zestFont(.link)
                .foregroundColor(color)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
                .frame(minWidth: 48, minHeight: 48)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

/// "Terms" and "Privacy": open the server's pages in Safari.
struct LegalLinks: View {
    @EnvironmentObject private var app: AppModel

    var body: some View {
        HStack(spacing: 28) {
            TextLink(title: "Terms") {
                app.openTerms()
            }
            .accessibilityHint("Opens in Safari")
            TextLink(title: "Privacy") {
                app.openPrivacy()
            }
            .accessibilityLabel("Privacy Policy")
            .accessibilityHint("Opens in Safari")
        }
    }
}
