import Foundation

// Wire models for docs/video/API.md. Keys are camelCase on the wire, so the
// property names here ARE the contract: rename one and the app stops talking
// to the server.

// MARK: - Shared

/// Where a video is on the server.
public enum VideoState: String, Codable, Equatable, Sendable {
    case awaitingUpload = "awaiting_upload"
    case held
    case sent
    case watched
    case noEmail = "no_email"
    case unmatched
    case pulled
    case deleted
    case replaced
    /// A state this build does not know. Treated as "the server has it".
    case unknown

    public init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = VideoState(rawValue: raw) ?? .unknown
    }

    /// Pulled, deleted or replaced: the video no longer counts for its stop.
    public var isGone: Bool {
        self == .pulled || self == .deleted || self == .replaced
    }

    /// The server has confirmed the whole file. The phone's copy can go.
    public var serverHasFile: Bool {
        switch self {
        case .held, .sent, .watched, .noEmail, .unmatched, .unknown: return true
        case .awaitingUpload, .pulled, .deleted, .replaced: return false
        }
    }
}

public struct TechInfo: Codable, Equatable, Sendable {
    public var id: Int
    public var firstName: String
    public var demo: Bool

    public init(id: Int, firstName: String, demo: Bool = false) {
        self.id = id
        self.firstName = firstName
        self.demo = demo
    }

    enum CodingKeys: String, CodingKey { case id, firstName, demo }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        firstName = c.value(.firstName, default: "")
        demo = c.value(.demo, default: false)
    }
}

/// Body of any non-2xx answer.
public struct APIErrorBody: Codable, Equatable, Sendable {
    public var error: String
    public var message: String
    public var triesLeft: Int?
    public var minBuild: Int?
    public var existing: ExistingVideo?

    public struct ExistingVideo: Codable, Equatable, Sendable {
        public var id: String
        public var state: VideoState
        public var byYou: Bool
        public var techFirstName: String?

        enum CodingKeys: String, CodingKey { case id, state, byYou, techFirstName }

        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            id = c.value(.id, default: "")
            state = c.value(.state, default: .unknown)
            byYou = c.value(.byYou, default: false)
            techFirstName = c.value(.techFirstName, default: nil)
        }
    }

    public init(error: String, message: String) {
        self.error = error
        self.message = message
    }

    enum CodingKeys: String, CodingKey { case error, message, triesLeft, minBuild, existing }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        error = c.value(.error, default: "server_error")
        message = c.value(.message, default: "")
        triesLeft = c.value(.triesLeft, default: nil)
        minBuild = c.value(.minBuild, default: nil)
        existing = c.value(.existing, default: nil)
    }
}

public struct OKResponse: Codable, Equatable, Sendable {
    public var ok: Bool
    public init(ok: Bool) { self.ok = ok }
}

// MARK: - Login

public struct RequestCodeRequest: Codable, Equatable, Sendable {
    public var contact: String
    public init(contact: String) { self.contact = contact }
}

public struct VerifyRequest: Codable, Equatable, Sendable {
    public var contact: String
    public var code: String
    public var deviceName: String
    public var deviceModel: String
    public var osVersion: String
    public var appBuild: Int

    public init(contact: String, code: String, deviceName: String, deviceModel: String, osVersion: String, appBuild: Int) {
        self.contact = contact
        self.code = code
        self.deviceName = deviceName
        self.deviceModel = deviceModel
        self.osVersion = osVersion
        self.appBuild = appBuild
    }
}

public struct VerifyResponse: Codable, Equatable, Sendable {
    public var token: String
    public var tech: TechInfo
}

// MARK: - Today

public struct AppConfig: Codable, Equatable, Sendable {
    public var maxSeconds: Int
    public var holdMinutes: Int
    public var farMeters: Double
    public var minBuild: Int
    public var requireVideo: Bool
    public var recommendButton: Bool
    public var leaveReminders: Bool
    public var prompts: [String]
    public var officePhone: String?

    /// What the tech says, in order. "{name}" is the customer's first name
    /// (see `PromptText`).
    public static let defaultPrompts = ["Hi {name}", "Here's what I did", "Here's what I recommend", "I closed the backyard gate"]

    /// What the app uses before it has ever reached the server.
    public static let fallback = AppConfig(
        maxSeconds: 15, holdMinutes: 10, farMeters: 150, minBuild: 1, requireVideo: true,
        recommendButton: true, leaveReminders: false, prompts: defaultPrompts, officePhone: nil
    )

