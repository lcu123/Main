import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

/// The tests' line to the fake server (`ios/Scripts/fake_server.py`).
///
/// It speaks HTTP over a plain socket rather than URLSession, so it needs no
/// App Transport Security exception in the test runner and every call is a
/// simple blocking call: reset, flip a switch, read back what the server saw.
struct FakeServer {
    var host = "127.0.0.1"
    var port: UInt16 = 8765

    /// What the app is pointed at. "localhost", not the number: a plain
    /// host name with no dots is what iOS's "local networking" allowance
    /// for plain HTTP is written for.
    var baseURL: String { "http://localhost:\(port)" }

    struct Reply {
        let status: Int
        let body: Data

        var json: [String: Any] {
            ((try? JSONSerialization.jsonObject(with: body)) as? [String: Any]) ?? [:]
        }

        var text: String { String(decoding: body, as: UTF8.self) }
    }

    // MARK: Controls

    /// Forgets everything and names the test that is about to run.
    /// - Returns: a device token the server has already signed in, so a test
    ///   can start the app signed in without typing a number and a code.
    ///   `nil` when the server did not answer.
    /// - Parameter route: "usual" (five stops, one locked) or "long" (twenty
    ///   stops side by side, customers 301 to 320, none locked).
    @discardableResult
    func reset(test: String, config: [String: Any] = [:], route: String = "usual") -> String? {
        guard let reply = request("POST", "/__test/reset", json: ["test": test, "config": config, "route": route]),
              reply.status == 200 else { return nil }
        return reply.json["token"] as? String
    }

    /// Flips switches (see the list at the top of fake_server.py).
    @discardableResult
    func set(_ switches: [String: Any]) -> Bool {
        request("POST", "/__test/set", json: switches)?.status == 200
    }

    /// Another tech already recorded this stop today.
    /// - Parameter helper: true when the signed-in tech has no appointment
    ///   of his own there, so the server shows him that video (as not his).
    ///   False when he has his own appointment: he is shown nothing.
    /// - Returns: the other tech's video ID, or nil if the server refused.
    @discardableResult
    func otherTechRecorded(customerId: Int, state: String = "sent", helper: Bool) -> String? {
        let body: [String: Any] = ["customerId": customerId, "state": state, "helper": helper]
        guard let reply = request("POST", "/__test/other-tech", json: body), reply.status == 200 else { return nil }
        return reply.json["id"] as? String
    }

    /// Everything the server has seen since the last reset.
    func state() -> ServerState {
        ServerState(raw: request("GET", "/__test/state")?.json ?? [:])
    }

    /// The request log as text, for a failure report.
    func logText() -> String {
        request("GET", "/__test/log")?.text ?? "(the fake server did not answer)"
    }

    var isUp: Bool {
        request("GET", "/__test/ping")?.status == 200
    }

    // MARK: HTTP over a socket

    func request(_ method: String, _ path: String, json: [String: Any]? = nil) -> Reply? {
        let payload: Data
        if let json, let encoded = try? JSONSerialization.data(withJSONObject: json) {
            payload = encoded
        } else {
            payload = Data()
        }
        var head = "\(method) \(path) HTTP/1.1\r\nHost: \(host):\(port)\r\nConnection: close\r\n"
        if json != nil {
            head += "Content-Type: application/json\r\n"
        }
        head += "Content-Length: \(payload.count)\r\n\r\n"
        var message = Data(head.utf8)
        message.append(payload)

        #if canImport(Darwin)
        let descriptor = socket(AF_INET, SOCK_STREAM, 0)
        #else
        let descriptor = socket(AF_INET, Int32(SOCK_STREAM.rawValue), 0)
        #endif
        guard descriptor >= 0 else { return nil }
        defer { close(descriptor) }

        #if canImport(Darwin)
        // A server that hangs up must give an error here, not kill the tests.
        var noSignal: Int32 = 1
        setsockopt(descriptor, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size))
        #endif
        var timeout = timeval(tv_sec: 20, tv_usec: 0)
        setsockopt(descriptor, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(descriptor, SOL_SOCKET, SO_SNDTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))

        var address = sockaddr_in()
        address.sin_family = sa_family_t(AF_INET)
        address.sin_port = port.bigEndian
        address.sin_addr.s_addr = inet_addr(host)
        let connected = withUnsafePointer(to: &address) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard connected == 0 else { return nil }

