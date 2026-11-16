import SwiftUI

// The building blocks every screen is made of: the big button, the screen
// frame with its top bar, the tab bar, cards, labels and chips.

// MARK: - Buttons

/// The app's one kind of button: full width, 88 pt tall on a big phone
/// (never under 76), words in bold 30 pt capitals. Big enough to hit with a
/// gloved thumb.
struct BigButton: View {
    enum Kind {
        /// Solid blue with white words: the main action.
        case primary
        /// White with blue words and a blue outline: the other choice.
        case plain
        /// Amber with dark words: go ahead despite a warning.
        case attention
        /// Dark with white words: delete and other serious actions. Never red.
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
                    .tracking(0.5)
                    .zestFont(.button)
                    .multilineTextAlignment(.center)
                    .lineLimit(2)
                    .minimumScaleFactor(0.6)
            }
            .frame(maxWidth: .infinity, minHeight: Theme.buttonHeight)
            .padding(.horizontal, 14)
            .padding(.vertical, 6)
        }
        .buttonStyle(BigButtonStyle(kind: kind, name: title))
        .disabled(isBusy)
        .accessibilityLabel(title.capitalized)
        // For UI tests: the button is found by the words on it.
        .accessibilityIdentifier(title)
    }
}

struct BigButtonStyle: ButtonStyle {
    let kind: BigButton.Kind
    /// The words on the button. Only UI tests use it (see `makeBody`).
    var name = ""

    static func foreground(_ kind: BigButton.Kind) -> Color {
        switch kind {
        case .primary, .dark: return Theme.white
        case .plain: return Theme.blueInk
        case .attention: return Theme.ink
        }
    }

    private func fill(pressed: Bool) -> Color {
        switch kind {
        case .primary: return pressed ? Theme.bluePressed : Theme.blue
        case .plain: return pressed ? Theme.paleCyan : Theme.card
        case .attention: return pressed ? Theme.amber.opacity(0.75) : Theme.amber
        case .dark: return pressed ? Theme.softInk : Theme.ink
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
                    .strokeBorder(Theme.blue, lineWidth: kind == .plain ? 2 : 0)
            )
            #if targetEnvironment(simulator)
            // UI tests only: say when a finger came down on this button. If
            // a tap then does nothing, the report can tell a touch that
            // never arrived from one the button felt and did not act on.
            .onChange(of: configuration.isPressed) { _, pressed in
                if pressed {
                    UITestMode.note("press \(name)")
                }
            }
            #endif
            .contentShape(RoundedRectangle(cornerRadius: Theme.corner, style: .continuous))
    }
}

// MARK: - Double taps

/// A screen that has just appeared ignores touches for a moment.
///
/// A gloved thumb often taps twice. Without this, the second tap lands on
/// whatever the first tap brought up. DELETE VIDEO is followed by YES,
/// DELETE in exactly the same place, and THIS IS IT by YES, STILL RIGHT, so
/// both questions could be answered by accident; on a small phone the record
/// button can sit under THIS IS IT too. With this, the second tap of a
/// double tap falls on a screen that is not listening yet.
///
/// The sheet opens once and stays open: only a new screen starts closed.
/// (It used to close again on every "appeared" SwiftUI sent for the
/// screen. SwiftUI may send more than one, and a second one would have
/// made a screen that had been up for seconds swallow a tap on a button
/// in plain view.)
struct SettleFirst: ViewModifier {
    /// Longer than a double tap, far shorter than reading the screen.
    static let seconds: Double = 0.4

    @State private var settled = false
    #if targetEnvironment(simulator)
    /// UI tests only: when this screen first appeared, and how often.
    @State private var seen = Sightings()

    private final class Sightings {
        var first: Date?
        var count = 0
    }
    #endif

    func body(content: Content) -> some View {
        content
            // A clear sheet over the screen takes the touches for that
            // moment. The screen under it is untouched, so the cursor can
            // still be put in a text field as it appears.
            .overlay {
                if !settled {
                    Color.clear
                        .contentShape(Rectangle())
                        .onTapGesture {
                            #if targetEnvironment(simulator)
                            // A failed UI test can then say that its tap
                            // was kept here, and how old the screen was.
                            let age = String(format: "%.2f", Date().timeIntervalSince(seen.first ?? Date()))
                            UITestMode.note("shield kept a tap \(age) s after its screen appeared")
                            #endif
                        }
                        .ignoresSafeArea()
                        .accessibilityHidden(true)
                }
            }
            #if targetEnvironment(simulator)
            .onAppear {
                // UI tests only: was "appeared" sent twice for one screen?
                seen.count += 1
                if let first = seen.first {
                    let later = String(format: "%.2f", Date().timeIntervalSince(first))
                    UITestMode.note("shield: its screen appeared again (time \(seen.count)), \(later) s after the first")
                } else {
                    seen.first = Date()
                }
            }
            #endif
            // `task`, not `onAppear`: it belongs to this screen's one life,
            // and it only ever opens the sheet.
            .task {
                try? await Task.sleep(nanoseconds: UInt64(SettleFirst.seconds * 1_000_000_000))
                settled = true
            }
    }
}

