import XCTest
@testable import ZestCore

final class SpokenMatcherTests: XCTestCase {
    private func evaluate(_ words: String?, _ target: Stop) -> SpokenMatcher.Outcome {
        SpokenMatcher.evaluate(transcript: words, target: target, all: Fixtures.route)
    }

    // MARK: Matching the chosen stop

    func testLastNameMatches() {
        let outcome = evaluate("Hi this is Sample at the Quillfeather home. I mowed the lawn.", Fixtures.quill)
        XCTAssertEqual(outcome.result, .match)
        XCTAssertTrue(outcome.target.lastName)
        XCTAssertNil(outcome.better)
    }

    func testCaseAndPunctuationDoNotMatter() {
        // A possessive adds a letter; one slip is allowed on a name this long.
        XCTAssertEqual(evaluate("at the QUILLFEATHER'S place, all done!", Fixtures.quill).result, .match)
        XCTAssertEqual(evaluate("at the QUILLFEATHER place, all done!", Fixtures.quill).result, .match)
        XCTAssertEqual(evaluate("Maple. Mowed, edged, blew off.", Fixtures.maple).result, .match)
    }

    func testSmallMisspellingsOfANameStillMatch() {
        // What a recognizer might write for an unusual surname.
        XCTAssertEqual(evaluate("this is the Ostrandor home", Fixtures.ostrander).result, .match)
        XCTAssertEqual(evaluate("this is the Quilfeather home", Fixtures.quill).result, .match)
        XCTAssertEqual(evaluate("this is the quill feather home", Fixtures.quill).result, .match, "split in two words")
        XCTAssertEqual(evaluate("this is the Winterborn home", Fixtures.noPin).result, .match)
    }

    func testShortNamesNeedAnExactHit() {
        XCTAssertTrue(SpokenMatcher.fuzzyEqual("maple", "maples"))
        XCTAssertFalse(SpokenMatcher.fuzzyEqual("oak", "oat"))
        XCTAssertFalse(SpokenMatcher.fuzzyEqual("main", "mane"))
        XCTAssertFalse(SpokenMatcher.fuzzyEqual("ostrander", "alexander"))
        XCTAssertEqual(SpokenMatcher.editDistance("kitten", "sitting"), 3)
        XCTAssertEqual(SpokenMatcher.editDistance("", "abc"), 3)
    }

    func testStreetNameMatches() {
        let outcome = evaluate("I am on Birchwood Lane, fertilizer is down", Fixtures.maple)
        XCTAssertEqual(outcome.result, .match)
        XCTAssertTrue(outcome.target.street)
        XCTAssertFalse(outcome.target.lastName)
    }

    func testMultiWordStreetNeedsEveryWord() {
        XCTAssertTrue(SpokenMatcher.score(transcript: "here on harbor view drive", stop: Fixtures.quill).street)
        XCTAssertFalse(SpokenMatcher.score(transcript: "nice view from here", stop: Fixtures.quill).street)
    }

    func testNumberedStreetSpokenOrWritten() {
        XCTAssertTrue(SpokenMatcher.score(transcript: "seventy seven fifth street", stop: Fixtures.fifth).street)
        XCTAssertTrue(SpokenMatcher.score(transcript: "at 77 5th street", stop: Fixtures.fifth).street)
        XCTAssertTrue(SpokenMatcher.score(transcript: "seventy seven fifth street", stop: Fixtures.fifth).houseNumber)
    }

    func testBusinessNameWithoutALastName() {
        XCTAssertEqual(evaluate("this is Pinecone Bakery, sprayed the perimeter", Fixtures.business).result, .match)
        XCTAssertEqual(evaluate("this is the bakery, sprayed the perimeter", Fixtures.business).result, .mismatch)
    }

    // MARK: House numbers

    func testHouseNumberSpokenInChunks() {
        XCTAssertTrue(SpokenMatcher.numberCandidates(SpokenMatcher.tokens("one twenty three harbor view")).contains("123"))
        XCTAssertTrue(SpokenMatcher.numberCandidates(SpokenMatcher.tokens("one two three harbor view")).contains("123"))
        XCTAssertTrue(SpokenMatcher.numberCandidates(SpokenMatcher.tokens("one hundred twenty three")).contains("123"))
        XCTAssertTrue(SpokenMatcher.numberCandidates(SpokenMatcher.tokens("one hundred and twenty three")).contains("123"))
        XCTAssertTrue(SpokenMatcher.numberCandidates(SpokenMatcher.tokens("twenty forty cedar crest")).contains("2040"))
        XCTAssertTrue(SpokenMatcher.numberCandidates(SpokenMatcher.tokens("four ten birchwood")).contains("410"))
        XCTAssertTrue(SpokenMatcher.numberCandidates(SpokenMatcher.tokens("four oh one")).contains("401"))
        XCTAssertTrue(SpokenMatcher.numberCandidates(SpokenMatcher.tokens("one hundred five")).contains("105"))
        XCTAssertTrue(SpokenMatcher.numberCandidates(SpokenMatcher.tokens("twelve hundred")).contains("1200"))
        XCTAssertTrue(SpokenMatcher.numberCandidates(SpokenMatcher.tokens("twelve thirty four")).contains("1234"))
        XCTAssertTrue(SpokenMatcher.numberCandidates(SpokenMatcher.tokens("I am at 123 Harbor View")).contains("123"))
        XCTAssertTrue(SpokenMatcher.numberCandidates(SpokenMatcher.tokens("I am at 1 23 Harbor View")).contains("123"))
        XCTAssertTrue(SpokenMatcher.numberCandidates(SpokenMatcher.tokens("no numbers here")).isEmpty)
    }

