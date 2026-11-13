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

    // MARK: Is a line of text whole?

    /// How much clear background there is between the drawn text and each
    /// edge of its picture, in pixels.
    ///
    /// A line that is cut off by something, or that spills out of the room
    /// it was given, has ink running right up to an edge. A line that is
    /// whole has clear background all the way along its top, its bottom and
    /// its right-hand end. (Its first letter may sit against the left edge:
    /// that is where the line starts.)
    public struct Air: Equatable, Sendable {
        public let top: Int
        public let bottom: Int
        public let left: Int
        public let right: Int

        public init(top: Int, bottom: Int, left: Int, right: Int) {
            self.top = top
            self.bottom = bottom
            self.left = left
            self.right = right
        }

        /// Nothing drawn touches the top, the bottom or the far end.
        public var isWhole: Bool { top >= 1 && bottom >= 1 && right >= 1 }

        /// "air above 10, below 8, at the end 12 px"
        public var summary: String { "air above \(top), below \(bottom), at the end \(right) px" }
    }

    /// A pixel is ink when any of its three colours is further than this
    /// from the background's (of 255). Above the faintest edge of a letter's
    /// soft outline, well under any colour the app writes in.
    public static let inkTolerance = 24

    /// Finds the drawn part of a picture of one line of text and says how
    /// far it is from each edge.
    /// - Parameters:
    ///   - rgb: red, green and blue for each pixel, row after row from the top.
    ///   - width: pixels in a row.
    /// - Returns: `nil` when there is no picture or nothing is drawn in it.
    public static func air(rgb: [UInt8], width: Int) -> Air? {
        guard width > 0, rgb.count >= width * 3 else { return nil }
        let height = rgb.count / 3 / width
        guard height > 0 else { return nil }

        // The background: the most common colour round the rim of the
        // picture. What a line sits on is what surrounds it, even when the
        // line itself (a row of heavy capitals, a picture character) fills
        // more of the picture than the background does.
        var pixels = [Int](repeating: 0, count: 32_768)
        var index = 0
        func count(_ x: Int, _ y: Int) {
            let at = (y * width + x) * 3
            pixels[((Int(rgb[at]) >> 3) << 10) | ((Int(rgb[at + 1]) >> 3) << 5) | (Int(rgb[at + 2]) >> 3)] += 1
        }
        for x in 0..<width {
            count(x, 0)
            count(x, height - 1)
        }
        for y in 0..<height {
            count(0, y)
            count(width - 1, y)
        }
        var backgroundGroup = 0
        for group in 0..<pixels.count where pixels[group] > pixels[backgroundGroup] {
            backgroundGroup = group
        }
        // The middle of that group is near enough to its true colour here.
        let red = ((backgroundGroup >> 10) & 31) * 8 + 4
        let green = ((backgroundGroup >> 5) & 31) * 8 + 4
        let blue = (backgroundGroup & 31) * 8 + 4

        var top = height, bottom = -1, left = width, right = -1
        index = 0
        for y in 0..<height {
            for x in 0..<width {
                let apart = max(abs(Int(rgb[index]) - red), abs(Int(rgb[index + 1]) - green), abs(Int(rgb[index + 2]) - blue))
                index += 3
                if apart > inkTolerance {
                    if y < top { top = y }
                    if y > bottom { bottom = y }
                    if x < left { left = x }
                    if x > right { right = x }
                }
            }
        }
        guard bottom >= 0 else { return nil }
        return Air(top: top, bottom: height - 1 - bottom, left: left, right: width - 1 - right)
    }

    /// True when the words hold a picture character (an emoji). Such a
    /// character comes from another font, which claims more height than it
    /// draws in; Apple's audit takes that claim for clipped text.
    public static func hasPictureCharacter(_ words: String) -> Bool {
        words.unicodeScalars.contains { scalar in
            scalar.properties.isEmojiPresentation || (scalar.properties.isEmoji && scalar.value > 0x238C)
        }
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
