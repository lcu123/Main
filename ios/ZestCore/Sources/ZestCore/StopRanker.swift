import Foundation

/// Works out which stop the tech is most likely standing on.
///
/// Order of trust:
/// 1. An open FieldRoutes check-in (checked in, not checked out).
/// 2. Phone GPS: the nearest stops that still need a video.
/// 3. No location: route order, next unfinished stop first.
///
/// GPS alone never *decides* the stop. It only orders the list; the tech
/// confirms, and the two-keys check runs after recording.
public enum StopRanker {
    public enum Reason: Equatable, Sendable {
        case checkedIn
        case nearest
        case routeOrder
    }

    public struct RankedStop: Equatable, Identifiable, Sendable {
        public let stop: Stop
        public let distanceMeters: Double?
        public var id: Int { stop.customerId }

        public init(stop: Stop, distanceMeters: Double?) {
            self.stop = stop
            self.distanceMeters = distanceMeters
        }
    }

    public struct Result: Equatable, Sendable {
        /// Up to three stops for the top of the screen, best guess first.
        public let suggested: [RankedStop]
        /// Everything else: stops still needing a video in route order, then
        /// the ones that already have one.
        public let rest: [RankedStop]
        public let reason: Reason

        public var all: [RankedStop] { suggested + rest }
    }

    public static let suggestionCount = 3

    /// - Parameters:
    ///   - stops: today's stops from the cached `/v1/today`.
    ///   - location: the phone's fix, if any.
    ///   - haveVideo: customer IDs that already have a video, from the server
    ///     *or* from this phone's own queue (which the server may not know yet).
    public static func rank(stops: [Stop], location: Coordinate?, haveVideo: Set<Int> = []) -> Result {
        func hasVideo(_ stop: Stop) -> Bool {
            stop.liveVideo != nil || haveVideo.contains(stop.customerId)
        }
        func distance(_ stop: Stop) -> Double? {
            guard let location, let pin = stop.coordinate else { return nil }
            return Geo.distanceMeters(location, pin)
        }
        func ranked(_ stop: Stop) -> RankedStop {
            RankedStop(stop: stop, distanceMeters: distance(stop))
        }
        func byRoute(_ a: Stop, _ b: Stop) -> Bool {
            a.order != b.order ? a.order < b.order : a.customerId < b.customerId
        }

        let needVideo = stops.filter { !hasVideo($0) }
        var suggested: [Stop] = []
        var reason: Reason = .routeOrder

        // 1. Open check-ins, most recent first.
        let checkedIn = needVideo
            .filter { $0.isCheckedInNow }
            .sorted { ($0.checkIn ?? .distantPast) > ($1.checkIn ?? .distantPast) }
        if let first = checkedIn.first {
            suggested.append(first)
            reason = .checkedIn
        }

        let remaining = needVideo.filter { candidate in !suggested.contains { $0.customerId == candidate.customerId } }
        let room = suggestionCount - suggested.count

        if location != nil {
            // 2. Nearest by GPS. Stops with no map pin cannot be ranked this way.
            let near = remaining
                .compactMap { stop -> (Stop, Double)? in
                    guard let d = distance(stop) else { return nil }
                    return (stop, d)
                }
                .sorted { $0.1 != $1.1 ? $0.1 < $1.1 : byRoute($0.0, $1.0) }
                .prefix(room)
                .map { $0.0 }
            if !near.isEmpty && suggested.isEmpty { reason = .nearest }
            suggested.append(contentsOf: near)
        }

        if suggested.count < suggestionCount {
            // 3. Route order, unfinished stops first.
            let left = remaining.filter { candidate in !suggested.contains { $0.customerId == candidate.customerId } }
            let ordered = left.filter { !$0.done }.sorted(by: byRoute) + left.filter { $0.done }.sorted(by: byRoute)
            suggested.append(contentsOf: ordered.prefix(suggestionCount - suggested.count))
        }

        let suggestedIDs = Set(suggested.map { $0.customerId })
        let others = stops.filter { !suggestedIDs.contains($0.customerId) }
        let rest = others.filter { !hasVideo($0) }.sorted(by: byRoute) + others.filter { hasVideo($0) }.sorted(by: byRoute)

        return Result(suggested: suggested.map(ranked), rest: rest.map(ranked), reason: reason)
    }
}
