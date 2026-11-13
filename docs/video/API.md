# Zest Visit Video: app ↔ server API

The contract between the iPhone app (`ios/`) and the video server
(`src/zest_video/`). The app talks to nothing else: not FieldRoutes, and the
bucket only through a signed URL the server hands it.

- Base URL: the server origin, e.g. `https://video.example.com`. HTTPS only.
- Bodies are JSON, `camelCase` keys. Times are ISO 8601 UTC (`2026-11-13T18:04:05Z`).
- Auth: `Authorization: Bearer <deviceToken>` on everything except the two
  `auth` calls below.
- Every request carries `X-Zest-Build: <CFBundleVersion integer>`.

## Errors

Any non-2xx body is `{"error": "<code>", "message": "<plain words for the tech>"}`.

| HTTP | code | App does |
| --- | --- | --- |
| 400 | `bad_request` | Show `message`. A bug; do not retry. |
| 401 | `unauthorized` | Token dead (cut off, tech inactive). Go to Enter Code. **Keep the queue on disk**; it sends after the next login. |
| 409 | `visit_has_video`, `too_late`, `not_uploaded` | See the calls below. |
| 423 | `locked` | Too many wrong codes. Show `message`. |
| 426 | `update_required` | Full-screen "Update the app". Body also has `minBuild`. |
| 429 | `slow_down` | Honor `Retry-After` seconds. |
| 5xx | `server_error` | Retry later with backoff. Never drop a queued video. |

## Login

### `POST /v1/auth/request-code`

```json
{"contact": "9165551234"}
```

`contact` is the mobile number or email on the tech's FieldRoutes record. The
server always answers `200 {"ok": true}` (it never says whether the contact
matched) and sends a 6-digit code to both the number and the email on file.

### `POST /v1/auth/verify`

```json
{"contact": "9165551234", "code": "123456",
 "deviceName": "iPhone", "deviceModel": "iPhone15,2",
 "osVersion": "18.1", "appBuild": 1}
```

`200`:

```json
{"token": "<opaque, store in Keychain>",
 "tech": {"id": 10008, "firstName": "Pedro", "demo": false}}
```

`401 {"error": "bad_code", "message": "...", "triesLeft": 3}`. The code works
once, dies after 15 minutes and locks after 5 wrong tries (`423 locked`).

`tech.demo` is true for the App Review account. The app behaves the same; the
server keeps demo videos away from customers and FieldRoutes.

### `POST /v1/auth/logout`

Cuts off this device. `200 {"ok": true}`.

## Today

### `GET /v1/today`

One call gives the app everything it needs for the day. Cache the whole body
on disk; it is the offline source of truth. Wipe it when `date` is no longer
today.

```json
{
  "date": "2026-11-13",
  "serverTime": "2026-11-13T15:04:05Z",
  "tech": {"id": 10008, "firstName": "Pedro", "demo": false},
  "config": {
    "maxSeconds": 15,
    "holdMinutes": 10,
    "farMeters": 150,
    "minBuild": 1,
    "requireVideo": true,
    "recommendButton": true,
    "leaveReminders": true,
    "prompts": ["Say the name or street", "What I did", "What I saw", "What I recommend"],
    "officePhone": "+19165550100"
  },
  "stops": [
    {
      "customerId": 11753,
      "name": "Jane Smith",
      "lastName": "Smith",
      "address": "123 Main St",
      "cityLine": "Folsom, CA 95630",
      "lat": 38.548,
      "lng": -121.365,
      "appointmentIds": [19782, 19852, 19853],
      "services": ["Mowing", "Edging"],
      "order": 1,
      "checkIn": "2026-11-13T18:32:56Z",
      "checkOut": null,
      "done": false,
      "video": null
    }
  ],
  "videos": [
    {"id": "<uuid>", "customerId": 11753, "state": "held", "emailAt": "2026-11-13T18:50:00Z"}
  ]
}
```

- A **stop** is one customer on one day. All of that customer's appointments
  for the day are folded into it: one video per property per visit.
- `lat`/`lng` may be `null` (no map pin). `lastName` may be empty (a business).
- `checkIn` is the earliest check-in across the stop's appointments, `checkOut`
  the latest check-out, `null` until it happens. `done` is true when every
  appointment is completed.
- `stop.video` is `{"id", "state", "byYou"}` when any tech has a live video for
  the stop, else `null`.
- `videos` lists this tech's own videos for the day.
- The phone holds only what is in this body. No phone numbers, emails, billing
  or notes are ever sent to it.

