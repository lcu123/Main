import XCTest
@testable import ZestCore

final class APIModelsTests: XCTestCase {
    func testDecodesTodayExactlyAsTheContractShows() throws {
        let today = try ZestJSON.decoder().decode(TodayResponse.self, from: Data(Fixtures.todayJSON.utf8))
        XCTAssertEqual(today.date, "2026-11-13")
        XCTAssertEqual(today.tech, TechInfo(id: 42, firstName: "Sample", demo: false))
        XCTAssertEqual(today.config.maxSeconds, 15)
        XCTAssertEqual(today.config.holdMinutes, 10)
        XCTAssertEqual(today.config.farMeters, 150)
        XCTAssertEqual(today.config.prompts.count, 4)
        XCTAssertEqual(today.config.officePhone, "+15555550100")
        XCTAssertTrue(today.config.leaveReminders)
        XCTAssertEqual(today.stops.count, 2)

        let first = today.stops[0]
        XCTAssertEqual(first.customerId, 2)
        XCTAssertEqual(first.appointmentIds, [20, 21, 22])
        XCTAssertEqual(first.checkIn, ZestDates.parseISO("2026-11-13T18:32:56Z"))
        XCTAssertNil(first.checkOut)
        XCTAssertTrue(first.isCheckedInNow)
        XCTAssertNil(first.video)
        XCTAssertNotNil(first.coordinate)

        let second = today.stops[1]
        XCTAssertNil(second.coordinate)
        XCTAssertEqual(second.lastName, "")
        XCTAssertEqual(second.video, StopVideo(id: "aaaa", state: .sent, byYou: true))
        XCTAssertEqual(today.videos, [TodayVideo(id: "aaaa", customerId: 5, state: .held, emailAt: ZestDates.parseISO("2026-11-13T18:50:00Z"))])
    }

    func testTodaySurvivesADiskRoundTrip() throws {
        let today = try ZestJSON.decoder().decode(TodayResponse.self, from: Data(Fixtures.todayJSON.utf8))
        let data = try ZestJSON.encoder().encode(today)
        XCTAssertEqual(try ZestJSON.decoder().decode(TodayResponse.self, from: data), today)
    }

    func testMissingConfigFieldsFallBackInsteadOfLosingTheRoute() throws {
        let json = #"{"date":"2026-11-13","tech":{"id":1,"firstName":"A"},"config":{"maxSeconds":20},"stops":[{"customerId":9}]}"#
        let today = try ZestJSON.decoder().decode(TodayResponse.self, from: Data(json.utf8))
        XCTAssertEqual(today.config.maxSeconds, 20)
        XCTAssertEqual(today.config.holdMinutes, 10)
        XCTAssertEqual(today.config.prompts, AppConfig.defaultPrompts)
        XCTAssertFalse(today.config.leaveReminders)
        XCTAssertFalse(today.tech.demo)
        XCTAssertEqual(today.stops[0].name, "")
        XCTAssertEqual(today.videos, [])
        XCTAssertNil(today.serverTime)
    }

    func testConfigIsClampedToSafeValues() throws {
        let json = #"{"maxSeconds":0,"holdMinutes":-5,"farMeters":1,"prompts":[]}"#
        let config = try ZestJSON.decoder().decode(AppConfig.self, from: Data(json.utf8))
        XCTAssertEqual(config.maxSeconds, 5)
        XCTAssertEqual(config.holdMinutes, 0)
        XCTAssertEqual(config.farMeters, 25)
        XCTAssertEqual(config.prompts, AppConfig.defaultPrompts)
    }

