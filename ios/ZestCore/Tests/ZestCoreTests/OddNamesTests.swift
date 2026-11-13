import XCTest
@testable import ZestCore

/// Awkward customers: apostrophes, accents, hyphens, particles, businesses,
/// capitals, an emoji, one-word names, units, a PO box. Every name and
/// address here is made up.
final class OddNamesTests: XCTestCase {
    private func stop(_ id: Int, _ name: String, last: String, _ address: String) -> Stop {
        Fixtures.stop(id, name, last: last, address, lat: 38.6 + Double(id) / 1000, lng: -121.3, order: id)
    }

    private lazy var obrien = stop(1, "Sean O'Brien", last: "O'Brien", "220 Saint Andrews Dr")
    private lazy var pena = stop(2, "José Peña", last: "Peña", "8 Cañada Verde Rd")
    private lazy var mcdonald = stop(3, "Anne-Marie McDonald-Smith", last: "McDonald-Smith", "1200 Long Winding Meadow Creek Boulevard Apt 14B")
    private lazy var vanDerBerg = stop(4, "Pieter van der Berg", last: "van der Berg", "77 5th Ave Unit 3")
    private lazy var business = stop(5, "Greater Riverbend Homeowners Association Inc.", last: "", "4500 Hwy 50")
    private lazy var capitals = stop(6, "MARGARET THORNWOOD-HIGHTOWER", last: "THORNWOOD-HIGHTOWER", "14 St Marys Ct")
    private lazy var emoji = stop(7, "Kai 🌻 Rivers", last: "Rivers", "31 Mt Shasta Way")
    private lazy var oneWord = stop(8, "Prince", last: "", "9 Elm St")
    private lazy var poBox = stop(9, "Lee Wong", last: "Wong", "PO Box 1432")
    private lazy var longWord = stop(10, "Maximiliano Wolfeschlegelsteinhausen", last: "Wolfeschlegelsteinhausen", "14 Elm St")
    private lazy var all = [obrien, pena, mcdonald, vanDerBerg, business, capitals, emoji, oneWord, poBox, longWord]

    private func heard(_ words: String, at target: Stop) -> SpokenMatcher.Outcome {
        SpokenMatcher.evaluate(transcript: words, target: target, all: all)
    }

    // MARK: "Last, First"

    func testLastCommaFirstForEveryAwkwardName() {
        XCTAssertEqual(obrien.listName, "O'Brien, Sean")
        XCTAssertEqual(pena.listName, "Peña, José")
        XCTAssertEqual(mcdonald.listName, "McDonald-Smith, Anne-Marie")
        XCTAssertEqual(vanDerBerg.listName, "van der Berg, Pieter")
        XCTAssertEqual(business.listName, "Greater Riverbend Homeowners Association Inc.", "a business is left as it is")
        XCTAssertEqual(business.name.count, 45, "the long business name is 45 characters")
        XCTAssertEqual(capitals.listName, "THORNWOOD-HIGHTOWER, MARGARET", "capitals stay capitals")
        XCTAssertEqual(emoji.listName, "Rivers, Kai 🌻")
        XCTAssertEqual(oneWord.listName, "Prince")
        XCTAssertEqual(poBox.listName, "Wong, Lee")
        XCTAssertEqual(longWord.listName, "Wolfeschlegelsteinhausen, Maximiliano")
    }

    func testTheLastNameOnFileMayDifferInAccentsCaseAndApostrophes() {
        // FieldRoutes often has the last name typed plainer than the full name.
        XCTAssertEqual(PersonName.listed(name: "José Peña", lastName: "Pena"), "Peña, José", "the full name's own spelling is kept")
        XCTAssertEqual(PersonName.listed(name: "José Peña", lastName: "PEÑA"), "Peña, José")
        XCTAssertEqual(PersonName.listed(name: "Sean O\u{2019}Brien", lastName: "O'Brien"), "O\u{2019}Brien, Sean", "curly or straight apostrophe")
        XCTAssertEqual(PersonName.listed(name: "Anne McDonald\u{2013}Smith", lastName: "McDonald-Smith"), "McDonald\u{2013}Smith, Anne", "any kind of dash")
        XCTAssertEqual(PersonName.listed(name: "Zoë Brontë", lastName: "Bronte"), "Brontë, Zoë")
    }

