# Zest Visit Video

A tech records one short video per property visit (one per tech: when two techs
each have work at a property the same day, each sends his own). The customer gets an email
with a link to watch it. Built from the Visit Video build brief (Nov 12, 2026).

| Piece | Where | What it does |
| --- | --- | --- |
| iPhone app | `ios/` | Pick the stop, record 15 seconds, one tap to save. Uploads by itself. |
| Video server | `src/zest_video/` | The only thing the app talks to. Holds the FieldRoutes keys, tracks every video, sends the email, serves the watch page and the office page. |
| App ↔ server contract | [`API.md`](API.md) | Every call the app makes. |
| Owner's checklist | [`GO-LIVE.md`](GO-LIVE.md) | What is left before techs can use it. |
| Apple submission | [`APP-REVIEW.md`](APP-REVIEW.md) | The usual rejection reasons, where we stand, and what to type in App Store Connect. |
| Tech's one-page guide | [`TECH-GUIDE.md`](TECH-GUIDE.md) | Hand this to the techs. |
| Pictures of the app | [`screens/`](screens/00-contact-sheet.png) | Every screen, photographed from the real app by CI at App Store size. |

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
- Recording again inside the 10 minutes replaces that tech's old video by itself. After
  the email has gone out the stop is locked for him; the office can pull or move it.
- The office can leave a note on any row of the day page (a stop with no video, or a
  video). Notes are kept in the audit trail with who and when; the row shows the newest.
- One video per tech, per property, per day. Two techs with appointments at the same
  property each send their own video, tied to their own appointments; the customer gets
  one email per video, naming the tech and the service. A missing video is flagged for
  the tech who owes it. A helper's video covers the stop for the lead.

## What the server checks by itself

| Check | When | Result |
| --- | --- | --- |
| Stop finished, no video | 60 min after FieldRoutes shows it done, and only for techs signed in on a phone | Office flag. Clears itself if the video arrives late. |
| Video stuck on a phone | 30 min, then 3 hours | Push nudge to the tech, then an office flag. |
| Recorded outside the check-in time | When the email is sent | Office flag. Never blocks. |
| Tech sent past a two-keys warning | At register | Office flag and an audit entry. Never blocks. |
| Customer has no usable email | When the email would go | Link saved as a FieldRoutes note, office flag. |
| FieldRoutes write failed | After 8 tries over a day | Office flag. Writes wait in an outbox meanwhile. |
| Tech no longer active in FieldRoutes | Daily, and at login | App locks. |

## Rolling it out to one tech first

Nothing has to be switched for a pilot. A tech counts only once he has signed
in on a phone: until then his stops are not flagged, not counted as missed, and
show "Tech has no app yet" on the office page.

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
  work hours once any tech has the app, and when an app or the office page asks
  with a cache older than 3 minutes), plus one `customer/get` only for
  customers not seen yet that day.
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

## Where the build differs from the brief, on purpose

Each of these was the owner's later call or a choice made while building. An
independent read of the brief against the code (Nov 13) listed them; they are
kept here so nobody "fixes" one by accident.

| Brief | Built | Why |
| --- | --- | --- |
| Keep, Retake, Send | One question, "Save this video?", then it uploads by itself | Owner: one prompt only |
| A second video asks "replace?" | Recording again replaces the tech's own video with no question; delete for 10 minutes only | Owner |
| Prompts: name or street, did, saw, recommend | "Hi {name}", "Here's what I did", "Here's what I recommend", "I closed the backyard gate" | Owner: say the name only |
| Code by text and email | Text first (Quo), email as a fallback | Owner: techs do not read email |
| Zest colours in the tech app | The look of the field app the techs already use; customer pages and emails stay Zest | Owner |
| Location: While Using, then Always | While Using only; the leave-a-stop reminder is built but off | App Review questions "Always" |
| The email goes a set time after upload | The 10 minutes run from the recording; a late upload goes about a minute after it lands | So the tech's undo time is the same whatever the signal |
| "Not my home": one tap | Two taps (open, then confirm) | One stray tap would kill a customer's own video |
| No stop button | A tap on the ring ends the clip early, but not in the first 3 seconds | A tech who is done at 9 seconds should not film his boots for 6 more |
| Two keys must agree | On a stop with no map pin and no check-in, the spoken name alone is enough | Nothing else can be read there; warning on every such stop would teach techs to ignore warnings |
| Search by name or street | None; with no location the whole route is listed in order | "No typing after setup" |
| Add a tech | Techs come only from FieldRoutes; the office page has "Refresh the list" | FieldRoutes is the source of truth |
| Short-lived upload link | The link lasts a day so a slow upload can finish; the server checks the file is still the confirmed one before any email and before every play, and pulls it if not | A weak-signal upload must not die at the 15-minute mark |
| Sales task category 11 | The office's own Task Category ID | The live API refuses the brief's IDs |

