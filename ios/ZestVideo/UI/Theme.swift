import SwiftUI
import UIKit
import ZestCore

/// The app's colors and every size in it.
///
/// **The look** is the one the techs already work in all day: a light grey
/// page, white cards with soft shadows, one strong blue for anything that
/// can be tapped, small grey capital labels, and the plain system typeface.
/// It is a familiar style, not a copy: every symbol is a system symbol and
/// the only artwork is Zest's own icon.
///
/// **The sizes** are still Zest's own rule: built to be read by a
/// 75-year-old in gloves in bright sun. Very large type, big buttons, strong
/// contrast. Red means one thing only: the camera is recording.
///
/// All sizes live here. They are written for a 430 pt wide phone (the big
/// iPhones) and shrink gently on narrower ones through `scale`, so nothing
/// clips on a small phone and nothing looks lost on a big one. The phone's
/// own text-size setting still works on top (see `ZestFont`).
enum Theme {
    // MARK: Colors

    /// Anything that can be tapped: the fill of the main button, borders,
    /// the round badges. White words on it are large and bold (4.1 to 1).
    static let blue = Color(hex: 0x217DE7)
    static let bluePressed = Color(hex: 0x1A64B9)
    /// Blue WORDS on a light ground: action words on a card ("RECORD
    /// VIDEO"), the words of a white button, links, the date in the top bar.
    /// A shade deeper than `blue`, so they read in bright sun: 5.5 to 1 on
    /// white, 5.0 on the page, and still 4.5 on the pale tints. (`blue`
    /// itself is 4.1 on white and 3.7 on the page: fine as a fill, too
    /// faint for words.)
    static let blueInk = Color(hex: 0x1A68C4)
    /// The back arrow and its round button. The arrow is a deep cyan: the
    /// lighter #00A7E1 it started as was only 2.3 to 1 against the pale
    /// circle, too faint in bright sun. This one is 4.7 to 1.
    static let cyan = Color(hex: 0x006E94)
    static let paleCyan = Color(hex: 0xCCEFF5)
    /// The selected tab.
    static let navy = Color(hex: 0x1A1353)
    /// The YES / NO switch. Each track is dark enough that its white word
    /// and its white knob both stand out in bright sun: white on the deep
    /// blue is 5.9 to 1, white on the grey is 5.7 to 1. (The first version
    /// had a pale grey track for NO; its white knob was 1.5 to 1 on it, and
    /// Apple's accessibility audit said so.)
    static let switchOn = Color(hex: 0x1A64B9)
    static let switchOff = Color(hex: 0x666666)

    /// The page behind the cards.
    static let page = Color(hex: 0xF5F5F5)
    /// Cards and bars.
    static let card = Color.white
    static let hairline = Color(hex: 0xD0D0D0)

    /// Titles.
    static let titleInk = Color(hex: 0x48484A)
    /// Main text.
    static let ink = Color(hex: 0x333333)
    /// Small labels and second-rank text. Never used under the minimum size.
    /// 7 to 1 on white and 6.4 on the page (it was #666666, 5.7 and 5.3:
    /// enough by the book, faint for old eyes in bright sun).
    static let softInk = Color(hex: 0x595959)

    /// Sent and saved. White on it is about 4.7 to 1.
    static let green = Color(hex: 0x1E8449)
    static let paleGreen = Color(hex: 0xDDF3E4)
    static let darkGreen = Color(hex: 0x14532D)
    /// Waiting and "look at this". Always with dark text, never blue.
    static let amber = Color(hex: 0xF5A623)
    static let paleAmber = Color(hex: 0xFFF1CC)

    /// Recording only: the button, the border, the pill.
    static let recordRed = Color(hex: 0xE5121A)
    /// The countdown ring only. It has to read over any picture.
    static let ringYellow = Color(hex: 0xFFF515)

    static let white = Color.white
    static let black = Color.black

    // MARK: The one scale

    /// The short side of the screen, in points.
    @MainActor static let screenWidth: CGFloat = min(UIScreen.main.bounds.width, UIScreen.main.bounds.height)
    /// The long side of the screen, in points.
    @MainActor static let screenHeight: CGFloat = max(UIScreen.main.bounds.width, UIScreen.main.bounds.height)

    /// 1.0 on a 430 pt wide phone or wider, 0.87 at 375 pt, never under 0.78
    /// (a 320 pt screen, which is also what Apple uses to run the app on an iPad).
    /// (The numbers are in `GloveRule`, where the tests read them too.)
    @MainActor static let scale: CGFloat = CGFloat(GloveRule.scale(screenWidth: Double(screenWidth)))

    /// A very short screen (the 320 x 480 iPad compatibility size). Screens
    /// put their buttons inside the scrolling part so nothing is squeezed out.
    @MainActor static let compactHeight: Bool = screenHeight < 600

    // MARK: Type

    /// Every kind of text in the app, with its size on a 430 pt wide phone.
    enum TextRole {
        /// The question at the top of a screen. 40 pt.
        case title
        /// The screen's name in the top bar. 26 pt.
        case barTitle
        /// Ordinary text. 26 pt, and never under 24 on any phone.
        case body
        case bodyBold
        /// Button labels. 30 pt, in capitals.
        case button
        /// The customer's name on the "is this the right house" screens. 52 pt.
        case heroName
        /// The street on those screens. 38 pt.
        case heroStreet
        /// A name on a card in a list. 32 pt.
        case rowName
        /// A street on a card in a list. 26 pt.
        case rowStreet
        /// Small grey capital labels ("CUSTOMER", "50 FT AWAY"). 22 pt.
        case label
        /// Blue capital action words on a card ("RECORD VIDEO"). 26 pt.
        case action
        /// Status chips. 22 pt.
        case pill
        /// A number under a small label ("3/9"). 34 pt.
        case stat
        /// The words under a tab's symbol. 22 pt, never under 20.
        case tab
        /// What is typed into the sign-in field. 34 pt.
        case field
        /// A digit in a code box. 52 pt.
        case codeDigit
        /// "TAP TO RECORD" and "RECORDING", over the camera. 34 pt.
        case recordLabel
        /// What to say while recording, over the camera. 40 pt.
        case prompt
        /// "Saved". 64 pt.
        case saved
        /// Terms, Privacy, Sign out. 24 pt.
        case link