extension View {
    /// Ignore touches for the first moment after this view appears.
    func settleFirst() -> some View {
        modifier(SettleFirst())
    }
}

/// For a switch that stays on the same screen: one press must not count
/// twice. Without this a double tap turns "I recommended something" on and
/// straight off again, and the tech thinks it is on.
@MainActor
enum TapGate {
    private static var lastTap = Date.distantPast

    /// False for a tap that comes right behind another one.
    static func pass() -> Bool {
        let now = Date()
        guard now.timeIntervalSince(lastTap) >= SettleFirst.seconds else { return false }
        lastTap = now
        return true
    }
}

// MARK: - The screen frame

/// The two home screens, which share the tab bar.
enum HomeTab: Equatable {
    case route
    case videos
}

/// A screen: a white top bar with the screen's name, a page that scrolls when
/// it has to, and either the big buttons or the tab bar at the bottom.
///
/// - The page never runs up under the clock and the camera island: the top
///   bar sits between it and the top of the screen and paints that area white.
/// - The page ends with spare room, so the last thing on it is never
///   half-hidden behind the buttons.
/// - On a very short screen the buttons join the scrolling page, so nothing
///   is squeezed out.
struct Screen<Content: View, Footer: View>: View {
    /// The screen's short name, centred in the top bar.
    let bar: String
    /// A second, blue line under the name in the top bar (the date on My Route).
    var barDetail: String?
    /// Where the back arrow goes. The big BACK button at the bottom does the same.
    var onBack: (() -> Void)?
    /// The question, large, at the top of the page.
    var title: String?
    /// Show the tab bar at the bottom (the two home screens).
    var tab: HomeTab?
    /// Open scrolled to the end. Only ever true in screenshot mode.
    var startAtBottom = false
    /// The page holds something that can give way (the clip on the review
    /// screen). It is laid out to fill exactly the room there is, so nothing
    /// is hidden below the fold; it only scrolls if even the smallest
    /// version will not fit.
    var squeeze = false
    @ViewBuilder var content: () -> Content
    @ViewBuilder var footer: () -> Footer

    var body: some View {
        VStack(spacing: 0) {
            TopBar(title: bar, detail: barDetail, onBack: onBack)

            if squeeze {
                GeometryReader { room in
                    ScrollView {
                        page
                            .padding(.bottom, 12)
                            // At least as tall as the room: whatever can
                            // stretch fills it, and no more.
                            .frame(minHeight: room.size.height, alignment: .top)
                    }
                }
            } else {
                ScrollView {
                    page
                        // Spare room, so the last line clears the edge when scrolled.
                        .padding(.bottom, 32)
                }
                .defaultScrollAnchor(startAtBottom ? .bottom : nil)
            }

            if let tab {
                TabBar(selected: tab)
            } else if Theme.compactHeight {
                Color.clear.frame(height: 1)
            } else {
                VStack(spacing: 14) {
                    footer()
                }
                .padding(.horizontal, Theme.gutter)
                .padding(.top, 14)
                .padding(.bottom, 14)
                .frame(maxWidth: .infinity)
                .background(Theme.card.ignoresSafeArea(edges: .bottom))
                .overlay(alignment: .top) {
                    Hairline()
                }
            }
        }
        .background(Theme.page.ignoresSafeArea())
        // The second tap of a double tap must not answer this screen.
        .settleFirst()
    }