    func testNamesThatCannotBeTurnedRoundAreLeftAlone() {
        XCTAssertEqual(PersonName.listed(name: "Cher", lastName: "Cher"), "Cher", "a name that is only the last name")
        XCTAssertEqual(PersonName.listed(name: "John Smith Jr", lastName: "Smith"), "John Smith Jr", "the last name is not at the end")
        XCTAssertEqual(PersonName.listed(name: "🌻", lastName: ""), "🌻")
        XCTAssertEqual(PersonName.listed(name: "", lastName: "Smith"), "")
        XCTAssertEqual(PersonName.listed(name: "Dr. María-José de la Cruz", lastName: "de la Cruz"), "de la Cruz, Dr. María-José")
    }

    func testInitialsForBadges() {
        XCTAssertEqual(PersonName.initial("José"), "J")
        XCTAssertEqual(PersonName.initial("🌻 Sunny"), "S", "an emoji is not an initial")
        XCTAssertEqual(PersonName.initial("'Akoni"), "A")
        XCTAssertEqual(PersonName.initial("éric"), "É")
        XCTAssertEqual(PersonName.initial("🌻"), "")
    }

    // MARK: The spoken key

    func testApostrophesAreNotNeededToMatch() {
        XCTAssertEqual(heard("this is the O'Brien house", at: obrien).result, .match)
        XCTAssertEqual(heard("this is the OBrien house", at: obrien).result, .match)
        XCTAssertEqual(heard("this is the O Brien house", at: obrien).result, .match, "written as two words by the recognizer")
        XCTAssertEqual(heard("this is the O\u{2019}Brien house", at: obrien).result, .match, "curly apostrophe")
        XCTAssertEqual(heard("this is the Brian house", at: obrien).result, .mismatch, "Brian is not O'Brien")
    }

    func testAccentsAreNotNeededToMatch() {
        XCTAssertEqual(heard("at the Pena home", at: pena).result, .match)
        XCTAssertEqual(heard("at the Peña home", at: pena).result, .match)
        XCTAssertEqual(heard("on Canada Verde", at: pena).result, .match, "the street, said without the accent")
        XCTAssertTrue(SpokenMatcher.score(transcript: "eight cañada verde road", stop: pena).street)
    }

    func testHyphenatedNamesMatchWithOrWithoutTheHyphen() {
        XCTAssertEqual(heard("the McDonald-Smith home", at: mcdonald).result, .match)
        XCTAssertEqual(heard("the McDonald Smith home", at: mcdonald).result, .match)
        XCTAssertEqual(heard("the Mc Donald Smith home", at: mcdonald).result, .match, "written in three words by the recognizer")
        XCTAssertEqual(heard("the MACDONALD SMITH home", at: mcdonald).result, .match, "one letter off in a long name")
        XCTAssertFalse(SpokenMatcher.score(transcript: "the Smith home", stop: mcdonald).lastName, "half a double name is not the name")
        XCTAssertEqual(heard("the Thornwood Hightower home", at: capitals).result, .match, "capitals on file do not matter")
    }

    func testNamesWithParticlesMatchHoweverTheyAreWritten() {
        XCTAssertEqual(heard("the van der Berg home", at: vanDerBerg).result, .match)
        XCTAssertEqual(heard("the Vanderberg home", at: vanDerBerg).result, .match, "run together by the recognizer")
        XCTAssertEqual(heard("the Vandenberg home", at: vanDerBerg).result, .match, "a near miss on a long name")
        XCTAssertFalse(SpokenMatcher.score(transcript: "the berg home", stop: vanDerBerg).lastName, "one part is not the name")
    }