        let bytes = [UInt8](message)
        var sent = 0
        while sent < bytes.count {
            let count = bytes[sent...].withUnsafeBytes { raw in
                send(descriptor, raw.baseAddress, raw.count, 0)
            }
            if count <= 0 { return nil }
            sent += count
        }

        var response = Data()
        var buffer = [UInt8](repeating: 0, count: 16_384)
        while true {
            let count = recv(descriptor, &buffer, buffer.count, 0)
            if count <= 0 { break }
            response.append(buffer, count: count)
        }

        guard let split = response.range(of: Data("\r\n\r\n".utf8)) else { return nil }
        let headText = String(decoding: response[response.startIndex..<split.lowerBound], as: UTF8.self)
        let statusLine = headText.components(separatedBy: "\r\n").first ?? ""
        let parts = statusLine.split(separator: " ")
        guard parts.count >= 2, let status = Int(parts[1]) else { return nil }
        return Reply(status: status, body: Data(response[split.upperBound...]))
    }
}

/// What the server saw, with the questions the tests ask of it.
struct ServerState {
    let raw: [String: Any]

    /// Videos in the order they were first registered. The one locked
    /// fixture video is not among them.
    var videos: [ServerVideo] {
        ((raw["videos"] as? [[String: Any]]) ?? []).map(ServerVideo.init(raw:))
    }

    /// The other tech's videos (put in by `otherTechRecorded`), which the
    /// app must never replace, delete or list as its own.
    var otherTechVideos: [ServerVideo] {
        ((raw["otherTechVideos"] as? [[String: Any]]) ?? []).map(ServerVideo.init(raw:))
    }

    var requests: [[String: Any]] {
        (raw["requests"] as? [[String: Any]]) ?? []
    }

    var logins: Int { (raw["logins"] as? Int) ?? 0 }
    var logouts: Int { (raw["logouts"] as? Int) ?? 0 }
    var codesRequested: [String] { (raw["codes"] as? [String]) ?? [] }
    var events: [[String: Any]] { (raw["events"] as? [[String: Any]]) ?? [] }

    /// What the app said about its uploads: each task's start and end.
    var notes: [String] {
        ((raw["notes"] as? [[String: Any]]) ?? []).compactMap { $0["note"] as? String }
    }

    /// The numbers the app posted for the performance tests, in order. Each
    /// is one note: "metric route=today ms=812 memoryMB=41.3 videoFiles=0 ...".
    var metrics: [AppMetric] {
        notes.compactMap(AppMetric.init(note:))
    }

    /// How many requests of this kind the app made.
    func count(_ method: String, _ pathPart: String, status: Int? = nil) -> Int {
        requests.filter { entry in
            guard (entry["method"] as? String) == method,
                  ((entry["path"] as? String) ?? "").contains(pathPart) else { return false }
            if let status { return (entry["status"] as? Int) == status }
            return true
        }.count
    }

    /// How many times the app asked to register a video (`POST /v1/videos`),
    /// whatever the answer, or only those answered with `status`.
    func registerCalls(status: Int? = nil) -> Int {
        calls("POST", exactly: "/v1/videos", status: status)
    }

    /// One try at registering a video, as the server saw it.
    struct RegisterTry {
        /// The video it was for ("" if the call carried none).
        let videoId: String
        /// "200", "409"... or "dropped" when the server was out of reach and
        /// the call got no answer at all.
        let answer: String
        /// Seconds since the test's server was reset.
        let at: Double

        var gotThrough: Bool { answer == "200" }
        var summary: String { "\(videoId.prefix(8)) \(answer) at \(Int(at)) s" }
    }

    /// Every `POST /v1/videos` in the order it reached the server, answered
    /// or not.
    var registerTries: [RegisterTry] {
        requests.compactMap { entry in
            guard (entry["method"] as? String) == "POST",
                  ((entry["path"] as? String) ?? "").components(separatedBy: "?").first == "/v1/videos" else { return nil }
            let body = (entry["body"] as? [String: Any]) ?? [:]
            let answer = (entry["status"] as? Int).map(String.init) ?? (entry["status"] as? String) ?? "?"
            return RegisterTry(videoId: (body["id"] as? String) ?? "", answer: answer, at: (entry["t"] as? Double) ?? 0)
        }
    }