    func testUnknownVideoStateDoesNotBreakDecoding() throws {
        let video = try ZestJSON.decoder().decode(TodayVideo.self, from: Data(#"{"id":"x","customerId":null,"state":"archived"}"#.utf8))
        XCTAssertEqual(video.state, .unknown)
        XCTAssertTrue(video.state.serverHasFile)
        XCTAssertFalse(video.state.isGone)
    }

    func testVideoStateRawValuesMatchTheContract() {
        XCTAssertEqual(VideoState.awaitingUpload.rawValue, "awaiting_upload")
        XCTAssertEqual(VideoState.noEmail.rawValue, "no_email")
        for gone in [VideoState.pulled, .deleted, .replaced] {
            XCTAssertTrue(gone.isGone)
            XCTAssertFalse(gone.serverHasFile)
        }
        for held in [VideoState.held, .sent, .watched, .noEmail, .unmatched] {
            XCTAssertTrue(held.serverHasFile)
        }
        XCTAssertFalse(VideoState.awaitingUpload.serverHasFile)
    }

    func testRegisterRequestMatchesTheContractKeys() throws {
        let request = Fixtures.item().registerRequest
        let data = try ZestJSON.encoder().encode(request)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(
            Set(object.keys),
            ["id", "stopDate", "customerId", "appointmentIds", "notOnMyList", "recordedAt", "durationSec",
             "sizeBytes", "location", "signals", "override", "recommend", "spokenText"]
        )
        XCTAssertNil(object["replace"], "the replace field was removed from the contract")
        XCTAssertEqual(object["recordedAt"] as? String, "2026-11-13T18:00:00Z")
        XCTAssertEqual(object["durationSec"] as? Double, 14.6)
        XCTAssertEqual(object["sizeBytes"] as? Int, 5_012_345)
        XCTAssertEqual(object["customerId"] as? Int, 2)
        let signals = try XCTUnwrap(object["signals"] as? [String: String])
        XCTAssertEqual(signals, ["spoken": "match", "gps": "match", "checkIn": "unknown"])
        let location = try XCTUnwrap(object["location"] as? [String: Double])
        XCTAssertEqual(location["accuracyM"], 8)
    }

    func testNotOnMyListSendsNullCustomerAndNoAppointments() throws {
        var item = Fixtures.item(notOnMyList: true)
        item.location = nil
        item.spokenText = nil
        let data = try ZestJSON.encoder().encode(item.registerRequest)
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertTrue(object["customerId"] is NSNull)
        XCTAssertTrue(object["location"] is NSNull)
        XCTAssertTrue(object["spokenText"] is NSNull)
        XCTAssertEqual(object["appointmentIds"] as? [Int], [])
        XCTAssertEqual(object["notOnMyList"] as? Bool, true)
    }

    func testDecodesRegisterResponses() throws {
        let fresh = #"{"id":"v1","state":"awaiting_upload","upload":{"method":"PUT","url":"https://bucket.example.com/v1.mp4?sig=1","headers":{"Content-Type":"video/mp4"},"expiresAt":"2026-11-14T18:40:02Z"}}"#
        let a = try ZestJSON.decoder().decode(RegisterVideoResponse.self, from: Data(fresh.utf8))
        XCTAssertEqual(a.state, .awaitingUpload)
        XCTAssertEqual(a.upload?.url.host, "bucket.example.com")
        XCTAssertEqual(a.upload?.headers, ["Content-Type": "video/mp4"])
        XCTAssertNotNil(a.upload?.expiresAt)

        let repeatAnswer = #"{"id":"v1","state":"held","upload":null}"#
        let b = try ZestJSON.decoder().decode(RegisterVideoResponse.self, from: Data(repeatAnswer.utf8))
        XCTAssertEqual(b.state, .held)
        XCTAssertNil(b.upload)

        let broken = #"{"id":"v1","state":"awaiting_upload","upload":{"method":"PUT"}}"#
        XCTAssertThrowsError(try ZestJSON.decoder().decode(RegisterVideoResponse.self, from: Data(broken.utf8)))
    }

    func testDecodesErrorBodies() throws {
        let locked = #"{"error":"visit_has_video","message":"Call the office.","existing":{"id":"v0","state":"sent","byYou":true,"techFirstName":"Sample"}}"#
        let a = try ZestJSON.decoder().decode(APIErrorBody.self, from: Data(locked.utf8))
        XCTAssertEqual(a.error, "visit_has_video")
        XCTAssertEqual(a.existing?.state, .sent)
        XCTAssertEqual(a.existing?.byYou, true)

        let badCode = #"{"error":"bad_code","message":"Wrong code.","triesLeft":3}"#
        XCTAssertEqual(try ZestJSON.decoder().decode(APIErrorBody.self, from: Data(badCode.utf8)).triesLeft, 3)

        let update = #"{"error":"update_required","message":"Update.","minBuild":7}"#
        XCTAssertEqual(try ZestJSON.decoder().decode(APIErrorBody.self, from: Data(update.utf8)).minBuild, 7)

        let junk = try ZestJSON.decoder().decode(APIErrorBody.self, from: Data("{}".utf8))
        XCTAssertEqual(junk.error, "server_error")
    }

    func testAuditEventWireNames() throws {
        let event = AuditEvent(type: .twoKeysWarning, at: Fixtures.base, videoId: nil, customerId: 2, detail: ["spoken": "mismatch"])
        let data = try ZestJSON.encoder().encode(AuditBatch(events: [event]))
        let text = try XCTUnwrap(String(data: data, encoding: .utf8))
        XCTAssertTrue(text.contains("\"two_keys_warning\""))
        XCTAssertTrue(text.contains("\"videoId\":null"))
        XCTAssertEqual(AuditEventType.recordStart.rawValue, "record_start")
        XCTAssertEqual(AuditEventType.draftDiscarded.rawValue, "draft_discarded")
        XCTAssertEqual(AuditEventType.stopSwitched.rawValue, "stop_switched")
    }

    func testDates() {
        XCTAssertEqual(ZestDates.formatISO(Fixtures.base), "2026-11-13T18:00:00Z")
        XCTAssertNotNil(ZestDates.parseISO("2026-11-13T18:00:00.250Z"))
        XCTAssertNil(ZestDates.parseISO("yesterday"))
        let pacific = TimeZone(identifier: "America/Los_Angeles")!
        // 18:00 UTC is 10:00 in California: same calendar day.
        XCTAssertEqual(ZestDates.dayString(Fixtures.base, timeZone: pacific), "2026-11-13")
        // 03:00 UTC on the 14th is still the evening of the 13th there.
        let late = ZestDates.parseISO("2026-11-14T03:00:00Z")!
        XCTAssertEqual(ZestDates.dayString(late, timeZone: pacific), "2026-11-13")
        XCTAssertTrue(ZestDates.isToday("2026-11-13", now: late, timeZone: pacific))
        XCTAssertFalse(ZestDates.isToday("2026-11-12", now: late, timeZone: pacific))
    }
}
