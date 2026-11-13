import XCTest
@testable import ZestCore

/// One video per tech, per property, per day. Two techs at one property
/// each send their own; neither can change the other's.
final class StopCoverTests: XCTestCase {
    private let now = Fixtures.base

    /// One stop, as `/v1/today` sends it, with this `video` value.
    private func stop(video json: String) throws -> Stop {
        let text = """
        {"customerId": 2, "name": "Jordan Quillfeather", "lastName": "Quillfeather", "address": "123 Harbor View Dr",
         "cityLine": "Sampletown, CA 90000", "lat": 38.61, "lng": -121.31, "appointmentIds": [20, 21],
         "services": ["Mowing"], "order": 2, "checkIn": null, "checkOut": null, "done": false, "video": \(json)}
        """
        return try ZestJSON.decoder().decode(Stop.self, from: Data(text.utf8))
    }

    private func own(_ id: String, _ state: VideoState, customer: Int? = 2, emailIn minutes: Double? = 8) -> TodayVideo {
        TodayVideo(id: id, customerId: customer, state: state, emailAt: minutes.map { Fixtures.at($0) })
    }

    // MARK: The lead (he has his own appointment at the stop)

    func testTheLeadIsShownNoVideoWhereAnotherTechRecorded() throws {
        // The server keeps the other tech's video to itself: "video" is null.
        let stop = try stop(video: "null")
        let cover = StopCover.of(stop)
        XCTAssertEqual(cover, .open)
        XCTAssertFalse(cover.isCovered)
        XCTAssertNil(cover.video)
        let standing = cover.standing(now: now)
        XCTAssertEqual(standing, .record)
        XCTAssertTrue(standing.canRecordNew)
        XCTAssertFalse(standing.canRecordAgain)
        XCTAssertFalse(standing.canDelete)
        // And it is still his to record: the list suggests it.
        let ranked = StopRanker.rank(stops: [stop], fix: nil)
        XCTAssertEqual(ranked.suggested.map { $0.stop.customerId }, [2])
    }

