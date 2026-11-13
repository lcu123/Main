import Foundation
import ZestCore

/// The phone's side of the audit trail. Events are kept on disk and sent in
/// batches when there is signal; nothing here ever blocks the tech.
@MainActor
final class EventLog {
    private let url: URL
    private var events: [AuditEvent]
    private var sending = false
    private let limit = 200

    init(directory: URL = Files.root) {
        url = directory.appendingPathComponent("events.json")
        events = Files.read([AuditEvent].self, from: url) ?? []
    }

    func log(_ type: AuditEventType, videoId: String? = nil, customerId: Int? = nil, detail: [String: String] = [:]) {
        events.append(AuditEvent(type: type, at: Date(), videoId: videoId, customerId: customerId, detail: detail))
        if events.count > limit {
            events.removeFirst(events.count - limit)
        }
        Files.write(events, to: url)
    }

    func flush(using api: APIClient) {
        guard !sending, !events.isEmpty, api.token != nil else { return }
        sending = true
        let batch = Array(events.prefix(50))
        Task {
            defer { sending = false }
            do {
                try await api.sendEvents(batch)
                events.removeFirst(min(batch.count, events.count))
                Files.write(events, to: url)
            } catch let error as APIError {
                // A batch the server calls malformed would block the trail for good: drop it.
                if error.status == 400 {
                    events.removeFirst(min(batch.count, events.count))
                    Files.write(events, to: url)
                }
            } catch {}
        }
    }
}