    public init(
        maxSeconds: Int, holdMinutes: Int, farMeters: Double, minBuild: Int, requireVideo: Bool,
        recommendButton: Bool, leaveReminders: Bool, prompts: [String], officePhone: String?
    ) {
        self.maxSeconds = maxSeconds
        self.holdMinutes = holdMinutes
        self.farMeters = farMeters
        self.minBuild = minBuild
        self.requireVideo = requireVideo
        self.recommendButton = recommendButton
        self.leaveReminders = leaveReminders
        self.prompts = prompts
        self.officePhone = officePhone
    }

    enum CodingKeys: String, CodingKey {
        case maxSeconds, holdMinutes, farMeters, minBuild, requireVideo, recommendButton, leaveReminders, prompts, officePhone
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let d = AppConfig.fallback
        // Clamp so a bad setting can never produce a zero-length or endless clip.
        maxSeconds = min(max(c.value(.maxSeconds, default: d.maxSeconds), 3), 60)
        holdMinutes = max(c.value(.holdMinutes, default: d.holdMinutes), 0)
        farMeters = max(c.value(.farMeters, default: d.farMeters), 25)
        minBuild = c.value(.minBuild, default: d.minBuild)
        requireVideo = c.value(.requireVideo, default: d.requireVideo)
        recommendButton = c.value(.recommendButton, default: d.recommendButton)
        leaveReminders = c.value(.leaveReminders, default: d.leaveReminders)
        let decoded: [String] = c.value(.prompts, default: d.prompts)
        prompts = decoded.isEmpty ? d.prompts : decoded
        officePhone = c.value(.officePhone, default: nil)
    }
}

/// The video a stop shows this tech. It is his own (`byYou`), except where
/// he has no appointment of his own at the stop: there it can be a
/// teammate's, to say the stop is covered. See `StopCover`.
public struct StopVideo: Codable, Equatable, Sendable {
    public var id: String
    public var state: VideoState
    public var byYou: Bool
    /// Present and true only while the server is holding this video for
    /// the office to decide (the visit left the day's route after it was
    /// filmed). No email has gone to anyone. An older server never sends it.
    public var withOffice: Bool?

    public init(id: String, state: VideoState, byYou: Bool, withOffice: Bool? = nil) {
        self.id = id
        self.state = state
        self.byYou = byYou
        self.withOffice = withOffice
    }

    /// The office has this video and is deciding what becomes of it.
    public var isWithOffice: Bool { withOffice == true }

    enum CodingKeys: String, CodingKey { case id, state, byYou, withOffice }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        state = c.value(.state, default: .unknown)
        byYou = c.value(.byYou, default: false)
        withOffice = c.value(.withOffice, default: nil)
    }
}

/// One customer on one day. All of that customer's appointments are folded in.
public struct Stop: Codable, Equatable, Identifiable, Sendable {
    public var customerId: Int
    public var name: String
    public var lastName: String
    public var address: String
    public var cityLine: String
    public var lat: Double?
    public var lng: Double?
    public var appointmentIds: [Int]
    public var services: [String]
    public var order: Int
    public var checkIn: Date?
    public var checkOut: Date?
    public var done: Bool
    public var video: StopVideo?

    public var id: Int { customerId }

    public var coordinate: Coordinate? {
        guard let lat, let lng else { return nil }
        return Coordinate(lat: lat, lng: lng)
    }

    /// Checked in and not yet checked out: the tech is standing on it.
    public var isCheckedInNow: Bool { checkIn != nil && checkOut == nil }

    /// A video that still counts (not pulled, deleted or replaced).
    public var liveVideo: StopVideo? {
        guard let video, !video.state.isGone else { return nil }
        return video
    }

    public init(
        customerId: Int, name: String, lastName: String = "", address: String, cityLine: String = "",
        lat: Double? = nil, lng: Double? = nil, appointmentIds: [Int] = [], services: [String] = [],
        order: Int = 0, checkIn: Date? = nil, checkOut: Date? = nil, done: Bool = false, video: StopVideo? = nil
    ) {
        self.customerId = customerId
        self.name = name
        self.lastName = lastName
        self.address = address
        self.cityLine = cityLine
        self.lat = lat
        self.lng = lng
        self.appointmentIds = appointmentIds
        self.services = services
        self.order = order
        self.checkIn = checkIn
        self.checkOut = checkOut
        self.done = done
        self.video = video
    }

    enum CodingKeys: String, CodingKey {
        case customerId, name, lastName, address, cityLine, lat, lng, appointmentIds, services, order, checkIn, checkOut, done, video
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        customerId = try c.decode(Int.self, forKey: .customerId)
        name = c.value(.name, default: "")
        lastName = c.value(.lastName, default: "")
        address = c.value(.address, default: "")
        cityLine = c.value(.cityLine, default: "")
        lat = c.value(.lat, default: nil)
        lng = c.value(.lng, default: nil)
        appointmentIds = c.value(.appointmentIds, default: [])
        services = c.value(.services, default: [])
        order = c.value(.order, default: 0)
        checkIn = c.value(.checkIn, default: nil)
        checkOut = c.value(.checkOut, default: nil)
        done = c.value(.done, default: false)
        video = c.value(.video, default: nil)
    }
}

