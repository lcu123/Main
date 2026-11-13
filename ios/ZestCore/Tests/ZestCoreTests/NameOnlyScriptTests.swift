import XCTest
@testable import ZestCore

/// The tech's script: "Hi John. Here's what I did. Here's what I recommend.
/// I closed the backyard gate." He says the customer's first name and
/// nothing about the street. Every name here is made up.
final class NameOnlyScriptTests: XCTestCase {
    private func stop(_ id: Int, _ name: String, last: String, _ address: String) -> Stop {
        Fixtures.stop(id, name, last: last, address, lat: 38.6 + Double(id) / 1000, lng: -121.3, order: id)
    }

    private lazy var john = stop(1, "John Carver", last: "Carver", "410 Birchwood Ln")
    private lazy var otherJohn = stop(2, "John Albright", last: "Albright", "9 Elm St")
    private lazy var maryAnn = stop(3, "Mary Ann O'Brien", last: "O'Brien", "22 Cedar Ct")
    private lazy var couple = stop(4, "Pedro & Lucia Vega", last: "Vega", "7 Oak Ave")
    private lazy var rose = stop(5, "Rose Whitlock", last: "Whitlock", "31 Pine Way")
    private lazy var will = stop(6, "Will Harrow", last: "Harrow", "5 Park Blvd")
    private lazy var al = stop(7, "Al Nakamura", last: "Nakamura", "18 Lake Dr")
    private lazy var sam = stop(8, "Sam Winterbourne", last: "Winterbourne", "2040 Cedar Crest Ct")
    private lazy var business = stop(9, "Pinecone Bakery LLC", last: "", "9 N Market Way")
    private lazy var all = [john, otherJohn, maryAnn, couple, rose, will, al, sam, business]

    private let script = "Here's what I did. Here's what I recommend. I closed the backyard gate."

    private func heard(_ words: String, at target: Stop, tech: String? = nil) -> SpokenMatcher.Outcome {
        SpokenMatcher.evaluate(transcript: words, target: target, all: all, techName: tech)
    }

    // MARK: The first name ("Hi John")

    func testCalledByTheFirstName() {
        XCTAssertEqual(PersonName.first(name: "John Carver", lastName: "Carver"), "John")
        XCTAssertEqual(PersonName.first(name: "Mary Ann O'Brien", lastName: "O'Brien"), "Mary Ann")
        XCTAssertEqual(PersonName.first(name: "Pedro & Lucia Vega", lastName: "Vega"), "Pedro & Lucia")
        XCTAssertEqual(PersonName.first(name: "Pieter van der Berg", lastName: "van der Berg"), "Pieter")
        XCTAssertEqual(PersonName.first(name: "José Peña", lastName: "Pena"), "José", "accents on file do not matter")
        XCTAssertEqual(PersonName.first(name: "  Jane   Smith ", lastName: "Smith"), "Jane")
        XCTAssertEqual(PersonName.first(name: "Smith, Jane", lastName: "Smith"), "Jane", "already written last name first")
        XCTAssertEqual(PersonName.first(name: "John Smith Jr", lastName: "Smith"), "John", "the last name is not at the end: the first word")
        XCTAssertEqual(PersonName.first(name: "Kai 🌻 Rivers", lastName: "Rivers"), "Kai 🌻")
    }

    func testABusinessIsCalledByItsNameAndNothingGivesNothing() {
        XCTAssertEqual(PersonName.first(name: "Pinecone Bakery LLC", lastName: ""), "Pinecone Bakery LLC")
        XCTAssertEqual(PersonName.first(name: "Prince", lastName: ""), "Prince")
        XCTAssertEqual(PersonName.first(name: "Cher", lastName: "Cher"), "Cher", "a name that is only the last name")
        XCTAssertEqual(PersonName.first(name: "", lastName: "Smith"), "")
        XCTAssertEqual(PersonName.first(name: "   ", lastName: ""), "")
        XCTAssertEqual(business.greetingName, "Pinecone Bakery LLC")
        XCTAssertEqual(maryAnn.greetingName, "Mary Ann")
    }

    // MARK: The lines on the record screen