    /// The title, the content and (on a very short screen) the buttons.
    private var page: some View {
        VStack(alignment: .leading, spacing: 18) {
            if let title {
                Text(title)
                    .zestFont(.title)
                    .foregroundColor(Theme.titleInk)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityAddTraits(.isHeader)
                    .accessibilityIdentifier("screen-title")
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
    }
}

extension Screen where Footer == EmptyView {
    /// A screen with nothing pinned at the bottom but the tab bar.
    init(
        bar: String, barDetail: String? = nil, tab: HomeTab, startAtBottom: Bool = false,
        @ViewBuilder content: @escaping () -> Content
    ) {
        self.init(
            bar: bar, barDetail: barDetail, onBack: nil, title: nil, tab: tab, startAtBottom: startAtBottom,
            squeeze: false, content: content, footer: { EmptyView() }
        )
    }
}

/// The white bar across the top: the screen's name centred and bold, a
/// hairline under it, and (where there is a way back) a back arrow on the left.
struct TopBar: View {
    let title: String
    /// A second line in blue, e.g. the day: "Friday, Nov 13".
    var detail: String?
    var onBack: (() -> Void)?

    var body: some View {
        ZStack {
            VStack(spacing: 0) {
                Text(title)
                    .zestFont(.barTitle)
                    .foregroundColor(Theme.titleInk)
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
                    .accessibilityAddTraits(.isHeader)
                    .accessibilityIdentifier("bar-title")
                if let detail {
                    Text(detail)
                        .zestFont(.label)
                        .foregroundColor(Theme.blueInk)
                        .lineLimit(1)
                        .minimumScaleFactor(0.7)
                }
            }
            .padding(.horizontal, Theme.backTarget + 20)
            .padding(.vertical, 5)

            if let onBack {
                HStack {
                    Button(action: onBack) {
                        Image(systemName: "arrow.left")
                            .font(.system(size: 28, weight: .heavy))
                            .foregroundColor(Theme.cyan)
                            .frame(width: Theme.backTarget, height: Theme.backTarget)
                            .background(Circle().fill(Theme.paleCyan))
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Back")
                    .accessibilityIdentifier("bar-back")
                    Spacer(minLength: 0)
                }
                .padding(.leading, 10)
            }
        }
        .frame(maxWidth: .infinity, minHeight: Theme.barHeight)
        // White right up to the top of the screen, behind the clock.
        .background(Theme.card.ignoresSafeArea(edges: .top))
        .overlay(alignment: .bottom) {
            Hairline()
        }
        // Above the scrolling page, so nothing can slide over the bar.
        .zIndex(1)
    }
}

/// The bar at the bottom of the two home screens: a symbol with a word under
/// it for each. The chosen one is navy with a navy bar along its top edge.
/// Taller than usual, with large words, for gloves.
struct TabBar: View {
    @EnvironmentObject private var app: AppModel
    let selected: HomeTab

    var body: some View {
        HStack(spacing: 0) {
            tab(.route, title: "My Route", symbol: "map") {
                app.backToToday()
            }
            tab(.videos, title: "My Videos", symbol: "video") {
                app.showQueue()
            }
        }
        .frame(maxWidth: .infinity)
        .background(Theme.card.ignoresSafeArea(edges: .bottom))
        .overlay(alignment: .top) {
            Hairline()
        }
    }

    private func tab(_ which: HomeTab, title: String, symbol: String, action: @escaping () -> Void) -> some View {
        let isSelected = which == selected
        return Button {
            if !isSelected { action() }
        } label: {
            VStack(spacing: 4) {
                Image(systemName: isSelected ? symbol + ".fill" : symbol)
                    .font(.system(size: 28, weight: .semibold))
                    .accessibilityHidden(true)
                Text(title)
                    .zestFont(.tab)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
            }
            .foregroundColor(isSelected ? Theme.navy : Theme.softInk)
            .frame(maxWidth: .infinity, minHeight: Theme.tabBarHeight)
            .overlay(alignment: .top) {
                Rectangle()
                    .fill(isSelected ? Theme.navy : Color.clear)
                    .frame(height: 5)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(title)
        .accessibilityAddTraits(isSelected ? [.isButton, .isSelected] : .isButton)
        .accessibilityIdentifier(which == .route ? "tab-route" : "tab-videos")
    }
}

/// The two largest text sizes the app follows. At these a page of text is
/// often taller than the room above its buttons (it scrolls), so screens
/// put what has to be read first and draw the customer's card smaller.
enum LargeType {
    static func isOn(_ size: DynamicTypeSize) -> Bool {
        size >= .xxLarge
    }
}

// MARK: - Surfaces

/// A white card with large round corners and a soft shadow.
struct Card<Content: View>: View {
    /// The card's color: white, or a pale tint for a warning or a success.
    var tint: Color = Theme.card
    /// A colored edge, e.g. blue round the stop the app thinks you are at.
    var outline: Color?
    @ViewBuilder var content: () -> Content

    var body: some View {
        content()
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(tint)
            .clipShape(RoundedRectangle(cornerRadius: Theme.corner, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: Theme.corner, style: .continuous)
                    .strokeBorder(outline ?? Color.clear, lineWidth: outline == nil ? 0 : 3)
            )
            .shadow(color: Color.black.opacity(0.13), radius: 6, x: 0, y: 2)
    }
}

/// A thin grey line: inside cards, and under and over the bars.
struct Hairline: View {
    var body: some View {
        Rectangle()
            .fill(Theme.hairline)
            .frame(height: 1)
            .accessibilityHidden(true)
    }
}

// MARK: - Text

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

/// A small grey label in capitals with a little space between the letters:
/// "CUSTOMER", "50 FT AWAY • MOWING". Small only next to the rest of the
/// app; it is still 22 pt.
struct SmallLabel: View {
    let text: String
    var color: Color = Theme.softInk

    init(_ text: String, color: Color = Theme.softInk) {
        self.text = text
        self.color = color
    }

    var body: some View {
        Text(text.uppercased())
            .tracking(1)
            .zestFont(.label)
            .foregroundColor(color)
            .fixedSize(horizontal: false, vertical: true)
    }
}

/// Bold capitals for something that can be done or that is true right now:
/// "RECORD VIDEO", "CLOSEST STOP". Blue unless told otherwise.
struct ActionText: View {
    let text: String
    var color: Color = Theme.blueInk

    init(_ text: String, color: Color = Theme.blueInk) {
        self.text = text
        self.color = color
    }

    var body: some View {
        Text(text.uppercased())
            .tracking(0.6)
            .zestFont(.action)
            .foregroundColor(color)
            .lineLimit(2)
            .minimumScaleFactor(0.7)
    }
}

/// How each `StateTone` is colored. Green and amber always carry dark words,
/// never blue.
extension StateTone {
    /// The color of the words.
    var ink: Color {
        switch self {
        case .action: return Theme.blueInk
        case .good: return Theme.darkGreen
        case .waiting: return Theme.ink
        case .problem: return Theme.white
        }
    }

    /// What the words sit on.
    var fill: Color {
        switch self {
        case .action: return Theme.card
        case .good: return Theme.paleGreen
        case .waiting: return Theme.paleAmber
        case .problem: return Theme.ink
        }
    }
}

/// A video's state in words on a colored chip: never a bare icon.
///
/// The chip is as wide as its words and is never shrunk or cut off. Where
/// there is not the width for them on one line (a small phone with the
/// largest text), they take a second line.
struct StatusChip: View {
    let text: String
    let tone: StateTone

    var body: some View {
        Text(text)
            .tracking(0.5)
            .zestFont(.pill)
            .foregroundColor(tone == .action ? Theme.softInk : tone.ink)
            .multilineTextAlignment(.center)
            .lineLimit(2)
            .fixedSize(horizontal: false, vertical: true)
            .padding(.horizontal, 14)
            .padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: 18, style: .continuous).fill(tone.fill))
            .overlay(
                RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .strokeBorder(tone == .action ? Theme.hairline : Color.clear, lineWidth: 2)
            )
    }
}

/// A full-width card with a short message: amber for "look at this", white
/// for plain information, green for good news.
struct Banner: View {
    enum Tone {
        case attention
        case info
        case good
    }

    let text: String
    var tone: Tone = .info

    var body: some View {
        Card(tint: fill) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: symbol)
                    .font(.system(size: 26, weight: .bold))
                    .foregroundColor(symbolColor)
                    .accessibilityHidden(true)
                Text(text)
                    .zestFont(.bodyBold)
                    .foregroundColor(tone == .good ? Theme.darkGreen : Theme.ink)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
            }
            .padding(16)
        }
    }