    func testHisOwnVideoInsideTheTenMinutesCanBeChanged() throws {
        let stop = try stop(video: #"{"id": "m1", "state": "held", "byYou": true}"#)
        let cover = StopCover.of(stop, ownVideos: [own("m1", .held, emailIn: 8)])
        XCTAssertTrue(cover.isMine)
        XCTAssertEqual(cover.standing(now: now), .change(closesAt: Fixtures.at(8)))
        XCTAssertTrue(cover.standing(now: now).canRecordAgain, "recording again replaces his own")
        XCTAssertTrue(cover.standing(now: now).canDelete)
        XCTAssertFalse(cover.standing(now: now).canRecordNew)
        // The ten minutes run out.
        XCTAssertEqual(cover.standing(now: Fixtures.at(8.1)), .locked)
        // Still going up counts as not out yet.
        let rising = try self.stop(video: #"{"id": "m1", "state": "awaiting_upload", "byYou": true}"#)
        XCTAssertEqual(StopCover.of(rising, ownVideos: [own("m1", .awaitingUpload)]).standing(now: now), .change(closesAt: Fixtures.at(8)))
    }

    func testHisOwnVideoThatWentOutIsLocked() throws {
        for state in ["sent", "watched", "no_email", "unmatched", "some_new_state"] {
            let stop = try stop(video: #"{"id": "m1", "state": "\#(state)", "byYou": true}"#)
            let standing = StopCover.of(stop, ownVideos: [own("m1", stop.video!.state, emailIn: -30)]).standing(now: now)
            XCTAssertEqual(standing, .locked, state)
            XCTAssertFalse(standing.canRecordNew, "\(state): the server would answer 409 visit_has_video")
            XCTAssertFalse(standing.canRecordAgain, state)
            XCTAssertFalse(standing.canDelete, state)
        }
        // With no word on when it goes out, nothing is offered either.
        let unknown = try stop(video: #"{"id": "m1", "state": "held", "byYou": true}"#)
        XCTAssertEqual(StopCover.of(unknown).standing(now: now), .locked)
    }

    // MARK: The helper (no appointment of his own at the stop)

    func testAHelperSeesTheTeamsVideoAndCanStillRecordHisOwn() throws {
        // Whatever became of the lead's video: going up, held, sent, watched.
        for state in ["awaiting_upload", "held", "sent", "watched", "no_email", "some_new_state"] {
            let stop = try stop(video: #"{"id": "t1", "state": "\#(state)", "byYou": false}"#)
            let cover = StopCover.of(stop)
            XCTAssertEqual(cover, .team(stop.video!), state)
            XCTAssertTrue(cover.isCovered, state)
            XCTAssertFalse(cover.isMine, state)
            for moment in [now, Fixtures.at(-60), Fixtures.at(600)] {
                let standing = cover.standing(now: moment)
                XCTAssertEqual(standing, .teamCovered, state)
                XCTAssertTrue(standing.canRecordNew, "\(state): his own is a new video; the customer gets both")
                XCTAssertFalse(standing.canRecordAgain, "\(state): nothing of the lead's is replaced")
                XCTAssertFalse(standing.canDelete, "\(state): the lead's video is not his to delete")
            }
        }
    }

    func testAVideoWithNoWordOnWhoseItIsIsNeverTreatedAsHis() throws {
        // "byYou" missing: the safe reading is "not mine".
        let stop = try stop(video: #"{"id": "t1", "state": "held"}"#)
        let standing = StopCover.of(stop).standing(now: now)
        XCTAssertEqual(standing, .teamCovered)
        XCTAssertFalse(standing.canDelete)
        XCTAssertFalse(standing.canRecordAgain)
    }

    func testAnotherTechsVideoIsNeverChangeableEvenIfItsIdIsListedAsOwn() throws {
        // A muddled answer must not hand out DELETE on somebody else's video
        // for a different customer.
        let stop = try stop(video: #"{"id": "t1", "state": "held", "byYou": false}"#)
        let cover = StopCover.of(stop, ownVideos: [own("m9", .held, customer: 5)])
        XCTAssertEqual(cover.standing(now: now), .teamCovered)
    }

    func testATeamCoveredStopReadsAsCoveredOnTheList() throws {
        var teamStop = try stop(video: #"{"id": "t1", "state": "sent", "byYou": false}"#)
        teamStop.order = 1
        let route = [teamStop, Fixtures.maple, Fixtures.ostrander]
        let ranked = StopRanker.rank(stops: route, fix: nil)
        XCTAssertFalse(ranked.suggested.contains { $0.stop.customerId == 2 }, "a covered stop is not what to record next")
        XCTAssertEqual(ranked.all.last?.stop.customerId, 2, "it stays on the list, after the stops that still need a video")
        XCTAssertEqual(ranked.all.count, 3)
    }

    func testATeamVideoThatIsGoneLeavesTheStopOpen() throws {
        for state in ["pulled", "deleted", "replaced"] {
            let stop = try stop(video: #"{"id": "t1", "state": "\#(state)", "byYou": false}"#)
            XCTAssertEqual(StopCover.of(stop), .open, state)
        }
    }

    // MARK: A helper who recorded his own too

    func testOnceTheHelperHasHisOwnTheStopIsHis() throws {
        // The server shows his own video for the stop.
        let mine = try stop(video: #"{"id": "m1", "state": "held", "byYou": true}"#)
        XCTAssertEqual(StopCover.of(mine, ownVideos: [own("m1", .held)]).standing(now: now), .change(closesAt: Fixtures.at(8)))
        // Even if the stop still showed the lead's, his own (listed among his
        // videos for that customer) is the one that counts for him.
        let stillTeam = try stop(video: #"{"id": "t1", "state": "sent", "byYou": false}"#)
        let cover = StopCover.of(stillTeam, ownVideos: [own("m1", .held)])
        XCTAssertEqual(cover, .mine(StopVideo(id: "m1", state: .held, byYou: true), emailAt: Fixtures.at(8)))
        XCTAssertEqual(cover.standing(now: now), .change(closesAt: Fixtures.at(8)), "recording again replaces his own, not the lead's")
        XCTAssertEqual(cover.standing(now: Fixtures.at(9)), .locked)
        // His own that is gone (deleted, or replaced by a newer one) does not count.
        XCTAssertEqual(StopCover.of(stillTeam, ownVideos: [own("m0", .deleted)]), .team(stillTeam.video!))
        XCTAssertEqual(
            StopCover.of(stillTeam, ownVideos: [own("m0", .replaced), own("m1", .sent, emailIn: -5)]).standing(now: now), .locked
        )
        // Nor does his video for another customer, or one for no customer.
        XCTAssertEqual(StopCover.of(stillTeam, ownVideos: [own("m7", .held, customer: 3), own("m8", .held, customer: nil)]), .team(stillTeam.video!))
    }

    // MARK: A helper deletes his own video: the lead's covers the stop again

    private func routeStop(_ id: Int, video: StopVideo?) -> Stop {
        Fixtures.stop(id, "Customer \(id)", last: "Sample\(id)", "\(id) Test Rd", order: id, video: video)
    }

    /// What happened in the UI test that found this. The server shows a
    /// helper the lead's video only while he has none of his own. He
    /// records his own, then deletes it: the last answer the phone holds
    /// shows his own (now deleted) video and says nothing of the lead's.
    func testTheLeadsVideoIsRememberedWhileTheHelperHasHisOwn() {
        let leads = StopVideo(id: "lead", state: .sent, byYou: false)
        let mine = StopVideo(id: "mine", state: .held, byYou: true)

        // 1. The first answer of the day: the lead's video covers stop 2.
        var memory = TeamVideoMemory.after([routeStop(1, video: nil), routeStop(2, video: leads)], remembered: [:])
        XCTAssertEqual(memory, [2: leads])
        // 2. He records his own: the server now shows his own for that stop.
        memory = TeamVideoMemory.after([routeStop(1, video: nil), routeStop(2, video: mine)], remembered: memory)
        XCTAssertEqual(memory, [2: leads], "the lead's video is still there, only out of his sight")
        // 3. He deletes his own. Before the server has answered again (or
        //    with no signal at all), the stop is covered by the lead's.
        XCTAssertEqual(TeamVideoMemory.whenOwnVideoIsGone(customerId: 2, remembered: memory), leads)
        let shown = routeStop(2, video: TeamVideoMemory.whenOwnVideoIsGone(customerId: 2, remembered: memory))
        XCTAssertEqual(StopCover.of(shown).standing(now: now), .teamCovered, "DONE BY TEAM again, and still open to record")
        // 4. The server answers: the lead's video, as before.
        memory = TeamVideoMemory.after([routeStop(1, video: nil), routeStop(2, video: leads)], remembered: memory)
        XCTAssertEqual(memory, [2: leads])
    }

    func testAStopNobodyElseRecordedSimplyReopens() {
        // The lead's own case: there never was a teammate's video for it.
        let mine = StopVideo(id: "mine", state: .held, byYou: true)
        let memory = TeamVideoMemory.after([routeStop(2, video: mine)], remembered: [:])
        XCTAssertEqual(memory, [:])
        XCTAssertNil(TeamVideoMemory.whenOwnVideoIsGone(customerId: 2, remembered: memory))
        XCTAssertEqual(StopCover.of(routeStop(2, video: nil)).standing(now: now), .record, "open: RECORD VIDEO")
    }

    func testWhatIsRememberedFollowsTheServer() {
        let leads = StopVideo(id: "lead", state: .held, byYou: false)
        let mine = StopVideo(id: "mine", state: .held, byYou: true)
        var memory = TeamVideoMemory.after([routeStop(2, video: leads), routeStop(3, video: leads)], remembered: [:])
        XCTAssertEqual(Set(memory.keys), [2, 3])
        // The server says stop 2 has no video at all now (the lead deleted
        // his): forgotten. Open is the safe way to be wrong.
        memory = TeamVideoMemory.after([routeStop(2, video: nil), routeStop(3, video: mine)], remembered: memory)
        XCTAssertEqual(Set(memory.keys), [3])
        XCTAssertNil(TeamVideoMemory.whenOwnVideoIsGone(customerId: 2, remembered: memory))
        // A stop that is no longer on the list is forgotten too.
        memory = TeamVideoMemory.after([routeStop(2, video: nil)], remembered: memory)
        XCTAssertEqual(memory, [:])
        // A teammate's video that was pulled or replaced does not count.
        let pulled = StopVideo(id: "lead", state: .pulled, byYou: false)
        XCTAssertEqual(TeamVideoMemory.after([routeStop(2, video: pulled)], remembered: [2: leads]), [:])
        XCTAssertNil(TeamVideoMemory.whenOwnVideoIsGone(customerId: 2, remembered: [2: pulled]))
        // His own video is never remembered as a teammate's.
        XCTAssertNil(TeamVideoMemory.whenOwnVideoIsGone(customerId: 2, remembered: [2: mine]))
        // The state the server last gave for the teammate's video is kept.
        let sent = StopVideo(id: "lead", state: .sent, byYou: false)
        XCTAssertEqual(TeamVideoMemory.after([routeStop(2, video: sent)], remembered: [2: leads]), [2: sent])
    }

    // MARK: The server's refusal is only ever about his own video

    func testALockedStopsRefusalNamesHisOwnVideo() throws {
        let body = #"{"error":"visit_has_video","message":"This video already went to the customer. Call the office to change it.","existing":{"id":"m1","state":"sent","byYou":true,"techFirstName":"Sample"}}"#
        let error = try ZestJSON.decoder().decode(APIErrorBody.self, from: Data(body.utf8))
        XCTAssertEqual(error.error, "visit_has_video")
        XCTAssertEqual(error.existing?.byYou, true)
        XCTAssertEqual(error.existing?.id, "m1")
    }
}
