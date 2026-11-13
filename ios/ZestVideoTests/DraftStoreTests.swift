import XCTest
import ZestCore
@testable import ZestVideo

/// "The draft stays": an unsaved clip outlives the day's list, which is
/// wiped every night. So the draft on disk has to carry everything needed
/// to check it and send it later.
final class DraftStoreTests: XCTestCase {
    private func makeStore() throws -> (store: DraftStore, videos: URL) {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("zest-drafts-\(UUID().uuidString)")
        let videos = root.appendingPathComponent("Videos")
        try FileManager.default.createDirectory(at: videos, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: root) }
        return (DraftStore(directory: root, videosDirectory: videos), videos)
    }

    private let checkIn = Date(timeIntervalSince1970: 1_800_000_000)

    private var stop: Stop {
        Stop(
            customerId: 2, name: "Jordan Quillfeather", lastName: "Quillfeather", address: "123 Harbor View Dr",
            cityLine: "Sampletown, CA 90000", lat: 38.61, lng: -121.31, appointmentIds: [20, 21], services: ["Mowing"],
            order: 2, checkIn: checkIn, checkOut: checkIn.addingTimeInterval(900), done: true, video: nil
        )
    }

    func testADraftCarriesWhatItNeedsToBeCheckedAndSentOnALaterDay() throws {
        let made = try makeStore()
        let draft = Draft(
            id: "d1", target: RecordTarget(stop: stop), fileName: "d1.mp4", stopDate: "2026-11-13",
            recordedAt: checkIn.addingTimeInterval(300), durationSec: 14.6, sizeBytes: 5,
            location: VideoLocation(lat: 38.61, lng: -121.31, accuracyM: 8), createdAt: checkIn.addingTimeInterval(320)
        )
        try Data("x".utf8).write(to: made.videos.appendingPathComponent("d1.mp4"))
        made.store.saveDraft(draft)

        // As a new launch finds it, with no route in memory at all.
        guard let loaded = made.store.loadDraft() else { return XCTFail("the draft did not load") }
        XCTAssertEqual(loaded, draft)
        // Who and where, for the question and for the server.
        XCTAssertEqual(loaded.target.customerId, 2)
        XCTAssertEqual(loaded.target.name, "Quillfeather, Jordan")
        XCTAssertEqual(loaded.target.address, "123 Harbor View Dr")
        XCTAssertEqual(loaded.target.appointmentIds, [20, 21])
        XCTAssertEqual(loaded.target.greeting, "Jordan")
        XCTAssertEqual(loaded.stopDate, "2026-11-13", "the day it was filmed, which is the day it is sent for")
        XCTAssertEqual(DraftRule.filmedText(stopDate: loaded.stopDate), "Filmed Friday, Nov 13")
        // The stop as it was, for the two-keys check: its pin and check-in.
        let kept = try XCTUnwrap(loaded.target.stop)
        XCTAssertEqual(kept.coordinate, Coordinate(lat: 38.61, lng: -121.31))
        XCTAssertEqual(kept.checkIn, checkIn)
        XCTAssertEqual(kept.checkOut, checkIn.addingTimeInterval(900))
        XCTAssertEqual(kept.lastName, "Quillfeather")

        // The check a day later, from the draft alone, still passes: the
        // name was said, the phone was on the lawn, inside the check-in.
        let outcome = TwoKeys.signals(
            transcript: "Hi Jordan. Here's what I did.", fix: loaded.location, recordedAt: loaded.recordedAt,
            stop: kept, all: [kept], farMeters: 150
        )
        XCTAssertEqual(outcome.signals, Signals(spoken: .match, gps: .match, checkIn: .match))
        XCTAssertTrue(TwoKeys.verdict(outcome.signals, stop: kept).clean)
        // And still warns when it should: another name, far from the pin.
        let wrong = TwoKeys.signals(
            transcript: "Hi Avery.", fix: VideoLocation(lat: 38.70, lng: -121.40, accuracyM: 8), recordedAt: loaded.recordedAt,
            stop: kept, all: [kept], farMeters: 150
        )
        XCTAssertFalse(TwoKeys.verdict(wrong.signals, stop: kept).clean)
    }

    func testADraftLeftByAnOlderBuildStillLoads() throws {
        // Before the draft carried its stop or the name to say: neither key
        // is in the file. It must still load, or the clip would be lost.
        let made = try makeStore()
        try Data("x".utf8).write(to: made.videos.appendingPathComponent("old.mp4"))
        let json = """
        {"id": "old", "fileName": "old.mp4", "stopDate": "2026-11-12", "recordedAt": "2026-11-12T18:40:02Z",
         "durationSec": 14.6, "sizeBytes": 5, "location": null, "createdAt": "2026-11-12T18:40:20Z",
         "target": {"customerId": 2, "name": "Quillfeather, Jordan", "address": "123 Harbor View Dr",
                    "cityLine": "Sampletown, CA 90000", "appointmentIds": [20, 21], "notOnMyList": false}}
        """
        let folder = made.videos.deletingLastPathComponent()
        try Data(json.utf8).write(to: folder.appendingPathComponent("draft.json"))

        let loaded = try XCTUnwrap(made.store.loadDraft(), "an older build's draft must still load")
        XCTAssertEqual(loaded.id, "old")
        XCTAssertEqual(loaded.target.name, "Quillfeather, Jordan")
        XCTAssertEqual(loaded.target.appointmentIds, [20, 21])
        XCTAssertNil(loaded.target.stop)
        XCTAssertNil(loaded.target.greeting)
        XCTAssertEqual(DraftRule.standing(stopDate: loaded.stopDate, today: "2026-11-13"), .earlier(daysOld: 1))
    }

    func testAStopNotOnTheListCarriesNoStop() {
        XCTAssertNil(RecordTarget.notOnMyList.stop)
        XCTAssertNil(RecordTarget.notOnMyList.customerId)
        XCTAssertTrue(RecordTarget.notOnMyList.notOnMyList)
    }
}