## What is proven and what is not

Proven:

- Server: the test suite (phone API, watch page, office page) passes on SQLite
  and on Postgres. The deployed server passed a live run with the demo account:
  sign in, register, upload a real file to the real bucket by signed URL,
  confirm. The bucket refused an upload with the wrong content type.
- URL signing matches Amazon's published SigV4 example.
- App: compiles and passes its unit tests on Xcode 16 and Xcode 26, and CI
  launches every screen in the iPhone simulator and photographs it.
- App, end to end in the simulator: 84 UI tests tap through every screen and
  button against a fake server and check both the screen and what the server
  received. They cover sign-in by text code, the route list, record → "Save
  this video?" → SAVE, record again, delete inside the 10 minutes, the
  two-keys warning, double taps, and the failures: the app killed in the
  middle of a recording (the clip is recovered and offered on relaunch),
  killed on the review screen, killed in the middle of an upload, killed
  before the server confirmed; server errors, no connection, an expired
  upload link, being signed out with a video queued. In every case exactly
  one video reaches the server and none is lost. A real background-session
  upload also arrives in a signed simulator build. The logic
  package (stop ranking, spoken-name matching, two-keys rule, queue state
  machine, 10-minute window, phone-number formatting) has its own test suite
  that also runs on Linux.

- Server under stress (`scripts/video_load_test.py`, on Postgres): 200 techs
  filming 15 stops each, all at once and every request sent twice. 3,000
  videos, 3,000 emails, exactly one per customer, 73 FieldRoutes reads for the
  whole day. One request alone takes about 14 ms to register and 4 ms to serve
  the watch page.
- Server against hostile input (`tests/test_video_hostile.py`): garbage in
  every field, broken bodies on every endpoint, script and SQL text in names,
  comments and sign-in fields, one tech reaching for another's video, office
  pages without a session, malformed FieldRoutes records, clock changes and
  midnight. Nothing crashes, leaks or crosses over.

- The brief's match spike, on real data (read-only, Nov 10 to 12, 2026: 4 techs,
  10 tech-days, 106 stops, every one with a check-in position and a map pin).
  Ranking a tech's stops by distance from where he checked in, the true stop was
  the nearest 98% of the time and in the nearest three 99%; against only the stops
  not yet visited, 106 of 106. Both misses were a tech checking the next customer
  in while still at the previous house, not GPS error. At the house, the check-in
  position lands a median 24 m from the pin (95th percentile 57 m), and anything
  from 100 to 200 m warns on the same stops, so the 150 m "you look far" line fits
  this office. Next-door customers are rare here (one pair within 30 m in ten
  tech-days) but real, which is why GPS orders the list and never picks. The
  check-in TIME is the weak key: two techs tap in and out within a minute at a
  fifth of their stops, six stops were left checked in for over 90 minutes, and
  with 20 minutes of slack a video at the right stop also fits a neighbouring
  stop's window about half the time. A time mismatch means something; a time
  match says little.

Not proven until it runs on real phones with real accounts:

- Recording, background upload and speech-to-text on a physical iPhone. The
  simulator has no camera, so the UI tests use a stand-in recorder that writes
  a real clip; the camera and the encoder themselves have never run. Also not
  reachable in a simulator: iOS pausing the app during a background upload,
  Low Power Mode, a force-quit by the user, and how well speech recognition
  hears a name outdoors. The brief's upload spike (airplane mode, Low Power
  Mode, app swiped closed, restart) still has to be run on a device.
- The TestFlight job (needs the Apple Developer account).
- Sending through Resend, the bounce webhook's payload, texts through Quo and
  push through APNs. Each is built to the provider's documentation and has
  never run with real keys.
- `review/create` against the live account. `note/create` and `task/create`
  use the same params the MCP server already verified live.
- Whether `emailReminders = 0` in FieldRoutes is a fair stand-in for "opted
  out". It is a setting; if it hides too many customers, turn it off.