/// One of this tech's own videos for the day.
public struct TodayVideo: Codable, Equatable, Sendable {
    public var id: String
    public var customerId: Int?
    public var state: VideoState
    public var emailAt: Date?
    /// See `StopVideo.withOffice`.
    public var withOffice: Bool?

    public init(id: String, customerId: Int?, state: VideoState, emailAt: Date?, withOffice: Bool? = nil) {
        self.id = id
        self.customerId = customerId
        self.state = state
        self.emailAt = emailAt
        self.withOffice = withOffice
    }

    /// The office has this video and is deciding what becomes of it.
    public var isWithOffice: Bool { withOffice == true }

    enum CodingKeys: String, CodingKey { case id, customerId, state, emailAt, withOffice }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        customerId = c.value(.customerId, default: nil)
        state = c.value(.state, default: .unknown)
        emailAt = c.value(.emailAt, default: nil)
        withOffice = c.value(.withOffice, default: nil)
    }
}

/// `GET /v1/today`: everything the phone holds for the day.
public struct TodayResponse: Codable, Equatable, Sendable {
    public var date: String
    public var serverTime: Date?
    public var tech: TechInfo
    public var config: AppConfig
    public var stops: [Stop]
    public var videos: [TodayVideo]

    public init(date: String, serverTime: Date?, tech: TechInfo, config: AppConfig, stops: [Stop], videos: [TodayVideo]) {
        self.date = date
        self.serverTime = serverTime
        self.tech = tech
        self.config = config
        self.stops = stops
        self.videos = videos
    }

    enum CodingKeys: String, CodingKey { case date, serverTime, tech, config, stops, videos }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        date = try c.decode(String.self, forKey: .date)
        serverTime = c.value(.serverTime, default: nil)
        tech = try c.decode(TechInfo.self, forKey: .tech)
        config = c.value(.config, default: .fallback)
        stops = c.value(.stops, default: [])
        videos = c.value(.videos, default: [])
    }
}

// MARK: - Videos

public enum SignalResult: String, Codable, Equatable, Sendable {
    case match
    case mismatch
    case unknown
}

/// The three two-keys results, as sent with the video.
public struct Signals: Codable, Equatable, Sendable {
    public var spoken: SignalResult
    public var gps: SignalResult
    public var checkIn: SignalResult

    public init(spoken: SignalResult, gps: SignalResult, checkIn: SignalResult) {
        self.spoken = spoken
        self.gps = gps
        self.checkIn = checkIn
    }

    public static let allUnknown = Signals(spoken: .unknown, gps: .unknown, checkIn: .unknown)
}

public struct VideoLocation: Codable, Equatable, Sendable {
    public var lat: Double
    public var lng: Double
    public var accuracyM: Double

    public init(lat: Double, lng: Double, accuracyM: Double) {
        self.lat = lat
        self.lng = lng
        self.accuracyM = accuracyM
    }

    public var coordinate: Coordinate { Coordinate(lat: lat, lng: lng) }
}

/// `POST /v1/videos`.
public struct RegisterVideoRequest: Codable, Equatable, Sendable {
    public var id: String
    public var stopDate: String
    public var customerId: Int?
    public var appointmentIds: [Int]
    public var notOnMyList: Bool
    public var recordedAt: Date
    public var durationSec: Double
    public var sizeBytes: Int64
    public var location: VideoLocation?
    public var signals: Signals
    public var override: Bool
    public var recommend: Bool
    public var spokenText: String?

    public init(
        id: String, stopDate: String, customerId: Int?, appointmentIds: [Int], notOnMyList: Bool,
        recordedAt: Date, durationSec: Double, sizeBytes: Int64, location: VideoLocation?, signals: Signals,
        override: Bool, recommend: Bool, spokenText: String?
    ) {
        self.id = id
        self.stopDate = stopDate
        self.customerId = customerId
        self.appointmentIds = appointmentIds
        self.notOnMyList = notOnMyList
        self.recordedAt = recordedAt
        self.durationSec = durationSec
        self.sizeBytes = sizeBytes
        self.location = location
        self.signals = signals
        self.override = override
        self.recommend = recommend
        self.spokenText = spokenText
    }

    enum CodingKeys: String, CodingKey {
        case id, stopDate, customerId, appointmentIds, notOnMyList, recordedAt, durationSec, sizeBytes
        case location, signals, override, recommend, spokenText
    }

