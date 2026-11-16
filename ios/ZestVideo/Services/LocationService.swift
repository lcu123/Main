import CoreLocation
import Foundation
import ZestCore

/// Location, used as lightly as possible: one fix when a screen needs it,
/// never a running stream. The app works fully with location turned off.
///
/// Leave reminders use region monitoring (cell and Wi-Fi based, close to
/// free on battery), not background GPS. There is no background location mode.
@MainActor
final class LocationService: NSObject, ObservableObject, CLLocationManagerDelegate {
    @Published private(set) var fix: VideoLocation?
    @Published private(set) var status: CLAuthorizationStatus = .notDetermined

    /// Called with the customer ID when the phone leaves a monitored stop.
    var onLeave: ((Int) -> Void)?

    private let manager = CLLocationManager()
    private var fixTime: Date?
    private static let regionPrefix = "stop-"
    static let leaveRadiusMeters: CLLocationDistance = 120
    static let maxRegions = 20

    override init() {
        super.init()
        manager.delegate = self
        status = manager.authorizationStatus
    }

    var isAllowed: Bool {
        status == .authorizedWhenInUse || status == .authorizedAlways
    }

    var isAlways: Bool { status == .authorizedAlways }

    /// The first, light question: location while the app is open.
    func askWhenInUse() {
        if manager.authorizationStatus == .notDetermined {
            manager.requestWhenInUseAuthorization()
        }
    }

    /// The second question, only after the tech taps "Turn on reminders".
    func askAlways() {
        manager.requestAlwaysAuthorization()
    }

    /// Asks for a single fix. `precise` for the stop being recorded;
    /// otherwise a coarse fix is enough to order the list.
    func refresh(precise: Bool) {
        guard isAllowed else { return }
        manager.desiredAccuracy = precise ? kCLLocationAccuracyNearestTenMeters : kCLLocationAccuracyHundredMeters
        manager.requestLocation()
    }

    /// The last fix, if it is recent enough to trust for "where am I now".
    func freshFix(maxAge: TimeInterval = 120) -> VideoLocation? {
        guard let fix, let fixTime, Date().timeIntervalSince(fixTime) <= maxAge else { return nil }
        return fix
    }

    // MARK: Leave reminders

    /// Watches up to 20 stops for the phone leaving them. Pass an empty list to stop.
    func setLeaveRegions(_ stops: [Stop]) {
        guard isAlways, CLLocationManager.isMonitoringAvailable(for: CLCircularRegion.self) else {
            clearLeaveRegions()
            return
        }
        let wanted = stops.filter { $0.coordinate != nil }.prefix(LocationService.maxRegions)
        let wantedIDs = Set(wanted.map { LocationService.regionPrefix + String($0.customerId) })

        for region in manager.monitoredRegions where region.identifier.hasPrefix(LocationService.regionPrefix) {
            if !wantedIDs.contains(region.identifier) {
                manager.stopMonitoring(for: region)
            }
        }
        let existing = Set(manager.monitoredRegions.map { $0.identifier })
        for stop in wanted {
            let identifier = LocationService.regionPrefix + String(stop.customerId)
            guard !existing.contains(identifier), let lat = stop.lat, let lng = stop.lng else { continue }
            let region = CLCircularRegion(
                center: CLLocationCoordinate2D(latitude: lat, longitude: lng),
                radius: LocationService.leaveRadiusMeters,
                identifier: identifier
            )
            region.notifyOnEntry = false
            region.notifyOnExit = true
            manager.startMonitoring(for: region)
        }
    }

    func clearLeaveRegions() {
        for region in manager.monitoredRegions where region.identifier.hasPrefix(LocationService.regionPrefix) {
            manager.stopMonitoring(for: region)
        }
    }

    // MARK: CLLocationManagerDelegate

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let last = locations.last else { return }
        let fix = VideoLocation(
            lat: last.coordinate.latitude, lng: last.coordinate.longitude,
            accuracyM: (last.horizontalAccuracy * 10).rounded() / 10
        )
        let time = last.timestamp
        Task { @MainActor in
            self.fix = fix
            self.fixTime = time
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        // No fix is fine: the app falls back to route order.
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        let status = manager.authorizationStatus
        Task { @MainActor in
            let wasAllowed = self.isAllowed
            self.status = status
            // Just allowed: get a fix now. Without this the list stayed in
            // route order (not nearest first) until the next refresh, two
            // minutes after the tech's very first sign-in.
            if !wasAllowed && self.isAllowed {
                self.refresh(precise: false)
            }
        }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didExitRegion region: CLRegion) {
        let identifier = region.identifier
        guard identifier.hasPrefix("stop-"), let customerId = Int(identifier.dropFirst("stop-".count)) else { return }
        Task { @MainActor in
            // One reminder per stop: stop watching it once it has fired.
            for watched in self.manager.monitoredRegions where watched.identifier == identifier {
                self.manager.stopMonitoring(for: watched)
            }
            self.onLeave?(customerId)
        }
    }
}

#if targetEnvironment(simulator)
extension LocationService {
    /// Screenshot mode only: pretend the phone is here. No permission is asked.
    func setScreenshotFix(_ fix: VideoLocation) {
        self.fix = fix
        fixTime = Date()
    }
}
#endif