    func testBusinessOneWordEmojiAndLongNames() {
        XCTAssertEqual(heard("Greater Riverbend Homeowners", at: business).result, .match, "Association and Inc are not needed")
        XCTAssertEqual(heard("this is Prince", at: oneWord).result, .match)
        XCTAssertEqual(heard("the Rivers home", at: emoji).result, .match)
        XCTAssertEqual(heard("the Wolfeschlegelsteinhausen home", at: longWord).result, .match)
        XCTAssertEqual(heard("the Wolfschlegelsteinhausen home", at: longWord).result, .match, "one letter dropped from a very long name")
    }

    func testSaintAndStAreTheSameStreet() {
        // Written "Saint", said either way.
        XCTAssertTrue(SpokenMatcher.score(transcript: "two twenty Saint Andrews Drive", stop: obrien).street)
        XCTAssertTrue(SpokenMatcher.score(transcript: "220 St Andrews", stop: obrien).street)
        // Written "St", said either way.
        XCTAssertTrue(SpokenMatcher.score(transcript: "fourteen Saint Marys Court", stop: capitals).street)
        XCTAssertTrue(SpokenMatcher.score(transcript: "14 St. Mary's Ct", stop: capitals).street)
        // "Mount" and "Mt", "Highway" and "Hwy".
        XCTAssertTrue(SpokenMatcher.score(transcript: "thirty one Mount Shasta Way", stop: emoji).street)
        XCTAssertTrue(SpokenMatcher.score(transcript: "31 Mt Shasta", stop: emoji).street)
        XCTAssertTrue(SpokenMatcher.score(transcript: "forty five hundred highway fifty", stop: business).street)
        // "Street" said after another street is not "Saint": Elm St is not St Marys.
        XCTAssertFalse(SpokenMatcher.score(transcript: "nine Elm Street", stop: capitals).street)
        XCTAssertEqual(SpokenMatcher.addressParts("220 Saint Andrews Dr").street, ["st", "andrews"])
        XCTAssertEqual(SpokenMatcher.addressParts("14 St Marys Ct").street, ["st", "marys"])
        XCTAssertEqual(SpokenMatcher.addressParts("14 St Marys Ct").streetDisplay, "St Marys", "the hint keeps the route's own spelling")
    }

    func testBoulevardAndBlvdAndTheOtherEndings() {
        let written = SpokenMatcher.addressParts("1200 Long Winding Meadow Creek Boulevard Apt 14B")
        let short = SpokenMatcher.addressParts("1200 Long Winding Meadow Creek Blvd #14B")
        XCTAssertEqual(written.street, ["long", "winding", "meadow", "creek"])
        XCTAssertEqual(written.street, short.street, "Boulevard and Blvd are the same street")
        XCTAssertEqual(written.houseNumber, "1200")
        XCTAssertEqual(short.houseNumber, "1200")
        XCTAssertTrue(SpokenMatcher.score(transcript: "twelve hundred Long Winding Meadow Creek Boulevard", stop: mcdonald).street)
        XCTAssertTrue(SpokenMatcher.score(transcript: "twelve hundred Long Winding Meadow Creek", stop: mcdonald).houseNumber)
    }

