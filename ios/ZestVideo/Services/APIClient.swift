import Foundation
import ZestCore

/// What can go wrong talking to the Zest video server.
enum APIError: Error {
    /// 401: the device was cut off or the tech is no longer active.
    case unauthorized
    /// 426: this build is too old.
    case updateRequired(minBuild: Int?)
    /// Any other non-2xx answer, with the server's plain-words message.
    case http(status: Int, body: APIErrorBody, retryAfter: TimeInterval?)
    /// No answer at all: no signal, timeout, server down.
    case offline(String)
    /// A 2xx answer the app could not read.
    case badResponse

    /// Plain words for the tech.
    var message: String {
        switch self {
        case .unauthorized:
            return "This phone was signed out. Please sign in again."
        case .updateRequired:
            return "Please update Zest Video."
        case .http(_, let body, _):
            return body.message.isEmpty ? "Something went wrong. Please try again." : body.message
        case .offline:
            return "No signal. Please try again."
        case .badResponse:
            return "Something went wrong. Please try again."
        }
    }

    var code: String? {
        if case .http(_, let body, _) = self { return body.error }
        return nil
    }

    var status: Int? {
        if case .http(let status, _, _) = self { return status }
        return nil
    }

    /// Worth trying again later without changing anything.
    var isTransient: Bool {
        switch self {
        case .offline, .badResponse: return true
        case .http(let status, _, _): return status >= 500 || status == 429 || status == 408
        case .unauthorized, .updateRequired: return false
        }
    }

    var retryAfter: TimeInterval? {
        if case .http(_, _, let retryAfter) = self { return retryAfter }
        return nil
    }
}

/// The only network the app does, apart from the signed upload itself.
final class APIClient: @unchecked Sendable {
    let baseURL: URL
    private let session: URLSession

    /// The device token. Set on the main thread at launch, login and sign-out.
    var token: String?

    /// `CFBundleVersion` as a number, sent on every request.
    static let build: Int = {
        let raw = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? ""
        return Int(raw) ?? 1
    }()

    init(baseURL: URL? = nil, session: URLSession? = nil) {
        if let baseURL {
            self.baseURL = baseURL
        } else {
            let configured = (Bundle.main.object(forInfoDictionaryKey: "ZestServerURL") as? String ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            self.baseURL = URL(string: configured).flatMap { $0.host == nil ? nil : $0 } ?? URL(string: "https://localhost")!
        }
        if let session {
            self.session = session
        } else {
            // No disk cache, no cookies: nothing about the route is left behind.
            let configuration = URLSessionConfiguration.ephemeral
            configuration.timeoutIntervalForRequest = 20
            configuration.timeoutIntervalForResource = 40
            configuration.waitsForConnectivity = false
            configuration.httpAdditionalHeaders = ["Accept": "application/json"]
            self.session = URLSession(configuration: configuration)
        }
    }

    // MARK: Calls

    func requestCode(contact: String) async throws {
        let _: OKResponse = try await send("POST", "/v1/auth/request-code", body: RequestCodeRequest(contact: contact), auth: false)
    }

    func verify(_ request: VerifyRequest) async throws -> VerifyResponse {
        try await send("POST", "/v1/auth/verify", body: request, auth: false)
    }

    func logout() async throws {
        let _: OKResponse = try await send("POST", "/v1/auth/logout")
    }

    func today() async throws -> TodayResponse {
        try await send("GET", "/v1/today")
    }

    func register(_ request: RegisterVideoRequest) async throws -> RegisterVideoResponse {
        try await send("POST", "/v1/videos", body: request)
    }

    func complete(id: String, sizeBytes: Int64) async throws -> VideoStatusResponse {
        try await send("POST", "/v1/videos/\(id)/complete", body: CompleteVideoRequest(sizeBytes: sizeBytes))
    }

    func deleteVideo(id: String) async throws -> VideoStatusResponse {
        try await send("DELETE", "/v1/videos/\(id)")
    }

    func registerPush(token apnsToken: String, sandbox: Bool) async throws {
        let _: OKResponse = try await send("POST", "/v1/devices/push", body: PushTokenRequest(apnsToken: apnsToken, sandbox: sandbox))
    }

    func sendEvents(_ events: [AuditEvent]) async throws {
        let _: OKResponse = try await send("POST", "/v1/events", body: AuditBatch(events: events))
    }

    // MARK: Plumbing

    private struct NoBody: Encodable {}

    private func send<Response: Decodable>(_ method: String, _ path: String, auth: Bool = true) async throws -> Response {
        try await send(method, path, body: Optional<NoBody>.none, auth: auth)
    }

    private func send<Body: Encodable, Response: Decodable>(
        _ method: String, _ path: String, body: Body?, auth: Bool = true
    ) async throws -> Response {
        var base = baseURL.absoluteString
        while base.hasSuffix("/") { base.removeLast() }
        guard let url = URL(string: base + path) else { throw APIError.badResponse }

        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue(String(APIClient.build), forHTTPHeaderField: "X-Zest-Build")
        if auth {
            guard let token else { throw APIError.unauthorized }
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try ZestJSONBridge.encoder.encode(body)
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw APIError.offline(error.localizedDescription)
        }
        guard let http = response as? HTTPURLResponse else { throw APIError.badResponse }

        if (200..<300).contains(http.statusCode) {
            // Some answers carry nothing the app needs; do not fail on an odd body.
            if Response.self == OKResponse.self, let ok = OKResponse(ok: true) as? Response {
                return ok
            }
            guard let decoded = try? ZestJSONBridge.decoder.decode(Response.self, from: data) else {
                throw APIError.badResponse
            }
            return decoded
        }

        let errorBody = (try? ZestJSONBridge.decoder.decode(APIErrorBody.self, from: data))
            ?? APIErrorBody(error: "server_error", message: "")
        if http.statusCode == 426 || errorBody.error == "update_required" {
            throw APIError.updateRequired(minBuild: errorBody.minBuild)
        }
        if http.statusCode == 401 && errorBody.error != "bad_code" {
            throw APIError.unauthorized
        }
        let retryAfter = http.value(forHTTPHeaderField: "Retry-After").flatMap { TimeInterval($0) }
        throw APIError.http(status: http.statusCode, body: errorBody, retryAfter: retryAfter)
    }
}
