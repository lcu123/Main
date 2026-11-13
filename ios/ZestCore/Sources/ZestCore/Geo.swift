import Foundation

/// A point on the map.
public struct Coordinate: Codable, Equatable, Sendable {
    public var lat: Double
    public var lng: Double

    public init(lat: Double, lng: Double) {
        self.lat = lat
        self.lng = lng
    }
}

public enum Geo {
    static let earthRadiusMeters = 6_371_000.0

    /// Great-circle distance in metres (haversine). Plenty for "which house".
    public static func distanceMeters(_ a: Coordinate, _ b: Coordinate) -> Double {
        let lat1 = a.lat * .pi / 180
        let lat2 = b.lat * .pi / 180
        let dLat = (b.lat - a.lat) * .pi / 180
        let dLng = (b.lng - a.lng) * .pi / 180
        let h = sin(dLat / 2) * sin(dLat / 2) + cos(lat1) * cos(lat2) * sin(dLng / 2) * sin(dLng / 2)
        return 2 * earthRadiusMeters * asin(min(1, sqrt(h)))
    }

    /// "350 ft away" / "1.2 miles away". US units, plain words.
    public static func distanceText(meters: Double) -> String {
        let feet = meters * 3.28084
        if feet < 1000 {
            let rounded = max(50, Int((feet / 50).rounded()) * 50)
            return "\(rounded) ft away"
        }
        let miles = meters / 1609.344
        if miles < 10 {
            return String(format: "%.1f miles away", miles)
        }
        return "\(Int(miles.rounded())) miles away"
    }
}
