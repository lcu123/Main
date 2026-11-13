import XCTest
@testable import ZestCore

final class PhoneFormatTests: XCTestCase {
    func testDigitsFromAnythingTyped() {
        XCTAssertEqual(PhoneFormat.digits(from: "(916) 555-0100"), "9165550100")
        XCTAssertEqual(PhoneFormat.digits(from: "916.555.0100"), "9165550100")
        XCTAssertEqual(PhoneFormat.digits(from: "916 555 0100"), "9165550100")
        XCTAssertEqual(PhoneFormat.digits(from: ""), "")
        XCTAssertEqual(PhoneFormat.digits(from: "abc"), "")
    }

    func testCountryCodeIsDropped() {
        // What the phone's own autofill hands over.
        XCTAssertEqual(PhoneFormat.digits(from: "+1 (916) 555-0100"), "9165550100")
        XCTAssertEqual(PhoneFormat.digits(from: "19165550100"), "9165550100")
        XCTAssertEqual(PhoneFormat.digits(from: "1"), "")
    }

    func testNeverMoreThanTenDigits() {
        XCTAssertEqual(PhoneFormat.digits(from: "916555010099"), "9165550100")
    }

    func testDisplayBuildsUpAsDigitsArrive() {
        XCTAssertEqual(PhoneFormat.display(""), "")
        XCTAssertEqual(PhoneFormat.display("9"), "(9")
        XCTAssertEqual(PhoneFormat.display("916"), "(916")
        XCTAssertEqual(PhoneFormat.display("9165"), "(916) 5")
        XCTAssertEqual(PhoneFormat.display("916555"), "(916) 555")
        XCTAssertEqual(PhoneFormat.display("9165550"), "(916) 555-0")
        XCTAssertEqual(PhoneFormat.display("9165550100"), "(916) 555-0100")
    }

    func testTheDeleteKeyNeverGetsStuck() {
        // Typing one digit at a time and then deleting one character at a
        // time must walk all the way back to empty: every delete removes a digit.
        var text = ""
        for digit in "9165550100" {
            text = PhoneFormat.display(PhoneFormat.digits(from: text + String(digit)))
        }
        XCTAssertEqual(text, "(916) 555-0100")

        var steps = 0
        while !text.isEmpty {
            let before = PhoneFormat.digits(from: text).count
            text = PhoneFormat.display(PhoneFormat.digits(from: String(text.dropLast())))
            XCTAssertEqual(PhoneFormat.digits(from: text).count, before - 1, "stuck at \\(text)")
            steps += 1
            XCTAssertLessThanOrEqual(steps, 10)
        }
        XCTAssertEqual(steps, 10)
    }

    func testRoundTripIsStable() {
        for digits in ["", "9", "91", "916", "9165", "91655", "916555", "9165550", "91655501", "916555010", "9165550100"] {
            let shown = PhoneFormat.display(digits)
            XCTAssertEqual(PhoneFormat.digits(from: shown), digits)
            XCTAssertEqual(PhoneFormat.display(PhoneFormat.digits(from: shown)), shown, "formatting twice changes nothing")
        }
    }

    func testComplete() {
        XCTAssertTrue(PhoneFormat.isComplete("9165550100"))
        XCTAssertFalse(PhoneFormat.isComplete("916555010"))
        XCTAssertFalse(PhoneFormat.isComplete(""))
    }
}
