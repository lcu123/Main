import Foundation

/// The sizes behind "built for a 75-year-old in gloves in bright sun".
///
/// The brief: main actions span the full width of the screen; no small
/// icons, no menus, no swipes, no typing after setup. The numbers live here
/// so the app's layout (`Theme`) and the tests that measure the real screens
/// read the same ones.
public enum GloveRule {
    /// Sizes are written for a phone this wide (the big iPhones) and shrink
    /// gently on narrower ones.
    public static let designWidth: Double = 430
    /// Never under this share of the written size (a 320 pt wide screen).
    public static let minScale: Double = 0.78

    /// 1.0 on a 430 pt wide phone or wider, 0.87 at 375 pt, never under 0.78.
    public static func scale(screenWidth: Double) -> Double {
        min(1, max(minScale, screenWidth / designWidth))
    }

    /// A main button on a wide phone.
    public static let buttonHeight: Double = 88
    /// A main button on any phone: never shorter than this.
    public static let minButtonHeight: Double = 76

    /// How tall a main button is on a phone this wide.
    public static func buttonHeight(screenWidth: Double) -> Double {
        max(minButtonHeight, (buttonHeight * scale(screenWidth: screenWidth)).rounded())
    }

    /// Space at the left and right of every screen.
    public static func gutter(screenWidth: Double) -> Double {
        screenWidth < 360 ? 14 : 18
    }

    /// A main action spans the screen, from one gutter to the other.
    public static func actionWidth(screenWidth: Double) -> Double {
        screenWidth - 2 * gutter(screenWidth: screenWidth)
    }

    /// The record button and the ring are never smaller than this across.
    public static let minRoundControl: Double = 120

    /// Anything that can be tapped at all (Apple's own minimum).
    public static let minTapTarget: Double = 44

    /// Ordinary text is never under this many points.
    public static let minBodyPoints: Double = 24

    // MARK: Bright sun

    /// Words in ordinary sizes need this much contrast with what they sit on.
    public static let minContrast: Double = 4.5
    /// Large bold words (the app's buttons and action words, 26 pt and up)
    /// need this much.
    public static let minLargeTextContrast: Double = 3

    /// How bright a colour is to the eye, 0 (black) to 1 (white). `hex` is
    /// 0xRRGGBB. (The WCAG relative luminance.)
    public static func luminance(hex: UInt32) -> Double {
        func channel(_ shift: UInt32) -> Double {
            let value = Double((hex >> shift) & 0xFF) / 255
            return value <= 0.03928 ? value / 12.92 : pow((value + 0.055) / 1.055, 2.4)
        }
        return 0.2126 * channel(16) + 0.7152 * channel(8) + 0.0722 * channel(0)
    }

    /// The contrast between two colours, from 1 (the same) to 21 (black on
    /// white). The order does not matter.
    public static func contrast(_ first: UInt32, _ second: UInt32) -> Double {
        let a = luminance(hex: first), b = luminance(hex: second)
        return (max(a, b) + 0.05) / (min(a, b) + 0.05)
    }
}