## Videos

The **video ID is made on the phone** (lowercase UUID v4) before any network
call. Every call below is safe to repeat with the same ID.

### `POST /v1/videos` — register

```json
{
  "id": "0b0e7c1e-7c35-4f0b-9c50-0f7f6f0f3f11",
  "stopDate": "2026-11-13",
  "customerId": 11753,
  "appointmentIds": [19782, 19852, 19853],
  "notOnMyList": false,
  "recordedAt": "2026-11-13T18:40:02Z",
  "durationSec": 14.6,
  "sizeBytes": 5012345,
  "location": {"lat": 38.5481, "lng": -121.3656, "accuracyM": 8},
  "signals": {"spoken": "match", "gps": "match", "checkIn": "unknown"},
  "override": false,
  "recommend": false,
  "spokenText": "This is Pedro at the Smith home on Main Street..."
}
```

- `notOnMyList: true` → `customerId` is `null` and `appointmentIds` is `[]`.
  The office matches it; no email goes out until then.
- `signals` are the two-keys results from the phone: each is `match`,
  `mismatch` or `unknown`. `override: true` when the tech sent it past a warning.
- `location` is `null` with no fix. `spokenText` is `null` when speech-to-text
  did not run.
- `recommend: true` when the tech tapped "I recommended something".

`200`:

```json
{"id": "...", "state": "awaiting_upload",
 "upload": {"method": "PUT", "url": "https://...signed...",
            "headers": {"Content-Type": "video/mp4"},
            "expiresAt": "2026-11-14T18:40:02Z"}}
```

A repeat for a video the server already holds returns its current `state`
with `"upload": null`: do not upload again, treat it as done.

**Recording again replaces the old one by itself.** If that customer already
has a video for the day that has not been emailed yet, registering a new ID
retires the old video (`replaced`) with no question asked.

`409 {"error": "visit_has_video", "message": "...", "existing": {"id": "...", "state": "sent", "byYou": true, "techFirstName": "Pedro"}}`
only when the earlier video has already gone to the customer. The stop is
locked; show `message` (it tells the tech to call the office).

### Upload

`PUT` the file to `upload.url` with exactly `upload.headers`, as one
background upload task from the file on disk. `200` means the bucket has it.
A `403` means the URL expired: register again for a fresh one.

### `POST /v1/videos/{id}/complete`

```json
{"sizeBytes": 5012345}
```

The server checks the object in the bucket (exists, size matches). `200`:

```json
{"id": "...", "state": "held", "emailAt": "2026-11-13T18:50:02Z"}
```

**Only after this `200` may the app delete the file.** `409 not_uploaded`
means the bucket does not have the whole file: upload again.

### `DELETE /v1/videos/{id}`

The tech's undo. `200 {"id", "state": "deleted"}`. Allowed until the email
goes out, which is `config.holdMinutes` (10) after the recording; then
`409 too_late` and only the office can pull it.

The app shows Delete and Record again for `holdMinutes` after `recordedAt`
and hides them after that. `emailAt` is the server's word on when the window
closes: `recordedAt + holdMinutes`, or about a minute after a late upload
whose window has already passed.

### Video states

| State | Meaning | Queue screen shows |
| --- | --- | --- |
| `awaiting_upload` | Registered, file not confirmed | Waiting / Sending n% (from the phone's own task) |
| `held` | Server has it; email goes at `emailAt` | Sent |
| `sent` | Customer emailed | Sent |
| `watched` | Customer opened it | Sent |
| `no_email` | No usable customer email; office flagged | Sent |
| `unmatched` | Not-on-my-list, waiting for the office | Sent |
| `pulled`, `deleted`, `replaced` | Gone | Removed |

## Device and audit

### `POST /v1/devices/push`

```json
{"apnsToken": "<hex>", "sandbox": false}
```

### `POST /v1/events`

Audit trail from the phone, sent in batches when there is signal.

```json
{"events": [
  {"type": "override", "at": "2026-11-13T18:41:00Z",
   "videoId": "<uuid or null>", "customerId": 11753,
   "detail": {"signals": {"spoken": "mismatch", "gps": "match", "checkIn": "unknown"}}}
]}
```

`type` is one of `record_start`, `record_stop`, `retake`, `draft_discarded`,
`far_warning`, `override`, `two_keys_warning`, `storage_warn`,
`storage_block`, `stop_switched`, `permission`.
