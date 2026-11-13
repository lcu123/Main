# Zest Visit Video

A tech records one short video per property visit. The customer gets an email
with a link to watch it. Built from the Visit Video build brief (Nov 12, 2026).

| Piece | Where | What it does |
| --- | --- | --- |
| iPhone app | `ios/` | Pick the stop, record 15 seconds, one tap to save. Uploads by itself. |
| Video server | `src/zest_video/` | The only thing the app talks to. Holds the FieldRoutes keys, tracks every video, sends the email, serves the watch page and the office page. |
| App ↔ server contract | [`API.md`](API.md) | Every call the app makes. |
| Owner's checklist | [`GO-LIVE.md`](GO-LIVE.md) | What is left before techs can use it. |
| Tech's one-page guide | [`TECH-GUIDE.md`](TECH-GUIDE.md) | Hand this to the techs. |

The server ships in the same Docker image as the FieldRoutes MCP server and
runs as a second Railway service (`zest-video`, start command `zest-video`).
It reuses `fr_mcp.client.FieldRoutesClient`, so auth, wire format, rate limit
and the shared daily API quota are handled once.

## How a video moves

```
phone:   record → SAVE → on disk → register → upload to bucket → confirm → delete local file
server:  awaiting_upload → held (tech's 10-minute undo) → sent → watched
                                                        ↘ no_email (link live, no email: reason kept)
         unmatched (filmed as "not on my list", office picks the customer)
         pulled / deleted / replaced (link dead)
```

- The video ID is made on the phone. Register, confirm and delete are all safe
  to repeat, so a retry on bad signal never makes a second video or a second email.
- Video bytes never pass through the server. The phone uploads straight to a
  private bucket with a signed URL; the watch page plays through
  `/w/<token>/video`, which redirects to a fresh short-lived signed URL.
- The customer link is a random 128-bit token. No login, marked noindex, rate limited.
- Recording again inside the 10 minutes replaces the old video by itself. After
  the email has gone out the stop is locked for the tech; the office can pull or move it.

## What the server checks by itself

| Check | When | Result |
| --- | --- | --- |
| Stop finished, no video | 60 min after FieldRoutes shows it done | Office flag. Clears itself if the video arrives late. |
| Video stuck on a phone | 30 min, then 3 hours | Push nudge to the tech, then an office flag. |
| Recorded outside the check-in time | When the email is sent | Office flag. Never blocks. |
| Tech sent past a two-keys warning | At register | Office flag and an audit entry. Never blocks. |
| Customer has no usable email | When the email would go | Link saved as a FieldRoutes note, office flag. |
| FieldRoutes write failed | After 8 tries over a day | Office flag. Writes wait in an outbox meanwhile. |
| Tech no longer active in FieldRoutes | Daily, and at login | App locks. |

## Safety switches

Nothing reaches a customer or FieldRoutes until someone turns it on.

| Variable | Values | Default |
| --- | --- | --- |
| `VIDEO_EMAIL_MODE` | `off`: no customer email. `test`: every customer email goes to `VIDEO_EMAIL_TEST_TO`. `live`. | `off` |
| `VIDEO_EMAIL_CUSTOMER_IDS` | Only these customers are ever emailed. | unset |
| `FR_WRITES` | `off`: no notes, reviews or tasks are written. | `on` |
| `FR_WRITE_CUSTOMER_IDS` | Only these customers are written to. | unset |

`FR_WRITES` and `FR_WRITE_CUSTOMER_IDS` mean the same thing here as on the MCP
server. Every other variable is listed in [`.env.example`](../../.env.example).
Things the owner can change without a deploy (video length, hold time, alert
timing, email wording, which services need a video) live on the office page
under Settings.

## FieldRoutes use

Reads, sized for the shared 3,000-a-day quota:

- The day's stops: one `appointment/search` per refresh (every 5 minutes in
  work hours, and when an app asks with a cache older than 3 minutes), plus one
  `customer/get` only for customers not seen yet that day.
- `route/search` only on a day where an appointment has no `assignedTech`.
- `employee/search` once a day and when an unknown contact asks for a code.
- One `customer/get` per email sent, to read the address fresh. The server
  never stores customer emails or phone numbers.

Writes, all through an outbox that retries: `note/create` (the link),
`review/create` (the score), `task/create` (tech recommended something).

Checked against a real day (Nov 12, 2026, 44 completed appointments, 4 techs):
`assignedTech` on the appointment matched the route's tech and `servicedBy` on
every one, so stops are grouped by `assignedTech` with the route as fallback.
`employeeID` on an appointment is who booked it, not the tech.

## Run it

```bash
pip install -e ".[dev,video]"
```

```bash
pytest
```

```bash
VIDEO_DATABASE_URL=sqlite+aiosqlite:///./zest_video.db VIDEO_MAIL_BACKEND=log zest-video
```

With no bucket configured the server uses an in-memory stand-in and says so at
startup. `VIDEO_MAIL_BACKEND=log` prints emails, login codes included, to the
log instead of sending them.

The iPhone app: see [`ios/README.md`](../../ios/README.md). CI builds it on
macOS for every push that touches `ios/`.

## What is proven and what is not

Proven:

- Server: the test suite (phone API, watch page, office page) passes on SQLite
  and on Postgres. The deployed server passed a live run with the demo account:
  sign in, register, upload a real file to the real bucket by signed URL,
  confirm. The bucket refused an upload with the wrong content type.
- URL signing matches Amazon's published SigV4 example.
- App: compiles on Xcode 16 and Xcode 26. The logic package (stop ranking,
  spoken-name matching, two-keys rule, queue state machine, 10-minute window)
  has 84 tests.

Not proven until it runs on real phones with real accounts:

- Recording, background upload and speech-to-text on a physical iPhone. The
  brief's upload spike (airplane mode, Low Power Mode, app swiped closed,
  restart) still has to be run on a device.
- The TestFlight job (needs the Apple Developer account).
- Sending through Resend, the bounce webhook's payload, texts through Quo and
  push through APNs. Each is built to the provider's documentation and has
  never run with real keys.
- `review/create` against the live account. `note/create` and `task/create`
  use the same params the MCP server already verified live.
- Whether `emailReminders = 0` in FieldRoutes is a fair stand-in for "opted
  out". It is a setting; if it hides too many customers, turn it off.