        var base: CGFloat {
            switch self {
            case .title, .prompt: return 40
            case .barTitle, .body, .bodyBold, .rowStreet, .action: return 26
            case .button: return 30
            case .heroName, .codeDigit: return 52
            case .heroStreet: return 38
            case .rowName: return 32
            case .label, .pill, .tab: return 22
            case .stat, .field, .recordLabel: return 34
            case .saved: return 64
            case .link: return 24
            }
        }

        /// The smallest this text may get on a narrow phone.
        var floor: CGFloat {
            switch self {
            case .body, .bodyBold, .rowStreet, .button: return 24
            case .barTitle, .action, .link: return 22
            case .label, .pill: return 19
            case .tab: return 20
            case .title, .rowName, .stat: return 28
            case .field, .recordLabel: return 26
            case .heroStreet, .prompt: return 30
            case .heroName, .codeDigit: return 38
            case .saved: return 48
            }
        }

        var weight: Font.Weight {
            switch self {
            case .recordLabel, .prompt: return .heavy
            case .title, .barTitle, .button, .action, .pill, .stat, .codeDigit, .saved: return .bold
            case .bodyBold, .heroName, .rowName, .label, .tab, .field: return .semibold
            case .heroStreet, .link: return .medium
            case .body, .rowStreet: return .regular
            }
        }

        /// The plain system face everywhere, except the two kinds of words
        /// that sit over the camera picture, which keep their rounded look.
        var design: Font.Design {
            switch self {
            case .recordLabel, .prompt: return .rounded
            default: return .default
            }
        }
    }

    /// Points for a kind of text on this phone, before the text-size setting.
    @MainActor
    static func size(_ role: TextRole) -> CGFloat {
        max(role.floor, (role.base * scale).rounded())
    }

    /// How far one word that is too long for its line may be drawn smaller
    /// rather than broken in two (see `customerText()`). Three quarters:
    /// "Christopherson," in the largest type on the smallest iPhone needs
    /// about 0.87. A word that would need less than this is broken instead.
    static let longWordShrink: CGFloat = 0.75

    // MARK: Controls and layout

    /// Cards and buttons.
    static let corner: CGFloat = 20
    /// Space at the left and right of every screen.
    @MainActor static let gutter: CGFloat = CGFloat(GloveRule.gutter(screenWidth: Double(screenWidth)))
    /// Buttons: 88 pt tall on a wide phone, never under 76.
    @MainActor static let buttonHeight: CGFloat = CGFloat(GloveRule.buttonHeight(screenWidth: Double(screenWidth)))
    /// The action row at the foot of a card.
    @MainActor static let actionRowHeight: CGFloat = max(64, (72 * scale).rounded())
    /// The top bar, and the back arrow's tap target inside it.
    static let barHeight: CGFloat = 68
    static let backTarget: CGFloat = 56
    /// The tab bar on the two home screens.
    static let tabBarHeight: CGFloat = 76
    /// The round badge on the left of a stop's card.
    @MainActor static let badgeSize: CGFloat = max(64, (84 * scale).rounded())
    /// The smaller badge with the tech's initial, in the header of My Route.
    @MainActor static let headerBadgeSize: CGFloat = max(56, (64 * scale).rounded())
    /// The sign-in field.
    @MainActor static let fieldHeight: CGFloat = max(84, (100 * scale).rounded())
    /// The ring's stroke.
    @MainActor static let ringStroke: CGFloat = max(16, (22 * scale).rounded())
    /// The record button and the ring are never smaller than this across.
    static let minRoundControl: CGFloat = CGFloat(GloveRule.minRoundControl)
    /// The ring and the record button are this share of the screen's width.
    static let ringShare: CGFloat = 0.64
    /// The seconds-left number is this share of the ring's diameter.
    static let ringNumberShare: CGFloat = 0.43
    /// The played-back clip on the review screen: as tall as this when there
    /// is room, and never shorter than `clipMinHeight`. It gives way so that
    /// everything else on that screen shows without scrolling.
    @MainActor static let clipHeight: CGFloat = compactHeight ? 200 : (340 * scale).rounded()
    @MainActor static let clipMinHeight: CGFloat = compactHeight ? 150 : 100
}

/// Applies one of the app's text sizes, and lets the phone's own text-size
/// setting scale it (within the range RootView allows).
struct ZestFont: ViewModifier {
    @ScaledMetric private var size: CGFloat
    private let weight: Font.Weight
    private let design: Font.Design

    @MainActor
    init(_ role: Theme.TextRole) {
        _size = ScaledMetric(wrappedValue: Theme.size(role), relativeTo: .body)
        weight = role.weight
        design = role.design
    }

    func body(content: Content) -> some View {
        content.font(.system(size: size, weight: weight, design: design))
    }
}

extension View {
    @MainActor
    func zestFont(_ role: Theme.TextRole) -> some View {
        modifier(ZestFont(role))
    }
}

extension Color {
    init(hex: UInt32) {
        self.init(
            .sRGB,
            red: Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue: Double(hex & 0xFF) / 255,
            opacity: 1
        )
    }
}