    func testTheServersFourLines() {
        XCTAssertEqual(AppConfig.defaultPrompts, ["Hi {name}", "Here's what I did", "Here's what I recommend", "I closed the backyard gate"])
        XCTAssertEqual(
            PromptText.fill(AppConfig.defaultPrompts, name: john.greetingName),
            ["Hi John", "Here's what I did", "Here's what I recommend", "I closed the backyard gate"]
        )
    }

    func testTheNameGoesIntoAnyLine() {
        XCTAssertEqual(PromptText.fill("Hi {name}", name: "Mary Ann"), "Hi Mary Ann")
        XCTAssertEqual(PromptText.fill("Hi {name}", name: "O'Brien"), "Hi O'Brien")
        XCTAssertEqual(PromptText.fill("Hi {name}", name: "Pinecone Bakery LLC"), "Hi Pinecone Bakery LLC")
        XCTAssertEqual(PromptText.fill("Thanks, {name}!", name: "José"), "Thanks, José!")
        XCTAssertEqual(PromptText.fill("{name}, here's what I did", name: "Kai 🌻"), "Kai 🌻, here's what I did")
        XCTAssertEqual(PromptText.fill("Hi {name}. Bye {name}.", name: "Jo"), "Hi Jo. Bye Jo.", "more than once")
        XCTAssertEqual(PromptText.fill("Here's what I did", name: "John"), "Here's what I did", "a line with no name is left alone")
        XCTAssertEqual(PromptText.fill("Hi {name}", name: "  John  "), "Hi John")
        let long = "Greater Riverbend Homeowners Association Inc."
        XCTAssertEqual(PromptText.fill("Hi {name}", name: long), "Hi " + long, "a very long name is kept whole; the screen shrinks and wraps it")
    }

    func testWithNoNameThePlaceholderGoesCleanly() {
        XCTAssertEqual(PromptText.fill("Hi {name}", name: nil), "Hi")
        XCTAssertEqual(PromptText.fill("Hi {name}", name: ""), "Hi")
        XCTAssertEqual(PromptText.fill("Hi {name}", name: "   "), "Hi")
        XCTAssertEqual(PromptText.fill("Hi {name}, here's what I did", name: nil), "Hi, here's what I did")
        XCTAssertEqual(PromptText.fill("Hi {name}.", name: nil), "Hi.")
        XCTAssertEqual(PromptText.fill("Thanks, {name}!", name: nil), "Thanks!")
        XCTAssertEqual(PromptText.fill("{name}, here's what I did", name: nil), "here's what I did")
        XCTAssertEqual(PromptText.fill("Hello {name} and welcome", name: nil), "Hello and welcome")
        XCTAssertEqual(PromptText.fill("{name}", name: nil), "")
        XCTAssertFalse(PromptText.fill(AppConfig.defaultPrompts, name: nil).contains { $0.contains("{") })
    }

    // MARK: The spoken key hears the first name

    func testTheFirstNameAloneIsEnough() {
        let outcome = heard("Hi Mary Ann. \(script)", at: maryAnn)
        XCTAssertEqual(outcome.result, .match)
        XCTAssertTrue(outcome.target.firstName)
        XCTAssertFalse(outcome.target.lastName)
        XCTAssertFalse(outcome.target.street, "nothing about the street was said, and nothing needed")
        XCTAssertEqual(outcome.target.value, 2)
        XCTAssertEqual(heard("Hi Mary. \(script)", at: maryAnn).result, .match, "one of two first names")
        XCTAssertEqual(heard("Hi Pedro. \(script)", at: couple).result, .match, "either of a couple")
        XCTAssertEqual(heard("Hi Lucia. \(script)", at: couple).result, .match)
        XCTAssertEqual(heard("hi sam", at: sam).result, .match, "two words are enough to judge")
    }