    func testAUnitNumberIsNeverTakenForTheHouseNumber() {
        // "Apt 14B" at number 1200; another customer lives at number 14.
        XCTAssertFalse(SpokenMatcher.score(transcript: "apartment fourteen", stop: mcdonald).houseNumber)
        XCTAssertFalse(SpokenMatcher.score(transcript: "fourteen B", stop: mcdonald).houseNumber)
        XCTAssertTrue(SpokenMatcher.score(transcript: "twelve hundred", stop: mcdonald).houseNumber)
        // The words fit 14 Elm St, not the apartment 14B: say so.
        let outcome = heard("fourteen Elm Street", at: mcdonald)
        XCTAssertEqual(outcome.result, .mismatch)
        XCTAssertEqual(outcome.better?.customerId, longWord.customerId)
        // A unit with no word in front of it, and a numbered street with a unit.
        XCTAssertEqual(SpokenMatcher.addressParts("88 Harbor View Dr 14B").street, ["harbor", "view"])
        XCTAssertEqual(SpokenMatcher.addressParts("77 5th Ave Unit 3").street, ["5th"])
        XCTAssertEqual(SpokenMatcher.addressParts("77 5th Ave Unit 3").houseNumber, "77")
        XCTAssertEqual(SpokenMatcher.addressParts("100 E 5th").street, ["5th"], "a numbered street is a name, not a unit")
        XCTAssertFalse(SpokenMatcher.score(transcript: "unit three", stop: vanDerBerg).houseNumber)
        XCTAssertTrue(SpokenMatcher.score(transcript: "seventy seven fifth avenue", stop: vanDerBerg).street)
    }

    func testAPOBoxIsKnownByItsNumber() {
        for written in ["PO Box 1432", "P.O. Box 1432", "P O Box 1432", "Post Office Box 1432", "POBox 1432"] {
            let parts = SpokenMatcher.addressParts(written)
            XCTAssertEqual(parts.houseNumber, "1432", written)
            XCTAssertEqual(parts.street, [], written)
        }
        XCTAssertTrue(SpokenMatcher.score(transcript: "PO box fourteen thirty two", stop: poBox).houseNumber)
        XCTAssertFalse(SpokenMatcher.score(transcript: "PO box fourteen thirty two", stop: poBox).street, "\"PO box\" is not a street every box shares")
        XCTAssertFalse(SpokenMatcher.score(transcript: "PO box ninety nine", stop: poBox).houseNumber)
        XCTAssertEqual(heard("this is the Wong place PO box fourteen thirty two", at: poBox).result, .match)
        // A street that only starts like one is still a street.
        XCTAssertEqual(SpokenMatcher.addressParts("12 Post Office Rd").street, ["post", "office"])
        XCTAssertEqual(SpokenMatcher.addressParts("12 Box Canyon Rd").street, ["box", "canyon"])
    }

    func testEveryAwkwardStopCanBeToldFromTheOthersByItsName() {
        let said: [(Stop, String)] = [
            (obrien, "the O'Brien home"), (pena, "the Pena home"), (mcdonald, "the McDonald Smith home"),
            (vanDerBerg, "the van der Berg home"), (business, "Greater Riverbend Homeowners"),
            (capitals, "the Thornwood Hightower home"), (emoji, "the Rivers home"), (oneWord, "this is Prince"),
            (poBox, "the Wong home"), (longWord, "the Wolfeschlegelsteinhausen home"),
        ]
        for (target, words) in said {
            XCTAssertEqual(heard(words, at: target).result, .match, "\(words) at \(target.name)")
            for other in all where other.customerId != target.customerId {
                let outcome = heard(words, at: other)
                XCTAssertEqual(outcome.result, .mismatch, "\(words) said at \(other.name)")
                XCTAssertEqual(outcome.better?.customerId, target.customerId, "\(words) said at \(other.name) points to the right stop")
            }
        }
    }

    func testHintsForTheRecognizerKeepTheRoutesSpelling() {
        let hints = SpokenMatcher.hints(for: all)
        for expected in ["O'Brien", "Peña", "McDonald-Smith", "van der Berg", "Saint Andrews", "Cañada Verde", "Long Winding Meadow Creek", "St Marys", "Mt Shasta"] {
            XCTAssertTrue(hints.contains(expected), "\(expected) should be a hint; hints are \(hints)")
        }
        XCTAssertFalse(hints.contains(""), "a PO box gives no empty hint")
    }
}
