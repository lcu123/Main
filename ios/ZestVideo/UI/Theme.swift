import SwiftUI
import UIKit

/// Zest's logo colors and every size in the app.
///
/// Built to be read by a 75-year-old in gloves in bright sun: very large
/// type, heavy weights, strong contrast, big buttons. Black goes on yellow
/// and lime, white goes on the purples, and red means one thing only: the
/// camera is recording.
///
/// **All sizes live here.** They are written for a 430 pt wide phone (the big
/// iPhones) and shrink gently on narrower ones through `scale`, so nothing
/// clips on a small phone and nothing looks lost on a big one. The phone's
/// own text-size setting still works on top (see `ZestFont`).
enum Theme {
    // MARK: Brand colors (the logo values)

    static let purple = Color(hex: 0x6E20B9)
    static let brightPurple = Color(hex: 0x932FFB)
    static let deepViolet = Color(hex: 0x5600C6)
    static let yellow = Color(hex: 0xFFF515)
    static let lime = Color(hex: 0xA7E84C)
    static let mint = Color(hex: 0x6FE7C8)
    static let black = Color.black
    static let white = Color.white

    // MARK: Roles

    static let background = Color.white
    static let ink = Color.black
    static let softInk = Color(hex: 0x3A3A3A)
    static let card = Color(hex: 0xF3EEFA)
    /// Recording only. Never used as a brand color.
    static let recordRed = Color(hex: 0xE5121A)

    // MARK: The one scale

    /// The short side of the screen, in points.
    @MainActor static let screenWidth: CGFloat = min(UIScreen.main.bounds.width, UIScreen.main.bounds.height)
    /// The long side of the screen, in points.
    @MainActor static let screenHeight: CGFloat = max(UIScreen.main.bounds.width, UIScreen.main.bounds.height)

    /// 1.0 on a 430 pt wide phone or wider, 0.87 at 375 pt, never under 0.78
    /// (a 320 pt screen, which is also what Apple uses to run the app on an iPad).
    @MainActor static let scale: CGFloat = min(1, max(0.78, screenWidth / 430))

    /// A very short screen (the 320 x 480 iPad compatibility size). Screens
    /// put their buttons inside the scrolling part so nothing is squeezed out.
    @MainActor static let compactHeight: Bool = screenHeight < 600

    // MARK: Type

    /// Every kind of text in the app, with its size on a 430 pt wide phone.
    enum TextRole {
        /// Screen titles. 40 pt.
        case title
        /// Ordinary text. 26 pt, and never under 24 on any phone.
        case body
        case bodyBold
        /// Button labels. 30 pt.
        case button
        /// The customer's name on the "is this the right house" screens. 52 pt.
        case heroName
        /// The street on those screens. 38 pt.
        case heroStreet
        /// A stop's name on the Today list. 32 pt.
        case rowName
        /// A stop's street on the Today list. 26 pt.
        case rowStreet
        /// Status pills and distances. 22 pt.
        case pill
        case distance
        /// Small purple labels over a group ("TAP YOUR STOP"). 24 pt.
        case section
        /// What is typed into the sign-in field. 34 pt.
        case field
        /// A digit in a code box. 52 pt.
        case codeDigit
        /// "TAP TO RECORD" and "RECORDING". 34 pt.
        case recordLabel
        /// What to say while recording. 40 pt.
        case prompt
        /// "SAVED ✓". 72 pt.
        case saved
        /// Terms, Privacy, Sign out. 24 pt.
        case link

        var base: CGFloat {
            switch self {
            case .title: return 40
            case .body, .bodyBold, .rowStreet: return 26
            case .button: return 30
            case .heroName: return 52
            case .heroStreet: return 38
            case .rowName: return 32
            case .pill, .distance: return 22
            case .section, .link: return 24
            case .field, .recordLabel: return 34
            case .codeDigit: return 52
            case .prompt: return 40
            case .saved: return 72
            }
        }

        /// The smallest this text may get on a narrow phone.
        var floor: CGFloat {
            switch self {
            case .body, .bodyBold, .rowStreet: return 24
            case .pill, .distance: return 19
            case .section, .link: return 22
            case .button: return 24
            case .title, .rowName: return 28
            case .field, .recordLabel: return 26
            case .heroStreet, .prompt: return 30
            case .heroName, .codeDigit: return 38
            case .saved: return 52
            }
        }

        var weight: Font.Weight {
            switch self {
            case .title, .button, .heroName, .codeDigit, .recordLabel, .prompt, .saved: return .heavy
            case .bodyBold, .heroStreet, .rowName, .rowStreet, .pill, .section, .field: return .bold
            case .body, .distance, .link: return .medium
            }
        }
    }

    /// Points for a kind of text on this phone, before the text-size setting.
    @MainActor
    static func size(_ role: TextRole) -> CGFloat {
        max(role.floor, (role.base * scale).rounded())
    }

    // MARK: Controls and layout

    static let corner: CGFloat = 20
    /// Space at the left and right of every screen.
    @MainActor static let gutter: CGFloat = screenWidth < 360 ? 16 : 20
    /// Buttons: 88 pt tall on a wide phone, never under 76.
    @MainActor static let buttonHeight: CGFloat = max(76, (88 * scale).rounded())
    /// The sign-in field.
    @MainActor static let fieldHeight: CGFloat = max(84, (100 * scale).rounded())
    /// The ring's stroke.
    @MainActor static let ringStroke: CGFloat = max(16, (22 * scale).rounded())
    /// The ring and the record button are this share of the screen's width.
    static let ringShare: CGFloat = 0.64
    /// The seconds-left number is this share of the ring's diameter.
    static let ringNumberShare: CGFloat = 0.43
    /// The played-back clip on the review screen.
    @MainActor static let clipHeight: CGFloat = compactHeight ? 200 : (340 * scale).rounded()
}

/// Applies one of the app's text sizes in the rounded system font, and lets
/// the phone's own text-size setting scale it (within the range RootView allows).
struct ZestFont: ViewModifier {
    @ScaledMetric private var size: CGFloat
    private let weight: Font.Weight

    @MainActor
    init(_ role: Theme.TextRole) {
        _size = ScaledMetric(wrappedValue: Theme.size(role), relativeTo: .body)
        weight = role.weight
    }

    func body(content: Content) -> some View {
        content.font(.system(size: size, weight: weight, design: .rounded))
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