    func testTwoCustomersWithTheSameFirstNameDoNotWarn() {
        // A tie with another stop is still a match.
        XCTAssertEqual(heard("Hi John. \(script)", at: john).result, .match)
        XCTAssertEqual(heard("Hi John. \(script)", at: otherJohn).result, .match)
        // The last name settles it when he says it.
        XCTAssertEqual(heard("Hi John Carver. \(script)", at: john).result, .match)
        let outcome = heard("Hi John Carver. \(script)", at: otherJohn)
        XCTAssertEqual(outcome.result, .mismatch, "first name 2 against first and last name 5")
        XCTAssertEqual(outcome.better?.customerId, john.customerId)
    }

    func testAnotherCustomersFirstNameIsAMismatchThatPointsToThem() {
        let outcome = heard("Hi Sam. \(script)", at: john)
        XCTAssertEqual(outcome.result, .mismatch)
        XCTAssertEqual(outcome.better?.customerId, sam.customerId)
        // Two Johns: the words fit both equally, so neither is suggested.
        let either = heard("Hi John. \(script)", at: sam)
        XCTAssertEqual(either.result, .mismatch)
        XCTAssertNil(either.better)
    }

    func testNoNameAtAllIsAMismatchWithNothingSuggested() {
        let outcome = heard(script, at: john)
        XCTAssertEqual(outcome.result, .mismatch)
        XCTAssertNil(outcome.better)
        XCTAssertEqual(outcome.target.value, 0)
    }

    func testTheLastNameStillCountsAndCountsForMore() {
        let last = SpokenMatcher.score(transcript: "this is the Carver home", stop: john)
        XCTAssertTrue(last.lastName)
        XCTAssertEqual(last.value, 3)
        XCTAssertEqual(SpokenMatcher.score(transcript: "Hi John Carver", stop: john).value, 5)
        XCTAssertEqual(SpokenMatcher.score(transcript: "Hi John", stop: john).value, 2)
    }

    func testTheStreetAndHouseNumberAreSilentBackups() {
        // Not asked for any more, but if he says them they still count.
        let street = SpokenMatcher.score(transcript: "I am at four ten Birchwood Lane", stop: john)
        XCTAssertTrue(street.street)
        XCTAssertTrue(street.houseNumber)
        XCTAssertFalse(street.firstName)
        XCTAssertEqual(heard("I am at four ten Birchwood Lane", at: john).result, .match)
    }

    func testABusinessGoesByItsNameAsBefore() {
        XCTAssertEqual(heard("Hi Pinecone Bakery. \(script)", at: business).result, .match)
        let score = SpokenMatcher.score(transcript: "Hi Pinecone Bakery", stop: business)
        XCTAssertTrue(score.lastName, "the telling words of a business name count as its name")
        XCTAssertFalse(score.firstName)
    }

    // MARK: Names that could be heard by accident

    func testAFirstNameThatIsAnEverydayWordOnlyCountsAfterAGreeting() {
        // Rose and Will are customers. Nobody was named here.
        let talk = "I will spray the rose bushes again in May"
        XCTAssertFalse(SpokenMatcher.score(transcript: talk, stop: rose).firstName)
        XCTAssertFalse(SpokenMatcher.score(transcript: talk, stop: will).firstName)
        XCTAssertEqual(heard("Hi John. \(talk)", at: john).result, .match, "and so they do not outvote the name that was said")
        // Said to the customer, they count.
        XCTAssertTrue(SpokenMatcher.score(transcript: "Hi Rose. \(script)", stop: rose).firstName)
        XCTAssertTrue(SpokenMatcher.score(transcript: "Good morning Will, \(script)", stop: will).firstName)
        XCTAssertEqual(heard("Hi Rose. I will be back in May.", at: rose).result, .match)
        XCTAssertEqual(heard("Hi Will. I trimmed the rose bushes.", at: will).result, .match)
    }

    func testAVeryShortFirstNameDoesNotCountAlone() {
        // "Al" is two letters: too easy to hear inside other words.
        XCTAssertFalse(SpokenMatcher.score(transcript: "Hi Al. \(script)", stop: al).firstName)
        XCTAssertEqual(heard("Hi Al Nakamura. \(script)", at: al).result, .match, "his last name still does")
        XCTAssertEqual(SpokenMatcher.firstNameWords(of: al), [])
        XCTAssertEqual(SpokenMatcher.firstNameWords(of: couple), ["pedro", "lucia"], "the & is not a name")
        XCTAssertEqual(SpokenMatcher.firstNameWords(of: maryAnn), ["mary", "ann"])
        XCTAssertEqual(SpokenMatcher.firstNameWords(of: business), ["pinecone", "bakery", "llc"], "(a business is scored by its name, not by this)")
    }