    func testFullHouseNumberAloneCounts() {
        let outcome = evaluate("this is four ten, all mowed and edged", Fixtures.maple)
        XCTAssertEqual(outcome.result, .match)
        XCTAssertTrue(outcome.target.houseNumber)
    }

    func testShortHouseNumberOnlyCountsWithTheStreet() {
        // "9 N Market Way": a lone nine in ordinary talk must not match.
        XCTAssertFalse(SpokenMatcher.score(transcript: "I fixed nine sprinkler heads", stop: Fixtures.business).houseNumber)
        let withStreet = SpokenMatcher.score(transcript: "nine market way all done", stop: Fixtures.business)
        XCTAssertTrue(withStreet.street)
        XCTAssertTrue(withStreet.houseNumber)
    }

    // MARK: Telling stops apart

    func testNextDoorNeighboursAreToldApartByNumber() {
        // The tech picked 123 but says 125 on the same street.
        let outcome = evaluate("this is one twenty five harbor view drive", Fixtures.quill)
        XCTAssertEqual(outcome.result, .mismatch)
        XCTAssertEqual(outcome.better?.customerId, Fixtures.ostrander.customerId)
        // Saying the right number is a clean match with no suggestion.
        let right = evaluate("this is one twenty three harbor view drive", Fixtures.quill)
        XCTAssertEqual(right.result, .match)
        XCTAssertNil(right.better)
    }

    func testStreetOnlyIsATieBetweenNeighboursAndStillMatches() {
        let outcome = evaluate("here on harbor view drive, lawn is mowed", Fixtures.quill)
        XCTAssertEqual(outcome.result, .match)
        XCTAssertNil(outcome.better)
    }

    func testADifferentStopsNameIsAMismatchWithASuggestion() {
        let outcome = evaluate("this is the Ostrander home, mowed front and back", Fixtures.maple)
        XCTAssertEqual(outcome.result, .mismatch)
        XCTAssertEqual(outcome.better?.customerId, Fixtures.ostrander.customerId)
    }

    func testNothingRecognisableIsAMismatchWithNoSuggestion() {
        let outcome = evaluate("I mowed the lawn and trimmed the hedges today", Fixtures.maple)
        XCTAssertEqual(outcome.result, .mismatch)
        XCTAssertNil(outcome.better)
        XCTAssertEqual(outcome.target.value, 0)
    }

    func testNoSpeechIsUnknownNotMismatch() {
        XCTAssertEqual(evaluate(nil, Fixtures.maple).result, .unknown)
        XCTAssertEqual(evaluate("", Fixtures.maple).result, .unknown)
        XCTAssertEqual(evaluate("um", Fixtures.maple).result, .unknown)
    }

    // MARK: Address parsing and hints

    func testAddressParts() {
        XCTAssertEqual(SpokenMatcher.addressParts("123 Harbor View Dr").houseNumber, "123")
        XCTAssertEqual(SpokenMatcher.addressParts("123 Harbor View Dr").street, ["harbor", "view"])
        XCTAssertEqual(SpokenMatcher.addressParts("123-B Harbor View Dr.").houseNumber, "123")
        XCTAssertEqual(SpokenMatcher.addressParts("9 N Market Way").street, ["market"])
        XCTAssertEqual(SpokenMatcher.addressParts("77 5th St Apt 4").street, ["5th"])
        XCTAssertEqual(SpokenMatcher.addressParts("77 Fifth Street #4").street, ["5th"])
        XCTAssertEqual(SpokenMatcher.addressParts("2040 Cedar Crest Ct").streetDisplay, "Cedar Crest")
        XCTAssertEqual(SpokenMatcher.addressParts("400 Court St").street, ["court"], "a suffix word can be the name")
        XCTAssertEqual(SpokenMatcher.addressParts("15 Elm St SW").street, ["elm"])
        XCTAssertNil(SpokenMatcher.addressParts("Harbor View Dr").houseNumber)
        XCTAssertEqual(SpokenMatcher.addressParts("").street, [])
    }

    func testTokensFoldAccentsAndApostrophes() {
        XCTAssertEqual(SpokenMatcher.tokens("O'Brien-Núñez, José!"), ["obrien", "nunez", "jose"])
        XCTAssertEqual(SpokenMatcher.tokens("Fifth  ST."), ["5th", "st"])
    }

    func testHintsAreLastNamesAndStreetsWithoutRepeats() {
        let hints = SpokenMatcher.hints(for: Fixtures.route)
        XCTAssertTrue(hints.contains("Quillfeather"))
        XCTAssertTrue(hints.contains("Harbor View"))
        XCTAssertTrue(hints.contains("Cedar Crest"))
        XCTAssertEqual(hints.filter { $0 == "Harbor View" }.count, 1, "two neighbours share one street hint")
        XCTAssertFalse(hints.contains(""))
    }
}
