import XCTest
import ZestCore
@testable import ZestVideo

/// The app's coders against bodies shaped exactly like docs/video/API.md.
final class APIDecodingTests: XCTestCase {
    private let todayJSON = """
    {
      "date": "2026-11-13",
      "serverTime": "2026-11-13T15:04:05Z",
      "tech": {"id": 42, "firstName": "Sample", "demo": false},
      "config": {
        "maxSeconds": 15, "holdMinutes": 10, "farMeters": 150, "minBuild": 1,
        "requireVideo": true, "recommendButton": true, "leaveReminders": true,
        "prompts": ["Hi {name}", "Here's what I did", "Here's what I recommend", "I closed the backyard gate"],
        "officePhone": "+15555550100"
      },
      "stops": [
        {
          "customerId": 2, "name": "Jordan Quillfeather", "lastName": "Quillfeather",
          "address": "123 Harbor View Dr", "cityLine": "Sampletown, CA 90000",
          "lat": 38.61, "lng": -121.31, "appointmentIds": [20, 21], "services": ["Mowing"],
          "order": 1, "checkIn": "2026-11-13T18:32:56Z", "checkOut": null, "done": false, "video": null
        }
      ],
      "videos": [
        {"id": "aaaa", "customerId": 2, "state": "held", "emailAt": "2026-11-13T18:50:00Z"}
      ]
    }
    """

    func testDecodesToday() throws {
        let today = try ZestJSONBridge.decoder.decode(TodayResponse.self, from: Data(todayJSON.utf8))
        XCTAssertEqual(today.date, "2026-11-13")
        XCTAssertEqual(today.config.maxSeconds, 15)
        XCTAssertEqual(today.stops.first?.appointmentIds, [20, 21])
        XCTAssertNotNil(today.stops.first?.checkIn)
        XCTAssertEqual(today.videos.first?.state, .held)
    }

    func testTodayStoreKeepsOnlyTodaysRoute() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("zest-today-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: directory) }

        var today = try ZestJSONBridge.decoder.decode(TodayResponse.self, from: Data(todayJSON.utf8))
        today.date = ZestDates.dayString(Date())
        let store = TodayStore(directory: directory)
        store.save(today)
        XCTAssertEqual(store.load()?.stops.count, 1)

        // The same file read tomorrow is wiped, not shown.
        XCTAssertNil(store.load(now: Date().addingTimeInterval(36 * 60 * 60)))
        XCTAssertNil(store.load(), "the stale route was deleted from disk")
    }

    func testRegisterRequestGoesOutAsTheContractShows() throws {
        let item = QueueItem(
            id: "0b0e7c1e-7c35-4f0b-9c50-0f7f6f0f3f11", stopDate: "2026-11-13", customerId: 2,
            customerName: "Jordan Quillfeather", appointmentIds: [20, 21], notOnMyList: false,
            recordedAt: Date(timeIntervalSince1970: 1_794_595_202), durationSec: 14.6, sizeBytes: 5_012_345,
            location: VideoLocation(lat: 38.61, lng: -121.31, accuracyM: 8),
            signals: Signals(spoken: .match, gps: .match, checkIn: .unknown), override: false, recommend: true,
            spokenText: "This is the Quillfeather home", fileName: "x.mp4"
        )
        let data = try ZestJSONBridge.encoder.encode(item.registerRequest)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(object["id"] as? String, item.id)
        XCTAssertEqual(object["recommend"] as? Bool, true)
        XCTAssertNil(object["replace"])
        XCTAssertNotNil(object["recordedAt"] as? String)
        XCTAssertEqual((object["signals"] as? [String: String])?["checkIn"], "unknown")
    }

    func testErrorMessagesAreInPlainWords() {
        XCTAssertFalse(APIError.offline("x").message.isEmpty)
        XCTAssertTrue(APIError.offline("x").isTransient)
        XCTAssertFalse(APIError.unauthorized.isTransient)
        let locked = APIError.http(status: 409, body: APIErrorBody(error: "visit_has_video", message: "Call the office."), retryAfter: nil)
        XCTAssertEqual(locked.code, "visit_has_video")
        XCTAssertEqual(locked.message, "Call the office.")
        XCTAssertFalse(locked.isTransient)
        let busy = APIError.http(status: 429, body: APIErrorBody(error: "slow_down", message: ""), retryAfter: 30)
        XCTAssertTrue(busy.isTransient)
        XCTAssertEqual(busy.retryAfter, 30)
        XCTAssertFalse(busy.message.isEmpty)
    }
}
