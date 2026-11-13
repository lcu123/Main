import Foundation

/// The contrast of words on a screen, measured from the pixels themselves.
///
/// The colours in `Theme` are checked by arithmetic (`AppSourceRulesTests`),
/// and Apple's accessibility audit looks at every screen. When the two
/// disagree about a line of text, this settles it: a picture of just that
/// line is read, pixel by pixel.
///
/// - **What the words sit on** is the most common colour in the picture.
/// - **The words** are the colour that stands out most from it, among the
///   colours that fill a real share of the picture. The soft edge of each
///   letter is a blend of the two, so it always stands out less than the
///   letter itself and never wins.
///
/// This reads one line of text on one background. A whole card, with a
/// badge, three greys and a coloured bar, has no single answer and is not
/// measured this way.
///
/// `Scripts/measure_contrast.py` does the same sum on the same pixels, so the
/// numbers in a test report can be checked without a Mac.
public enum PixelContrast {
    public struct Reading: Equatable, Sendable {
        /// What the words sit on, 0xRRGGBB.
        public let background: UInt32
        /// The words, 0xRRGGBB.
        public let words: UInt32
        /// From 1 (the same colour) to 21 (black on white).
        public let ratio: Double
        /// The share of the picture that is the colour of the words, 0 to 1.
        public let wordShare: Double

        public init(background: UInt32, words: UInt32, ratio: Double, wordShare: Double) {
            self.background = background
            self.words = words
            self.ratio = ratio
            self.wordShare = wordShare
        }

        /// Enough for ordinary text in bright sun.
        public var isEnough: Bool { ratio >= GloveRule.minContrast }

        /// "7.8:1, words #1F6B3A on #E1F3E6"
        public var summary: String {
            "\(PixelContrast.oneDecimal(ratio)):1, words \(PixelContrast.hexText(words)) on \(PixelContrast.hexText(background))"
        }
    }

    /// A colour has to fill at least this share of the picture to count as
    /// the words (and at least `minPixels` of them).
    public static let minShare: Double = 0.004
    public static let minPixels = 12

    /// Reads a picture of one line of text.
    /// - Parameter rgb: red, green and blue for each pixel, one after another.
    /// - Returns: `nil` when there is nothing to read: no pixels, or one flat
    ///   colour with no words on it.
    public static func read(rgb: [UInt8]) -> Reading? {
        let count = rgb.count / 3
        guard count > 0 else { return nil }

        // Colours that differ by a shade or two are one colour here: each
        // channel is cut to 32 steps, and the true colour of a group is the
        // average of the pixels in it.
        var pixels = [Int](repeating: 0, count: 32_768)
        var red = [Int](repeating: 0, count: 32_768)
        var green = [Int](repeating: 0, count: 32_768)
        var blue = [Int](repeating: 0, count: 32_768)
        var index = 0
        for _ in 0..<count {
            let r = Int(rgb[index]), g = Int(rgb[index + 1]), b = Int(rgb[index + 2])
            index += 3
            let group = ((r >> 3) << 10) | ((g >> 3) << 5) | (b >> 3)
            pixels[group] += 1
            red[group] += r
            green[group] += g
            blue[group] += b
        }
        func colour(_ group: Int) -> UInt32 {
            let n = pixels[group]
            // Rounded to the nearest whole value.
            let r = UInt32((red[group] * 2 + n) / (2 * n))
            let g = UInt32((green[group] * 2 + n) / (2 * n))
            let b = UInt32((blue[group] * 2 + n) / (2 * n))
            return (r << 16) | (g << 8) | b
        }

        var backgroundGroup = 0
        for group in 0..<pixels.count where pixels[group] > pixels[backgroundGroup] {
            backgroundGroup = group
        }
        let background = colour(backgroundGroup)

        let enough = max(minPixels, Int((Double(count) * minShare).rounded(.up)))
        var best: (group: Int, ratio: Double)?
        for group in 0..<pixels.count where group != backgroundGroup && pixels[group] >= enough {
            let ratio = GloveRule.contrast(colour(group), background)
            if best == nil || ratio > best!.ratio {
                best = (group, ratio)
            }
        }
        guard let words = best else { return nil }
        return Reading(
            background: background,
            words: colour(words.group),
            ratio: words.ratio,
            wordShare: Double(pixels[words.group]) / Double(count)
        )
    }

    /// "#1A68C4"
    public static func hexText(_ colour: UInt32) -> String {
        let digits = String(colour & 0xFFFFFF, radix: 16, uppercase: true)
        return "#" + String(repeating: "0", count: max(0, 6 - digits.count)) + digits
    }

    /// One decimal place, rounded down, so a 4.49 is never shown as 4.5.
    public static func oneDecimal(_ value: Double) -> String {
        let tenths = Int((value * 10).rounded(.down))
        return "\(tenths / 10).\(tenths % 10)"
    }
}