    /// Written by hand so that the optional fields go out as explicit `null`,
    /// exactly as the contract shows them, instead of being left out.
    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(id, forKey: .id)
        try c.encode(stopDate, forKey: .stopDate)
        try c.encode(customerId, forKey: .customerId)
        try c.encode(appointmentIds, forKey: .appointmentIds)
        try c.encode(notOnMyList, forKey: .notOnMyList)
        try c.encode(recordedAt, forKey: .recordedAt)
        try c.encode((durationSec * 10).rounded() / 10, forKey: .durationSec)
        try c.encode(sizeBytes, forKey: .sizeBytes)
        try c.encode(location, forKey: .location)
        try c.encode(signals, forKey: .signals)
        try c.encode(override, forKey: .override)
        try c.encode(recommend, forKey: .recommend)
        try c.encode(spokenText, forKey: .spokenText)
    }
}

public struct UploadTicket: Codable, Equatable, Sendable {
    public var method: String
    public var url: URL
    public var headers: [String: String]
    public var expiresAt: Date?

    public init(method: String = "PUT", url: URL, headers: [String: String], expiresAt: Date?) {
        self.method = method
        self.url = url
        self.headers = headers
        self.expiresAt = expiresAt
    }

    enum CodingKeys: String, CodingKey { case method, url, headers, expiresAt }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        method = c.value(.method, default: "PUT")
        url = try c.decode(URL.self, forKey: .url)
        headers = c.value(.headers, default: ["Content-Type": "video/mp4"])
        expiresAt = c.value(.expiresAt, default: nil)
    }
}

public struct RegisterVideoResponse: Codable, Equatable, Sendable {
    public var id: String
    public var state: VideoState
    public var upload: UploadTicket?
    public var emailAt: Date?

    enum CodingKeys: String, CodingKey { case id, state, upload, emailAt }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, default: "")
        state = c.value(.state, default: .unknown)
        // A broken `upload` must fail loudly, not look like "already sent".
        upload = try c.decodeIfPresent(UploadTicket.self, forKey: .upload)
        emailAt = c.value(.emailAt, default: nil)
    }
}

public struct CompleteVideoRequest: Codable, Equatable, Sendable {
    public var sizeBytes: Int64
    public init(sizeBytes: Int64) { self.sizeBytes = sizeBytes }
}

/// Answer to `complete` and to `DELETE`.
public struct VideoStatusResponse: Codable, Equatable, Sendable {
    public var id: String
    public var state: VideoState
    public var emailAt: Date?

    enum CodingKeys: String, CodingKey { case id, state, emailAt }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = c.value(.id, default: "")
        state = try c.decode(VideoState.self, forKey: .state)
        emailAt = c.value(.emailAt, default: nil)
    }
}

// MARK: - Device and audit

public struct PushTokenRequest: Codable, Equatable, Sendable {
    public var apnsToken: String
    public var sandbox: Bool
    public init(apnsToken: String, sandbox: Bool) {
        self.apnsToken = apnsToken
        self.sandbox = sandbox
    }
}

public enum AuditEventType: String, Codable, Equatable, Sendable {
    case recordStart = "record_start"
    case recordStop = "record_stop"
    case retake
    case draftDiscarded = "draft_discarded"
    case farWarning = "far_warning"
    case override
    case twoKeysWarning = "two_keys_warning"
    case storageWarn = "storage_warn"
    case storageBlock = "storage_block"
    case stopSwitched = "stop_switched"
    case permission
    /// An upload ended with an error or was refused: the detail says why.
    /// Not in the server's list yet (it skips types it does not know).
    case uploadFailed = "upload_failed"
}

public struct AuditEvent: Codable, Equatable, Sendable {
    public var type: AuditEventType
    public var at: Date
    public var videoId: String?
    public var customerId: Int?
    public var detail: [String: String]

    public init(type: AuditEventType, at: Date = Date(), videoId: String? = nil, customerId: Int? = nil, detail: [String: String] = [:]) {
        self.type = type
        self.at = at
        self.videoId = videoId
        self.customerId = customerId
        self.detail = detail
    }

    enum CodingKeys: String, CodingKey { case type, at, videoId, customerId, detail }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(type, forKey: .type)
        try c.encode(at, forKey: .at)
        try c.encode(videoId, forKey: .videoId)
        try c.encode(customerId, forKey: .customerId)
        try c.encode(detail, forKey: .detail)
    }
}

public struct AuditBatch: Codable, Equatable, Sendable {
    public var events: [AuditEvent]
    public init(events: [AuditEvent]) { self.events = events }
}
