import Foundation

/// The number on a stop's card.
///
/// It is the stop's place on the tech's own route for the day, in the order
/// the office planned it in FieldRoutes, counting from 1. (The server's
/// `order` counts every tech's stops together, so one tech's run 3, 7, 12:
/// the cards say 1, 2, 3.)
///
/// What the number is **not**:
///
/// - It is not where the card sits on the screen. The list puts the stop
///   the tech is standing at first (see `StopRanker`), so a tech who works
///   the route back to front sees card 9 on top, then 8, and that is right.
/// - It is not an instruction and nothing checks it. A stop can be filmed
///   in any order, left and come back to, or done last.
/// - It is not the stop's name for the day. When the office takes a stop
///   off the route the stops after it move up by one, as they do in
///   FieldRoutes; when it adds one, they move down.
public enum RouteNumbers {
    /// Each customer's number.
    public static func numbers(for stops: [Stop]) -> [Int: Int] {
        var numbers: [Int: Int] = [:]
        for (index, stop) in inRouteOrder(stops).enumerated() where numbers[stop.customerId] == nil {
            numbers[stop.customerId] = index + 1
        }
        return numbers
    }

    /// One stop's number; 1 for a stop that is not on the list (the caller
    /// has a card to draw and no better number to put on it).
    public static func number(of customerId: Int, in stops: [Stop]) -> Int {
        numbers(for: stops)[customerId] ?? 1
    }

    /// The route as planned. Two stops with the same `order` (the server
    /// never sends that; an old saved list might) fall back on the
    /// customer's number, so the answer is the same every time.
    public static func inRouteOrder(_ stops: [Stop]) -> [Stop] {
        stops.sorted { $0.order != $1.order ? $0.order < $1.order : $0.customerId < $1.customerId }
    }
}

/// The day's list, and a stop that is no longer on it.
///
/// The office changes a route during the day: a stop is cancelled, moved to
/// another tech, added, or taken off and put back. The phone asks for the
/// list again whenever the app comes to the front, after every video that
/// reaches the server (or is refused, or deleted), when a notification
/// arrives, and every two minutes while My Route is showing.
public enum RouteList {
    /// Is the stop the tech has open (or has just filmed) still on the list?
    ///
    /// - Returns: `true` only when there is a list and the customer is not
    ///   on it. With no list at all (no signal since the app was installed,
    ///   or a new day not loaded yet) nothing can be said, and nothing is.
    public static func hasLeft(customerId: Int?, list: [Stop]?) -> Bool {
        guard let customerId, let list else { return false }
        return !list.contains { $0.customerId == customerId }
    }
}