    private var fill: Color {
        switch tone {
        case .attention: return Theme.paleAmber
        case .info: return Theme.card
        case .good: return Theme.paleGreen
        }
    }

    private var symbol: String {
        switch tone {
        case .attention: return "exclamationmark.triangle.fill"
        case .info: return "info.circle.fill"
        case .good: return "checkmark.circle.fill"
        }
    }

    private var symbolColor: Color {
        switch tone {
        case .attention: return Theme.ink
        case .info: return Theme.softInk
        case .good: return Theme.green
        }
    }
}

/// The customer's name and street on a white card, each under its small grey
/// label, as large as they go: this is how the tech knows he has the right house.
struct StopHeadline: View {
    enum Style {
        /// Name 52 pt, street 38 pt, roomy. The confirm screens.
        case full
        /// The same type, with less padding: screens with three buttons.
        case tight
        /// One label, name 38 pt, street 26 pt: where a warning needs the room.
        case small
    }

    let name: String
    let address: String
    var cityLine: String = ""
    var style: Style = .full
    /// The video's state, shown at the right of the CUSTOMER label.
    var chip: StatusChip?

    @Environment(\.dynamicTypeSize) private var typeSize

    /// With the phone's text at its two largest sizes, the tight card (the
    /// one on screens that also carry a message and up to three buttons) is
    /// drawn as the small one: the name is still larger than ordinary text
    /// is at the usual size, and the message under it has room to show.
    private var shown: Style {
        style == .tight && LargeType.isOn(typeSize) ? .small : style
    }

