import Foundation
@testable import ZestCore

/// Made-up sample data. Nothing here is a real person or address.
enum Fixtures {
    static let base = ZestDates.parseISO("2026-11-13T18:00:00Z")!

    static func at(_ minutes: Double) -> Date { base.addingTimeInterval(minutes * 60) }

    static func stop(
        _ id: Int, _ name: String, last: String, _ address: String, lat: Double? = nil, lng: Double? = nil,
        order: Int = 0, checkIn: Date? = nil, checkOut: Date? = nil, done: Bool = false, video: StopVideo? = nil
    ) -> Stop {
        Stop(
            customerId: id, name: name, lastName: last, address: address, cityLine: "Sampletown, CA 90000",
            lat: lat, lng: lng, appointmentIds: [id * 10], services: ["Mowing"], order: order,
            checkIn: checkIn, checkOut: checkOut, done: done, video: video
        )
    }

    // A small route. 2 and 3 are next-door neighbours on the same street.
    static let maple = stop(1, "Avery Maple", last: "Maple", "410 Birchwood Ln", lat: 38.6000, lng: -121.3000, order: 1)
    static let quill = stop(2, "Jordan Quillfeather", last: "Quillfeather", "123 Harbor View Dr", lat: 38.6100, lng: -121.3100, order: 2)
    static let ostrander = stop(3, "Riley Ostrander", last: "Ostrander", "125 Harbor View Dr", lat: 38.6101, lng: -121.3100, order: 3)
    static let fifth = stop(4, "Casey Delacroix", last: "Delacroix", "77 5th St Apt 4", lat: 38.6500, lng: -121.3500, order: 4)
    static let business = stop(5, "Pinecone Bakery LLC", last: "", "9 N Market Way", lat: 38.7000, lng: -121.4000, order: 5)
    static let noPin = stop(6, "Sam Winterbourne", last: "Winterbourne", "2040 Cedar Crest Ct", order: 6)

    static let route = [maple, quill, ostrander, fifth, business, noPin]

    static func item(
        id: String = "0b0e7c1e-7c35-4f0b-9c50-0f7f6f0f3f11", customerId: Int? = 2, recordedAt: Date = base,
        notOnMyList: Bool = false
    ) -> QueueItem {
        QueueItem(
            id: id, stopDate: "2026-11-13", customerId: customerId, customerName: "Jordan Quillfeather",
            appointmentIds: [20, 21], notOnMyList: notOnMyList, recordedAt: recordedAt, durationSec: 14.64,
            sizeBytes: 5_012_345, location: VideoLocation(lat: 38.61, lng: -121.31, accuracyM: 8),
            signals: Signals(spoken: .match, gps: .match, checkIn: .unknown), override: false, recommend: false,
            spokenText: "This is the Quillfeather home", fileName: "\(id).mp4"
        )
    }

    static let ticket = UploadTicket(
        url: URL(string: "https://bucket.example.com/videos/x.mp4?sig=abc")!,
        headers: ["Content-Type": "video/mp4"], expiresAt: at(24 * 60)
    )

    static let todayJSON = """
    {
      "date": "2026-11-13",
      "serverTime": "2026-11-13T15:04:05Z",
      "tech": {"id": 42, "firstName": "Sample", "demo": false},
      "config": {
        "maxSeconds": 15, "holdMinutes": 10, "farMeters": 150, "minBuild": 1,
        "requireVideo": true, "recommendButton": true, "leaveReminders": true,
        "prompts": ["Say the name or street", "What I did", "What I saw", "What I recommend"],
        "officePhone": "+15555550100"
      },
      "stops": [
        {
          "customerId": 2, "name": "Jordan Quillfeather", "lastName": "Quillfeather",
          "address": "123 Harbor View Dr", "cityLine": "Sampletown, CA 90000",
          "lat": 38.61, "lng": -121.31, "appointmentIds": [20, 21, 22], "services": ["Mowing", "Edging"],
          "order": 1, "checkIn": "2026-11-13T18:32:56Z", "checkOut": null, "done": false, "video": null
        },
        {
          "customerId": 5, "name": "Pinecone Bakery LLC", "lastName": "",
          "address": "9 N Market Way", "cityLine": "Sampletown, CA 90000",
          "lat": null, "lng": null, "appointmentIds": [50], "services": [],
          "order": 2, "checkIn": null, "checkOut": null, "done": true,
          "video": {"id": "aaaa", "state": "sent", "byYou": true}
        }
      ],
      "videos": [
        {"id": "aaaa", "customerId": 5, "state": "held", "emailAt": "2026-11-13T18:50:00Z"}
      ]
    }
    """
}
