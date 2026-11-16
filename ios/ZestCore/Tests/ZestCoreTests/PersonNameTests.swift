import XCTest
@testable import ZestCore

final class PersonNameTests: XCTestCase {
    func testFirstLastBecomesLastCommaFirst() {
        XCTAssertEqual(PersonName.listed(name: "Jane Smith", lastName: "Smith"), "Smith, Jane")
        XCTAssertEqual(PersonName.listed(name: "Robert Johnson", lastName: "Johnson"), "Johnson, Robert")
    }

    func testMiddleNamesAndCouplesStayWithTheFirstName() {
        XCTAssertEqual(PersonName.listed(name: "Mary Ann Quillfeather", lastName: "Quillfeather"), "Quillfeather, Mary Ann")
        XCTAssertEqual(PersonName.listed(name: "Jane and John Smith", lastName: "Smith"), "Smith, Jane and John")
        XCTAssertEqual(PersonName.listed(name: "J. R. Ostrander", lastName: "Ostrander"), "Ostrander, J. R.")
    }

    func testALastNameOfTwoWords() {
        XCTAssertEqual(PersonName.listed(name: "Riley Van Dyke", lastName: "Van Dyke"), "Van Dyke, Riley")
        XCTAssertEqual(PersonName.listed(name: "Casey De La Cruz", lastName: "De La Cruz"), "De La Cruz, Casey")
    }

    func testABusinessIsShownAsItIs() {
        XCTAssertEqual(PersonName.listed(name: "Pinecone Bakery LLC", lastName: ""), "Pinecone Bakery LLC")
        XCTAssertEqual(PersonName.listed(name: "Green Valley HOA", lastName: "   "), "Green Valley HOA")
    }

    func testANameThatCannotBeTurnedRoundSafelyIsLeftAlone() {
        // Does not end with the last name on file.
        XCTAssertEqual(PersonName.listed(name: "Jane Smith Jr.", lastName: "Smith"), "Jane Smith Jr.")
        XCTAssertEqual(PersonName.listed(name: "Jane Smythe", lastName: "Smith"), "Jane Smythe")
        // Only the last name.
        XCTAssertEqual(PersonName.listed(name: "Smith", lastName: "Smith"), "Smith")
        // Already last-name-first.
        XCTAssertEqual(PersonName.listed(name: "Smith, Jane", lastName: "Smith"), "Smith, Jane")
        // Nothing at all.
        XCTAssertEqual(PersonName.listed(name: "", lastName: "Smith"), "")
        XCTAssertEqual(PersonName.listed(name: "", lastName: ""), "")
    }

    func testCaseDoesNotMatterAndTheNamesOwnSpellingIsKept() {
        XCTAssertEqual(PersonName.listed(name: "Avery McDonald", lastName: "MCDONALD"), "McDonald, Avery")
        XCTAssertEqual(PersonName.listed(name: "jane smith", lastName: "Smith"), "smith, jane")
    }

    func testStraySpacesAreCleanedUp() {
        // Seen in real data: a trailing space, a leading tab, doubled spaces.
        XCTAssertEqual(PersonName.listed(name: "  Jane   Smith ", lastName: "Smith "), "Smith, Jane")
        XCTAssertEqual(PersonName.listed(name: "\tPinecone  Bakery ", lastName: ""), "Pinecone Bakery")
    }

    func testAWordThatOnlyEndsLikeTheLastNameDoesNotCount() {
        // "Goldsmith" ends in "smith" but is not the last name "Smith".
        XCTAssertEqual(PersonName.listed(name: "Jane Goldsmith", lastName: "Smith"), "Jane Goldsmith")
    }

    func testStopListName() {
        XCTAssertEqual(Fixtures.quill.listName, "Quillfeather, Jordan")
        XCTAssertEqual(Fixtures.business.listName, "Pinecone Bakery LLC")
    }

    func testInitial() {
        XCTAssertEqual(PersonName.initial("sam"), "S")
        XCTAssertEqual(PersonName.initial("  Pedro "), "P")
        XCTAssertEqual(PersonName.initial("'Ángel"), "Á")
        XCTAssertEqual(PersonName.initial(""), "")
        XCTAssertEqual(PersonName.initial("   "), "")
    }

    func testLongDay() {
        XCTAssertEqual(ZestDates.longDay("2026-11-12"), "Thursday, Nov 12")
        XCTAssertEqual(ZestDates.longDay("2026-11-13"), "Friday, Nov 13")
        XCTAssertEqual(ZestDates.longDay("2027-01-01"), "Friday, Jan 1")
        XCTAssertEqual(ZestDates.longDay("2028-02-29"), "Tuesday, Feb 29", "a leap day")
        XCTAssertNil(ZestDates.longDay("2027-02-30"), "not a real date")
        XCTAssertNil(ZestDates.longDay("2026-13-01"))
        XCTAssertNil(ZestDates.longDay("today"))
        XCTAssertNil(ZestDates.longDay(""))
    }
}
