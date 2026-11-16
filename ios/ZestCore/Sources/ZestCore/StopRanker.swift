import Foundation

/// Works out which stop the tech is most likely standing on.
///
/// Order of trust:
/// 1. An open FieldRoutes check-in (checked in, not checked out), when it is
///    believable right now.
/// 2. Phone GPS: the nearest stops that still need a video.
/// 3. No usable location: the stop he has just finished, then the stops he
///    has not done yet in route order, then the ones he finished a while ago.
///
/// GPS alone never *decides* the stop. It only orders the list; the tech
/// confirms, and the two-keys check runs after recording.
///
/// Why an open check-in is not simply believed. In three real days of this
/// office's check-ins (106 stops, 4 techs): 6 stops were left checked in
/// for over 90 minutes (104 to 347); at 6 check-ins another stop was still
/// open; twice a tech checked the next customer in while still standing at
/// the previous house; and extra appointments at a stop with several are
/// often closed later, from somewhere else. A stop left open for hours
/// must not sit on top of the list as "checked in" while the tech is
/// kilometres away at another house. (Nearest-by-GPS, on the same days,
/// was right 98% of the time.)
public enum StopRanker {
    public enum Reason: Equatable, Sendable {
        case checkedIn
        case nearest
        /// No location, and the first stop is one he completed a moment ago
        /// and has not filmed: where he is standing with the phone.
        case justFinished
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

    /// With no location, a stop completed within this long (and not filmed
    /// yet) is taken to be where the tech still is: 60 minutes.
    ///
    /// In three real days of this office's data, two of the four techs tap
    /// check-in, check-out and "complete" within the same minute at about a
    /// fifth of their stops, and then work there for a median of about 20
    /// minutes. So when he films, the stop already reads "done". An hour
    /// covers the longest of those visits; after that he has most likely
    /// driven on, and the stop goes below the ones still to do.
    public static let justFinishedSeconds: TimeInterval = 60 * 60

    /// Where a stop that still needs a video goes when there is no usable
    /// location (after a believable open check-in, which comes first).
    public enum NoFixTier: Int, Comparable, Sendable {
        /// Completed within the last hour: he is probably still there.
        case justFinished = 2
        /// Not completed yet: the route still to drive, in route order.
        case toDo = 3
        /// Completed longer ago (or with no check-out time to go by): he has
        /// probably left. It stays on the list, lower down.
        case finishedEarlier = 4

        public static func < (a: NoFixTier, b: NoFixTier) -> Bool { a.rawValue < b.rawValue }
    }

    public static func noFixTier(of stop: Stop, now: Date) -> NoFixTier {
        guard stop.done else { return .toDo }
        guard let checkOut = stop.checkOut else { return .finishedEarlier }
        return now.timeIntervalSince(checkOut) <= justFinishedSeconds ? .justFinished : .finishedEarlier
    }

    /// Is this stop's open check-in to be believed right now?
    ///
    /// - With a usable GPS fix and a map pin on the stop, the phone has to
    ///   be at the stop: inside the same distance as the far-away warning
    ///   (`farMeters` plus the fix's own error). If it is not, the check-in
    ///   is stale or early, and the stop is not put first.
    /// - With no usable fix (or no pin), the check-in has to be recent:
    ///   `TwoKeys.openCheckInTrustSeconds`.
    public static func believesOpenCheckIn(of stop: Stop, fix: VideoLocation?, farMeters: Double, now: Date) -> Bool {
        guard stop.isCheckedInNow, let checkIn = stop.checkIn else { return false }
        switch TwoKeys.gpsSignal(fix: fix, stop: stop, farMeters: farMeters) {
        case .match:
            return true
        case .mismatch:
            return false
        case .unknown:
            return now.timeIntervalSince(checkIn) <= TwoKeys.openCheckInTrustSeconds
        }
    }

    /// - Parameters:
    ///   - stops: today's stops from the cached `/v1/today`.
    ///   - fix: the phone's GPS fix, if any, with its accuracy. One too
    ///     fuzzy to use (`TwoKeys.usable`) counts as none.
    ///   - farMeters: the far-away distance from the day's settings.
    ///   - now: the time, for how old an open check-in is.
    ///   - haveVideo: customer IDs that already have a video, from the server
    ///     *or* from this phone's own queue (which the server may not know yet).
    public static func rank(
        stops: [Stop], fix: VideoLocation?, farMeters: Double = 150, now: Date = Date(), haveVideo: Set<Int> = []
    ) -> Result {
        let location = TwoKeys.usable(fix)?.coordinate
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
        /// With no location: just finished (latest check-out first), then
        /// still to do in route order, then finished earlier in route order.
        func withNoFix(_ list: [Stop]) -> [Stop] {
            list.sorted { a, b in
                let ta = noFixTier(of: a, now: now), tb = noFixTier(of: b, now: now)
                if ta != tb { return ta < tb }
                if ta == .justFinished, let outA = a.checkOut, let outB = b.checkOut, outA != outB { return outA > outB }
                return byRoute(a, b)
            }
        }

        let needVideo = stops.filter { !hasVideo($0) }
        var suggested: [Stop] = []
        var reason: Reason = .routeOrder

        // 1. Open check-ins that are believable now, most recent first.
        let checkedIn = needVideo
            .filter { believesOpenCheckIn(of: $0, fix: fix, farMeters: farMeters, now: now) }
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
            let left = remaining.filter { candidate in !suggested.contains { $0.customerId == candidate.customerId } }
            let ordered: [Stop]
            if location == nil {
                // 3. No usable location. A stop he completed a moment ago and
                //    has not filmed is where he is standing, phone in hand:
                //    it must not fall below the ones he has not driven to.
                ordered = withNoFix(left)
                if suggested.isEmpty, let first = ordered.first, noFixTier(of: first, now: now) == .justFinished {
                    reason = .justFinished
                }
            } else {
                // With a location, what GPS could not place (no map pin):
                // route order, unfinished stops first.
                ordered = left.filter { !$0.done }.sorted(by: byRoute) + left.filter { $0.done }.sorted(by: byRoute)
            }
            suggested.append(contentsOf: ordered.prefix(suggestionCount - suggested.count))
        }

        let suggestedIDs = Set(suggested.map { $0.customerId })
        let others = stops.filter { !suggestedIDs.contains($0.customerId) }
        let stillNeed = others.filter { !hasVideo($0) }
        let rest = (location == nil ? withNoFix(stillNeed) : stillNeed.sorted(by: byRoute))
            + others.filter { hasVideo($0) }.sorted(by: byRoute)

        return Result(suggested: suggested.map(ranked), rest: rest.map(ranked), reason: reason)
    }
}
