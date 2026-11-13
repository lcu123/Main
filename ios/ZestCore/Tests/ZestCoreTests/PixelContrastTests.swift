import XCTest
@testable import ZestCore

/// Contrast read from pixels: the measure that settles it when Apple's audit
/// and the theme's arithmetic disagree about a line of text.
final class PixelContrastTests: XCTestCase {
    /// A made-up picture of a line of text: `words` share of solid letters,
    /// `edge` share of soft edges (blends of the two colours), the rest
    /// background.
    private func picture(words: UInt32, on background: UInt32, wordShare: Double = 0.18, edgeShare: Double = 0.08, pixels: Int = 6_000) -> [UInt8] {
        func parts(_ colour: UInt32) -> [Double] {
            [Double((colour >> 16) & 0xFF), Double((colour >> 8) & 0xFF), Double(colour & 0xFF)]
        }
        let ink = parts(words), paper = parts(background)
        var rgb: [UInt8] = []
        rgb.reserveCapacity(pixels * 3)
        let solid = Int(Double(pixels) * wordShare)
        let soft = Int(Double(pixels) * edgeShare)
        for index in 0..<pixels {
            var mix = 0.0                       // 0 = paper, 1 = ink
            if index < solid {
                mix = 1
            } else if index < solid + soft {
                // Every blend between the two, evenly.
                mix = Double(index - solid + 1) / Double(soft + 1)
            }
            for channel in 0..<3 {
                rgb.append(UInt8((paper[channel] + (ink[channel] - paper[channel]) * mix).rounded()))
            }
        }
        return rgb
    }

    func testDarkWordsOnALightPageReadAsTheThemeSaysTheyShould() throws {
        // Ordinary ink on the page grey.
        let reading = try XCTUnwrap(PixelContrast.read(rgb: picture(words: 0x333333, on: 0xF5F5F5)))
        XCTAssertEqual(reading.background, 0xF5F5F5)
        XCTAssertEqual(reading.words, 0x333333)
        XCTAssertEqual(reading.ratio, GloveRule.contrast(0x333333, 0xF5F5F5), accuracy: 0.01)
        XCTAssertTrue(reading.isEnough)
        XCTAssertEqual(reading.wordShare, 0.18, accuracy: 0.01)
    }

    func testWhiteWordsOnADarkButtonAreReadTheSameWay() throws {
        // The words are the lighter colour here: what counts is how far
        // they stand out, not which is darker.
        let reading = try XCTUnwrap(PixelContrast.read(rgb: picture(words: 0xFFFFFF, on: 0x1A64B9)))
        XCTAssertEqual(reading.background, 0x1A64B9)
        XCTAssertEqual(reading.words, 0xFFFFFF)
        XCTAssertEqual(reading.ratio, GloveRule.contrast(0xFFFFFF, 0x1A64B9), accuracy: 0.01)
    }

    func testTheSoftEdgesOfTheLettersNeverCountAsTheWords() throws {
        // A third of the picture is blends of the two colours. Every blend
        // stands out less than the letters do, so none of them wins.
        let reading = try XCTUnwrap(PixelContrast.read(rgb: picture(words: 0x1A68C4, on: 0xFFFFFF, wordShare: 0.10, edgeShare: 0.33)))
        XCTAssertEqual(reading.words, 0x1A68C4)
        XCTAssertEqual(reading.ratio, GloveRule.contrast(0x1A68C4, 0xFFFFFF), accuracy: 0.01)
    }

    func testFaintWordsAreCaught() throws {
        // The grey this app used to use for small labels on the pale cyan.
        let reading = try XCTUnwrap(PixelContrast.read(rgb: picture(words: 0x8A8A8A, on: 0xE0F7FA)))
        XCTAssertLessThan(reading.ratio, 4.5)
        XCTAssertFalse(reading.isEnough)
    }

    func testAFewStrayDarkPixelsAreNotTheWords() throws {
        // Ten black pixels in six thousand (a corner of a shadow, a speck):
        // under the share that counts. The words are still the grey.
        var rgb = picture(words: 0x777777, on: 0xFFFFFF)
        for pixel in 0..<10 {
            let at = (rgb.count / 3 - 1 - pixel) * 3
            rgb[at] = 0
            rgb[at + 1] = 0
            rgb[at + 2] = 0
        }
        let reading = try XCTUnwrap(PixelContrast.read(rgb: rgb))
        XCTAssertEqual(reading.words, 0x777777)
        XCTAssertLessThan(reading.ratio, 4.5, "A stray dark speck must not make faint words look strong.")
    }

    func testAPictureWithNoWordsHasNoReading() {
        XCTAssertNil(PixelContrast.read(rgb: []), "No pixels.")
        XCTAssertNil(PixelContrast.read(rgb: [UInt8](repeating: 0xF5, count: 3_000)), "One flat colour.")
    }

    func testShadesOneStepApartAreOneColour() throws {
        // A background that wobbles by a shade (a gradient too slight to see)
        // is still one background, and its true colour is the average.
        var rgb: [UInt8] = []
        for index in 0..<4_000 {
            let shade: UInt8 = index % 2 == 0 ? 0xF0 : 0xF2
            rgb += [shade, shade, shade]
        }
        for _ in 0..<800 { rgb += [0x33, 0x33, 0x33] }
        let reading = try XCTUnwrap(PixelContrast.read(rgb: rgb))
        XCTAssertEqual(reading.background, 0xF1F1F1)
        XCTAssertEqual(reading.words, 0x333333)
    }

    func testTheNumbersAreWrittenPlainly() {
        XCTAssertEqual(PixelContrast.hexText(0x1A68C4), "#1A68C4")
        XCTAssertEqual(PixelContrast.hexText(0x000A0B), "#000A0B")
        XCTAssertEqual(PixelContrast.oneDecimal(4.49), "4.4", "Never rounded up over the line.")
        XCTAssertEqual(PixelContrast.oneDecimal(4.5), "4.5")
        XCTAssertEqual(PixelContrast.oneDecimal(11.593), "11.5")
        let reading = PixelContrast.Reading(background: 0xF5F5F5, words: 0x333333, ratio: 11.59, wordShare: 0.2)
        XCTAssertEqual(reading.summary, "11.5:1, words #333333 on #F5F5F5")
    }

    func testEveryWordColourInTheThemeReadsBackFromPixelsAsItsArithmetic() throws {
        // The pairs the app really draws (see `AppSourceRulesTests`): reading
        // a picture gives the same answer as the sum on the two colours.
        let pairs: [(UInt32, UInt32)] = [
            (0x333333, 0xFFFFFF), (0x333333, 0xF5F5F5), (0x595959, 0xFFFFFF), (0x595959, 0xF5F5F5),
            (0x1A68C4, 0xFFFFFF), (0x1A68C4, 0xF5F5F5), (0xFFFFFF, 0x217DE7), (0xFFFFFF, 0x333333),
        ]
        for (words, background) in pairs {
            let reading = try XCTUnwrap(PixelContrast.read(rgb: picture(words: words, on: background)))
            XCTAssertEqual(reading.ratio, GloveRule.contrast(words, background), accuracy: 0.02, "\(PixelContrast.hexText(words)) on \(PixelContrast.hexText(background))")
        }
    }
}