    /// Requests to exactly this path (the query string is ignored).
    func calls(_ method: String, exactly path: String, status: Int? = nil) -> Int {
        requests.filter { entry in
            guard (entry["method"] as? String) == method else { return false }
            let full = (entry["path"] as? String) ?? ""
            guard full.components(separatedBy: "?").first == path else { return false }
            if let status { return (entry["status"] as? Int) == status }
            return true
        }.count
    }

    /// The only video, when a test expects exactly one.
    var onlyVideo: ServerVideo? { videos.count == 1 ? videos[0] : nil }
}

struct ServerVideo {
    let raw: [String: Any]

    var id: String { (raw["id"] as? String) ?? "" }
    var customerId: Int? { raw["customerId"] as? Int }
    var state: String { (raw["state"] as? String) ?? "" }
    var registers: Int { (raw["registers"] as? Int) ?? 0 }
    var bytes: Int { (raw["bytes"] as? Int) ?? 0 }
    var deletes: Int { (raw["deletes"] as? Int) ?? 0 }
    var putsStarted: Int { (raw["putsStarted"] as? Int) ?? 0 }

    /// The status of each finished upload attempt: 200, 403, or "cut-off".
    var puts: [String] {
        ((raw["puts"] as? [[String: Any]]) ?? []).map { entry in
            if let code = entry["status"] as? Int { return String(code) }
            return (entry["status"] as? String) ?? "?"
        }
    }

    /// The status of each "complete" call.
    var completes: [Int] { (raw["completes"] as? [Int]) ?? [] }

    /// What the app sent when it registered the video.
    var payload: [String: Any] { (raw["payload"] as? [String: Any]) ?? [:] }

    var sizeSent: Int { (payload["sizeBytes"] as? Int) ?? -1 }
    /// The day the video was filmed for, as the app sent it ("2026-11-13").
    var stopDate: String { (payload["stopDate"] as? String) ?? "" }
    /// True when the app sent no location at all (`"location": null`).
    var sentNoLocation: Bool { payload["location"] == nil || payload["location"] is NSNull }
    var recommend: Bool { (payload["recommend"] as? Bool) ?? false }
    var override: Bool { (payload["override"] as? Bool) ?? false }
    var notOnMyList: Bool { (payload["notOnMyList"] as? Bool) ?? false }
    var signals: [String: String] { (payload["signals"] as? [String: String]) ?? [:] }

    /// The server holds the whole file and has confirmed it.
    var isHeld: Bool { state == "held" && bytes > 0 && bytes == sizeSent }

    /// The same for a video not on the list: the whole file is there, and
    /// it waits for the office to match it to a customer.
    var isWithTheOffice: Bool { state == "unmatched" && bytes > 0 && bytes == sizeSent }

    /// Uploads that arrived whole.
    var goodUploads: Int { puts.filter { $0 == "200" }.count }

    /// "complete" calls the server said yes to.
    var goodCompletes: Int { completes.filter { $0 == 200 }.count }

    /// One line for a failure message.
    var summary: String {
        "customer \(customerId.map(String.init) ?? "none") state=\(state) registers=\(registers) puts=\(puts) bytes=\(bytes)/\(sizeSent) completes=\(completes) deletes=\(deletes)"
    }
}

/// One line of numbers from the app (UI-test mode with `ZEST_TEST_METRICS=1`).
struct AppMetric {
    /// "route=today", "routeUsable".
    let name: String
    let values: [String: Double]

    init?(note: String) {
        let words = note.split(separator: " ").map(String.init)
        guard words.count >= 2, words[0] == "metric" else { return nil }
        name = words[1]
        var values: [String: Double] = [:]
        for word in words.dropFirst(2) {
            let parts = word.split(separator: "=", maxSplits: 1).map(String.init)
            if parts.count == 2, let number = Double(parts[1]) {
                values[parts[0]] = number
            }
        }
        self.values = values
    }

    /// Milliseconds since the app's own code started in that launch.
    var milliseconds: Double { values["ms"] ?? -1 }
    var memoryMB: Double { values["memoryMB"] ?? -1 }
    var videoFiles: Int { Int(values["videoFiles"] ?? -1) }
    var videoBytes: Int { Int(values["videoBytes"] ?? -1) }
    var storedBytes: Int { Int(values["storedBytes"] ?? -1) }
    var tempBytes: Int { Int(values["tempBytes"] ?? -1) }
}
