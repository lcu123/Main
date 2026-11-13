import SwiftUI

/// Zest's logo colors and the app's type sizes.
///
/// Built to be read by a 75-year-old in gloves in bright sun: nothing under
/// 22 pt, titles 34 pt and up, heavy weights, strong contrast. Black goes on
/// yellow and lime, white goes on the purples, and red means one thing only:
/// the camera is recording.
enum Theme {
    // Brand
    static let purple = Color(hex: 0x6E20B9)
    static let brightPurple = Color(hex: 0x932FFB)
    static let deepViolet = Color(hex: 0x5600C6)
    static let yellow = Color(hex: 0xFFF515)
    static let lime = Color(hex: 0xA7E84C)
    static let mint = Color(hex: 0x6FE7C8)
    static let black = Color.black
    static let white = Color.white

    // Roles
    static let background = Color.white
    static let ink = Color.black
    static let softInk = Color(hex: 0x3A3A3A)
    static let card = Color(hex: 0xF3EEFA)
    static let idleDot = Color(hex: 0x8A8A8A)
    /// Recording only. Never used as a brand color.
    static let recordRed = Color(hex: 0xE5121A)

    // Type. Text styles (not fixed sizes) so the phone's text-size setting
    // still works; RootView caps the range so layouts never break.
    /// 34 pt and up.
    static let title = Font.system(.largeTitle, design: .rounded).weight(.heavy)
    /// 28 pt: stop names, buttons.
    static let heading = Font.system(.title, design: .rounded).weight(.bold)
    /// 22 pt: the smallest text in the app.
    static let body = Font.system(.title2, design: .rounded).weight(.medium)
    static let bodyBold = Font.system(.title2, design: .rounded).weight(.bold)
    static let button = Font.system(.title, design: .rounded).weight(.heavy)

    static let corner: CGFloat = 20
    static let gutter: CGFloat = 20
    static let buttonHeight: CGFloat = 76
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