    var body: some View {
        let style = shown
        Card {
            VStack(alignment: .leading, spacing: style == .small ? 4 : 6) {
                if let chip {
                    // The label and the video's state share a line when both
                    // fit whole, each at its own full width. When they do
                    // not (a long state such as DONE BY TEAM on a narrower
                    // phone, or larger text), the state goes under the
                    // label. Neither is ever squeezed.
                    ViewThatFits(in: .horizontal) {
                        HStack(alignment: .center, spacing: 10) {
                            SmallLabel("Customer")
                                .fixedSize()
                            Spacer(minLength: 0)
                            chip
                                .fixedSize()
                        }
                        VStack(alignment: .leading, spacing: 8) {
                            SmallLabel("Customer")
                            chip
                        }
                    }
                } else {
                    SmallLabel("Customer")
                }
                // As many lines as it needs: a 45-letter business name
                // has to show whole, at this size, on the smallest phone.
                Text(name)
                    .zestFont(style == .small ? .heroStreet : .heroName)
                    .fontWeight(.semibold)
                    .foregroundColor(Theme.ink)
                    .customerText()
                if !address.isEmpty {
                    if style != .small {
                        Hairline()
                            .padding(.vertical, style == .tight ? 6 : 10)
                        SmallLabel("Address")
                    }
                    Text(address)
                        .zestFont(style == .small ? .rowStreet : .heroStreet)
                        .foregroundColor(Theme.ink)
                        .customerText()
                    if !cityLine.isEmpty {
                        Text(cityLine)
                            .zestFont(.body)
                            .foregroundColor(Theme.softInk)
                            .customerText()
                    }
                }
            }
            .padding(style == .full ? 20 : 14)
        }
        .accessibilityElement(children: .combine)
    }
}

/// How every line that comes from a customer's record is drawn: a name, a
/// street, a town.
///
/// **At its full size, on as many lines as it needs.** It is never held to a
/// number of lines, so it is never squeezed to fit one and never ends in
/// "…". The card or page it is on grows, and scrolls if it has to.
///
/// The one case in which it is drawn smaller: a single word too long for
/// the line (a 15-letter surname in the largest type on the smallest
/// phone). That word is shrunk, to no less than `Theme.longWordShrink` of
/// its size, rather than broken in two. With no limit on lines, nothing
/// else makes the text shrink.
struct CustomerText: ViewModifier {
    func body(content: Content) -> some View {
        content
            .lineLimit(nil)
            .minimumScaleFactor(Theme.longWordShrink)
            .multilineTextAlignment(.leading)
            .fixedSize(horizontal: false, vertical: true)
    }
}

extension View {
    /// A line from a customer's record: full size, as many lines as it needs.
    func customerText() -> some View {
        modifier(CustomerText())
    }
}

/// An underlined word that does something. At least 48 pt tall to tap.
struct TextLink: View {
    let title: String
    var color: Color = Theme.blueInk
    /// For UI tests.
    var identifier: String = ""
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
        .accessibilityIdentifier(identifier.isEmpty ? title : identifier)
    }
}

/// "Terms" and "Privacy": open the server's pages in Safari.
struct LegalLinks: View {
    @EnvironmentObject private var app: AppModel

    var body: some View {
        HStack(spacing: 28) {
            TextLink(title: "Terms", identifier: "link-terms") {
                app.openTerms()
            }
            .accessibilityHint("Opens in Safari")
            TextLink(title: "Privacy", identifier: "link-privacy") {
                app.openPrivacy()
            }
            .accessibilityLabel("Privacy Policy")
            .accessibilityHint("Opens in Safari")
        }
    }
}
