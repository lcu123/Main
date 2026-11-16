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

    // MARK: The field as typed, read when SEND MY CODE is tapped

    func testPastedAndAutofilledNumbersAreTheSameTenDigits() {
        XCTAssertEqual(PhoneFormat.check("9165550100"), .ok("9165550100"))
        XCTAssertEqual(PhoneFormat.check("+1 (916) 555-0100"), .ok("9165550100"), "what the phone's autofill hands over")
        XCTAssertEqual(PhoneFormat.check("1-916-555-0100"), .ok("9165550100"))
        XCTAssertEqual(PhoneFormat.check("916.555.0100"), .ok("9165550100"))
        XCTAssertEqual(PhoneFormat.check("(916) 555-0100"), .ok("9165550100"))
        XCTAssertEqual(PhoneFormat.check("  916 555 0100  "), .ok("9165550100"))
        XCTAssertEqual(PhoneFormat.check("19165550100"), .ok("9165550100"), "eleven digits starting with 1")
        XCTAssertEqual(PhoneFormat.check("+19165550100"), .ok("9165550100"))
    }

    func testTooShortTooLongAndEmptyAreToldApart() {
        XCTAssertEqual(PhoneFormat.check(""), .empty)
        XCTAssertEqual(PhoneFormat.check("   "), .empty)
        XCTAssertEqual(PhoneFormat.check("()- "), .empty)
        XCTAssertEqual(PhoneFormat.check("916"), .tooShort)
        XCTAssertEqual(PhoneFormat.check("916555010"), .tooShort, "nine digits")
        XCTAssertEqual(PhoneFormat.check("+1 916 555 010"), .tooShort, "country code and nine digits")
        XCTAssertEqual(PhoneFormat.check("1916555010"), .tooShort, "ten digits, but the first is the country code")
        XCTAssertEqual(PhoneFormat.check("1"), .tooShort)
        XCTAssertEqual(PhoneFormat.check("+1"), .tooShort)
        XCTAssertEqual(PhoneFormat.check("91655501001"), .tooLong, "eleven digits not starting with 1")
        XCTAssertEqual(PhoneFormat.check("191655501009"), .tooLong, "twelve digits")
        XCTAssertEqual(PhoneFormat.check("916555010099"), .tooLong, "never silently cut to ten")
    }

    /// Whatever order and speed the keys arrive in, reading the field gives
    /// exactly the digits that are in it: nothing is rewritten on the way.
    func testEveryPrefixOfATypedNumberReadsBackWhole() {
        var typed = ""
        for digit in "9165550100" {
            typed.append(digit)
            XCTAssertEqual(PhoneFormat.allDigits(in: typed), typed)
            XCTAssertEqual(PhoneFormat.check(typed), typed.count == 10 ? .ok(typed) : .tooShort)
        }
        // The same number typed with the punctuation a person might add.
        var grouped = ""
        for character in "(916) 555-0100" {
            grouped.append(character)
            let digits = PhoneFormat.allDigits(in: grouped)
            XCTAssertEqual(PhoneFormat.check(grouped), digits.isEmpty ? .empty : (digits.count == 10 ? .ok(digits) : .tooShort))
        }
    }

    func testPreviewGroupsWhatWasTyped() {
        XCTAssertEqual(PhoneFormat.preview(""), "")
        XCTAssertEqual(PhoneFormat.preview("916"), "(916")
        XCTAssertEqual(PhoneFormat.preview("9165550100"), "(916) 555-0100")
        XCTAssertEqual(PhoneFormat.preview("+1 (916) 555-0100"), "(916) 555-0100")
        XCTAssertEqual(PhoneFormat.preview("19165550100"), "(916) 555-0100")
        XCTAssertEqual(PhoneFormat.preview("916555010099"), "(916) 555-0100 99", "extra digits stay in sight")
    }

    // MARK: The six-digit code

    func testCodeIsTheFirstSixDigitsHoweverItArrives() {
        XCTAssertEqual(CodeEntry.digits(from: "123456"), "123456")
        XCTAssertEqual(CodeEntry.digits(from: "123 456"), "123456")
        XCTAssertEqual(CodeEntry.digits(from: "123-456"), "123456")
        XCTAssertEqual(CodeEntry.digits(from: "Your Zest Video code is 123456"), "123456", "a pasted text message")
        XCTAssertEqual(CodeEntry.digits(from: "1234567"), "123456", "a seventh key changes nothing")
        XCTAssertEqual(CodeEntry.digits(from: "12"), "12")
        XCTAssertEqual(CodeEntry.digits(from: ""), "")
        XCTAssertTrue(CodeEntry.isComplete("123456"))
        XCTAssertFalse(CodeEntry.isComplete("12345"))
    }

    /// Typed one key at a time, filled in all at once, or reported twice by
    /// the field: the code goes to the server exactly once.
    func testAWholeCodeIsSubmittedExactlyOnce() {
        var submitted: [String] = []
        var last: String?
        func fieldChanged(_ text: String) {
            if let code = CodeEntry.shouldSubmit(text: text, lastSubmitted: last) {
                last = code
                submitted.append(code)
            }
        }
        // One key at a time, as fast as they come.
        var typed = ""
        for digit in "123456" {
            typed.append(digit)
            fieldChanged(typed)
        }
        XCTAssertEqual(submitted, ["123456"])
        // The field reports the same text again, then a seventh key.
        fieldChanged("123456")
        fieldChanged("1234567")
        XCTAssertEqual(submitted, ["123456"], "still once")

        // All six at once from the text message, then the same again.
        submitted = []
        last = nil
        fieldChanged("123456")
        fieldChanged("123456")
        XCTAssertEqual(submitted, ["123456"])

        // A wrong code, cleared, then the right one: each goes once.
        submitted = []
        last = nil
        fieldChanged("000000")
        last = nil              // the screen clears the field after "not right"
        fieldChanged("")
        fieldChanged("123456")
        XCTAssertEqual(submitted, ["000000", "123456"])
        // The same wrong code typed again after it was cleared is sent again.
        last = nil
        fieldChanged("")
        fieldChanged("000000")
        XCTAssertEqual(submitted, ["000000", "123456", "000000"])
    }

    func testComplete() {
        XCTAssertTrue(PhoneFormat.isComplete("9165550100"))
        XCTAssertFalse(PhoneFormat.isComplete("916555010"))
        XCTAssertFalse(PhoneFormat.isComplete(""))
    }
}