    func testAFirstNameMustBeSpelledRightNotNearly() {
        let grant = stop(20, "Grant Ellison", last: "Ellison", "3 Hill Rd")
        XCTAssertFalse(SpokenMatcher.score(transcript: "a grand job on the lawn today", stop: grant).firstName)
        XCTAssertTrue(SpokenMatcher.score(transcript: "Hi Grant", stop: grant).firstName)
        XCTAssertTrue(SpokenMatcher.score(transcript: "Hi Jose", stop: stop(21, "José Peña", last: "Peña", "8 Verde Rd")).firstName, "accents aside")
    }

    func testTheTechsOwnNameIsNotTakenForACustomer() {
        // Sam the tech is at John's. A customer called Sam is on the route too.
        let words = "Hi John, this is Sam with Zest. \(script)"
        XCTAssertEqual(heard(words, at: john, tech: "Sam").result, .match)
        // The recognizer missed "John": Sam the customer must not be suggested.
        let missed = heard("Hi there, this is Sam with Zest. \(script)", at: john, tech: "Sam")
        XCTAssertEqual(missed.result, .mismatch)
        XCTAssertNil(missed.better, "the tech's own name points nowhere")
        // At Sam the customer's own stop, "Hi Sam" still counts.
        XCTAssertEqual(heard("Hi Sam. \(script)", at: sam, tech: "Sam").result, .match)
        // Without knowing the tech's name the old behaviour stands.
        XCTAssertEqual(heard("Hi there, this is Sam with Zest.", at: john).better?.customerId, sam.customerId)
    }

    // MARK: Hints for the recognizer

    func testFirstNamesAreGivenToTheRecognizer() {
        let hints = SpokenMatcher.hints(for: all)
        for expected in ["John", "Mary Ann", "Mary", "Ann", "Pedro", "Lucia", "Rose", "Will", "Al", "Sam", "Carver", "O'Brien", "Birchwood"] {
            XCTAssertTrue(hints.contains(expected), "\(expected) should be a hint; hints are \(hints)")
        }
        XCTAssertEqual(hints.filter { $0 == "John" }.count, 1, "two Johns, one hint")
        XCTAssertFalse(hints.contains("&"))
        XCTAssertFalse(hints.contains("Pinecone Bakery LLC"), "a business has no first name")
        // Names come before streets, so a long route cuts streets first.
        XCTAssertLessThan(hints.firstIndex(of: "Sam") ?? 999, hints.firstIndex(of: "Birchwood") ?? -1)
        let many = (0..<120).map { stop(100 + $0, "First\($0) Last\($0)", last: "Last\($0)", "\($0) Street\($0) Rd") }
        let cut = SpokenMatcher.hints(for: many)
        XCTAssertEqual(cut.count, 100)
        XCTAssertTrue(cut.allSatisfy { $0.hasPrefix("First") }, "with 120 customers the first names fill the list")
    }

    // MARK: The warning's words

    func testTheWarningAsksForTheNameOnly() {
        let verdict = TwoKeys.verdict(Signals(spoken: .mismatch, gps: .match, checkIn: .unknown))
        XCTAssertEqual(verdict.reasons, ["I did not hear this customer's name."])
        XCTAssertFalse(verdict.reasons.joined().lowercased().contains("street"))
    }

    func testTwoKeysPassesTheTechsNameAlong() {
        let fix = VideoLocation(lat: john.lat ?? 0, lng: john.lng ?? 0, accuracyM: 5)
        let outcome = TwoKeys.signals(
            transcript: "Hi there, this is Sam with Zest", fix: fix, recordedAt: Fixtures.base, stop: john, all: all,
            farMeters: 150, techName: "Sam"
        )
        XCTAssertEqual(outcome.signals.spoken, .mismatch)
        XCTAssertNil(outcome.better)
    }
}
