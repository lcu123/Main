import XCTest
@testable import ZestCore

/// "Built for a 75-year-old in gloves in bright sun": the sizes the app's
/// layout is made from, on every width of screen it can run on.
final class GloveRuleTests: XCTestCase {
    /// Every iPhone width there is, the 320 pt iPad compatibility size, and
    /// odd ones in between.
    private let widths: [Double] = [320, 344, 360, 375, 390, 393, 402, 414, 428, 430, 440, 744, 1024]

    func testMainButtonsAreNeverUnder76PointsTall() {
        XCTAssertEqual(GloveRule.minButtonHeight, 76)
        for width in widths {
            let height = GloveRule.buttonHeight(screenWidth: width)
            XCTAssertGreaterThanOrEqual(height, 76, "at \(width) pt wide")
            XCTAssertLessThanOrEqual(height, 88, "at \(width) pt wide")
        }
        XCTAssertEqual(GloveRule.buttonHeight(screenWidth: 430), 88, "full size on the big phones")
        XCTAssertEqual(GloveRule.buttonHeight(screenWidth: 375), 77)
        XCTAssertEqual(GloveRule.buttonHeight(screenWidth: 320), 76, "the floor on the smallest screen")
    }

    func testMainActionsSpanTheScreenFromGutterToGutter() {
        for width in widths {
            let gutter = GloveRule.gutter(screenWidth: width)
            XCTAssertTrue(gutter == 14 || gutter == 18, "at \(width) pt wide")
            XCTAssertEqual(GloveRule.actionWidth(screenWidth: width), width - 2 * gutter)
            XCTAssertGreaterThanOrEqual(GloveRule.actionWidth(screenWidth: width) / width, 0.9, "at least nine tenths of the screen at \(width) pt")
        }
    }

    func testSizesShrinkGentlyAndNeverBelowTheFloor() {
        XCTAssertEqual(GloveRule.scale(screenWidth: 430), 1)
        XCTAssertEqual(GloveRule.scale(screenWidth: 1024), 1, "never bigger than written")
        XCTAssertEqual(GloveRule.scale(screenWidth: 375), 375.0 / 430.0, accuracy: 0.0001)
        XCTAssertEqual(GloveRule.scale(screenWidth: 320), 0.78)
        XCTAssertEqual(GloveRule.scale(screenWidth: 100), 0.78)
        var last = 0.0
        for width in stride(from: 300.0, through: 460.0, by: 1.0) {
            let scale = GloveRule.scale(screenWidth: width)
            XCTAssertGreaterThanOrEqual(scale, last, "a wider screen never gets smaller type")
            last = scale
        }
    }

    func testTheOtherFloors() {
        XCTAssertEqual(GloveRule.minRoundControl, 120, "the record button and the ring")
        XCTAssertEqual(GloveRule.minTapTarget, 44)
        XCTAssertEqual(GloveRule.minBodyPoints, 24)
        XCTAssertGreaterThan(GloveRule.minButtonHeight, GloveRule.minTapTarget)
    }
}
