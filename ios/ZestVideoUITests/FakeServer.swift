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
    @discardableResult
    func reset(test: String, config: [String: Any] = [:]) -> Bool {
        request("POST", "/__test/reset", json: ["test": test, "config": config])?.status == 200
    }

    /// Flips switches (see the list at the top of fake_server.py).
    @discardableResult
    func set(_ switches: [String: Any]) -> Bool {
        request("POST", "/__test/set", json: switches)?.status == 200
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

    var requests: [[String: Any]] {
        (raw["requests"] as? [[String: Any]]) ?? []
    }

    var logins: Int { (raw["logins"] as? Int) ?? 0 }
    var logouts: Int { (raw["logouts"] as? Int) ?? 0 }
    var codesRequested: [String] { (raw["codes"] as? [String]) ?? [] }
    var events: [[String: Any]] { (raw["events"] as? [[String: Any]]) ?? [] }

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
    var recommend: Bool { (payload["recommend"] as? Bool) ?? false }
    var override: Bool { (payload["override"] as? Bool) ?? false }
    var notOnMyList: Bool { (payload["notOnMyList"] as? Bool) ?? false }
    var signals: [String: String] { (payload["signals"] as? [String: String]) ?? [:] }

    /// The server holds the whole file and has confirmed it.
    var isHeld: Bool { state == "held" && bytes > 0 && bytes == sizeSent }

    /// Uploads that arrived whole.
    var goodUploads: Int { puts.filter { $0 == "200" }.count }

    /// "complete" calls the server said yes to.
    var goodCompletes: Int { completes.filter { $0 == 200 }.count }

    /// One line for a failure message.
    var summary: String {
        "customer \(customerId.map(String.init) ?? "none") state=\(state) registers=\(registers) puts=\(puts) bytes=\(bytes)/\(sizeSent) completes=\(completes) deletes=\(deletes)"
    }
}
