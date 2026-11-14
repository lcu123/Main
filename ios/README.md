# Zest Video (iPhone app)

A tech records one short video per stop: one video per tech, per property,
per day. The app saves it on the phone, sends it by itself, and deletes it
the moment the server confirms. The server
contract is in [`docs/video/API.md`](../docs/video/API.md); the app talks to
nothing else.

Built for a 75-year-old in gloves in bright sun: very large type, full-width
buttons, one question per screen, no swipes, no menus, no typing after setup.
Every size in the app comes from one table in `UI/Theme.swift`, written for a
430 pt wide phone and scaled down gently for narrower ones.

The look is the one the techs already work in all day in their route app: a
light grey page, white cards with soft shadows, one strong blue for anything
that can be tapped, small grey capital labels, a white top bar with the
screen's name and a back arrow, and a tab bar on the two home screens (My
Route, My Videos). Customers are listed last name first ("Smith, Jane"). It is
a familiar style, not a copy: every symbol is a system symbol, the typeface is
the system one, and the only artwork is Zest's own icon. Red is still used for
one thing only, recording, and the record screen (the red button, the border,
the draining ring) is Zest's own.

Sign-in is a mobile number and a texted 6-digit code (email is the other
choice on the same screen). Terms and Privacy open the server's `/terms` and
`/privacy` pages, from the sign-in screen and from My videos.

## Layout

```
ios/
  project.yml            XcodeGen spec. The .xcodeproj is generated, never committed.
  Config/App.xcconfig    Server URL, team ID, version numbers.
  ZestCore/              Swift package: all the logic, no UI. Builds and tests on Linux.
  ZestVideo/
    App/                 Entry point, app delegate, AppModel (state + router).
    Services/            API client, Keychain, disk stores, upload manager, camera,
                         speech check, location, notifications, storage guard.
    UI/                  Theme, big controls, the screens, the countdown ring.
    Resources/           Assets (icon, colors), privacy manifest, entitlements.
  ZestVideoTests/        Unit tests that run on the simulator.
  ZestVideoUITests/      End-to-end tests: the real app, driven in the simulator.
  Scripts/               fake_server.py (the UI tests' server); for the pictures:
                         pick_simulator.py, take_screens.sh, screens_job.sh,
                         contact_sheet.py, is_blank.py and measure_contrast.py.
```

No third-party code. Apple frameworks only, system font, one bundled image
(the icon).

## How the pieces fit

- **ZestCore** holds everything that can be wrong in a subtle way, as pure
  functions with tests: the wire models, which stop to suggest
  (`StopRanker`), the spoken check (`SpokenMatcher`), the two-keys rule and
  the 10-minute undo window (`TwoKeys`, `HoldWindow`), the upload queue state
  machine (`QueueMachine`), the storage thresholds and the ring's clock maths.
- **What the tech says** is four short lines, shown one at a time while the
  camera rolls: "Hi {name}", "Here's what I did", "Here's what I recommend",
  "I closed the backyard gate". The server sends them; the app puts the
  customer's first name in place of `{name}` ("Hi Jane"; a business goes by
  its name; with no name it is plain "Hi") in `PromptText`. He says the
  name and never the street. The spoken check listens for the first name
  (2 points) or the last name (3); the street and house number are no
  longer asked for but still count if he happens to say them. Two customers
  with the same first name tie, and a tie is not a warning. A first name
  that is also an everyday word ("Will", "Rose") only counts right after
  "Hi", a name of one or two letters never counts alone, and the tech's own
  first name is not taken for a customer's.
- **One video per tech, per property, per day** (`StopCover`). Two techs at
  one property each send their own and the customer gets both. A tech's
  second video replaces his own first and nobody else's, so RECORD AGAIN
  and DELETE VIDEO are only ever offered on his own video. The server shows
  a tech his own video for a stop, or none. Only where he has no
  appointment of his own (he is helping) is he shown a teammate's: the card
  says DONE BY TEAM, the stop counts as covered, and its screen says
  "Another tech already recorded this stop. You can record your own too.
  The customer gets both." with RECORD VIDEO, which is a plain new
  recording.
- **Which stop is he at?** (`StopRanker`) An open check-in puts a stop first
  only when it is believable now: with a GPS fix the phone has to be at the
  stop (the far-away distance), with none the check-in has to be under 90
  minutes old. Otherwise the nearest stop by GPS comes first. With no
  usable location: a stop he completed within the last hour and has not
  filmed ("JUST FINISHED": he is standing there), then the stops still to
  do in route order, then the ones finished longer ago. Three real days of
  this office's check-ins showed why: stops left checked in for hours, and
  a fifth of stops checked in, out and completed within a minute, twenty
  minutes before the work there was done.
- **Two keys** (`TwoKeys`): the spoken name, the GPS and the check-in time.
  Two must agree, and one of the two has to be the name or the GPS: the
  check-in only supports (10 minutes of slack; a check-in left open over 90
  minutes says nothing). One thing overrides it all: if the words named
  ANOTHER stop on today's list (`SpokenMiss.otherStop`), the video never
  goes without asking, whatever the GPS and the check-in say: that is the
  wrong house next door, which both of those can agree with. A mismatch
  because no name was heard at all (`.noName`) is only a missing key. The
  audit events say which it was. Every combination is a row of a table in
  `CheckInSignalsTests`.
- **Large text.** At the two largest text sizes a page can be taller than
  the room above its buttons and scrolls. Then the customer's card is drawn
  smaller and, on the screens with a message, the words come before the
  card, so what has to be read is in view as the screen opens. The colours
  of words are held to 4.5 to 1 or better on every ground they sit on
  (`AppSourceRulesTests` reads them from `Theme.swift`).
- **AppModel** is the one router. A screen is a value of `route`; there is no
  navigation stack to swipe back through.
- **A new screen ignores touches for 0.4 seconds** (`SettleFirst`), so the
  second tap of a gloved double tap cannot answer the next question. The
  shield opens once and never closes again while that screen is up.
- **The clip on "Save this video?"** is a small view of its own
  (`ReviewClip`). When it reaches its end only the clip is drawn again
  (TAP TO PLAY AGAIN), not the page and its two buttons: that is the very
  moment a tech taps SAVE or RECORD AGAIN.
- **A video's life**: record to `.mov` → re-wrap as a fast-start `.mp4`
  (H.264 + AAC, about 5 MB) → saved as a *draft* → the tech taps SAVE → a
  queue row is written to disk → `POST /v1/videos` → background upload
  straight to the bucket → `POST /complete` → only then is the file deleted.
  Each step is a `QueueEvent`; the queue file on disk is the truth, so a
  crash, a restart or a day with no signal loses nothing.
- **Uploads** run in a background `URLSession`: they carry on with the app
  closed, on cellular, and the app is woken to tell the server when one ends.
- **Phone storage**: videos live in the app's private folder (never Photos,
  never iCloud backup). The app shows what it is holding on *My Videos* and
  sweeps up stray files.
- **Free space** is checked on every way into the camera, by one function
  (`AppModel.enterCamera`): THIS IS IT, RECORD for a stop not on the list,
  and both RECORD AGAIN buttons. Under 500 MB the tech is warned and can
  RECORD ANYWAY; under 200 MB the camera is not reached, and nothing is
  deleted on the way (RECORD AGAIN throws the old clip away only once the
  new one can be recorded).
- **"Do not delete this app."** While any video is still on the phone
  (waiting, sending, or recorded and not saved), My Route's waiting line
  and the top of My Videos say so (`KeepApp`): deleting the app would lose it.
- **The draft stays** (`DraftRule`). A clip recorded and not saved is never
  thrown away because the day changed. The next time the app is opened the
  tech gets the one question for it, with the customer and the day it was
  filmed: SAVE or DELETE VIDEO (RECORD AGAIN only for a clip filmed today).
  It is checked against the stop as it was that day, which the draft
  carries, and sent for that day; it is not today's video for the same
  customer. Only after 14 days, when the server would refuse it, is it
  dropped by itself, and `draft_discarded` is logged.
- **Not on my list** goes to the office, not to a customer: the server
  answers `unmatched` with no email time. The app says SENT TO OFFICE and
  never promises an email (`VideoWords`); the ten minutes to change or
  delete it run from when it was filmed.
- **The gloves rule** (`GloveRule`): main buttons at least 76 pt tall and as
  wide as the screen less its gutters, round controls at least 120 pt,
  nothing to type after sign-in. `Theme` is built from these numbers and
  `ScreensUITests` measures the real screens against the same ones.

## When the day goes wrong

Three things happen on a real route. For each: nothing is lost, the app is
never stuck, and the tech is told in one plain line.

### A call (or anything else) stops a recording

When a call rings, Siri or an alarm takes the microphone, the app leaves
the screen (home, the lock button, an answered call) or another app takes
the camera, iOS interrupts the camera and ends the clip itself. The file is
written in one-second pieces, so what was filmed up to then is there.
`CameraService` listens for the interruption (it did not before), says what
took the camera (`RecordingCut`), and `RecordingRule` (ZestCore) decides
what happens to the clip:

| The clip | What the tech sees |
| --- | --- |
| ran to the limit, or he tapped the ring | "Save this video?", as always |
| cut short, 3 seconds or longer | "Save this video?" with one line: "A call or alarm stopped this video early." He chooses SAVE or RECORD AGAIN |
| cut short under 3 seconds | the camera screen, with "A call or alarm stopped the video too soon. Please record it again." and the same big button |

Three seconds is the same three seconds before which he cannot end a clip
himself. While the phone has the camera the record button waits and the
screen says "The camera is off during a call. It comes back by itself." It
does: when the interruption ends the camera restarts with no touch.

The phone does not say whether it took the microphone for a call,
FaceTime, Siri, an alarm or a timer, so the app says "a call or alarm".
Telling them apart would mean linking Apple's call framework, **which this
app must never do**: Apple does not allow it in the China mainland store,
App Store Connect offers every country by default, and an app that links
it can be turned away in review. `AppSourceRulesTests.testCallKitIsNeverLinked`
fails if it ever appears in the sources, the project file or the
entitlements. What the phone does name is said exactly: "Another app took
the camera.", "The phone ran out of room.", "This video stopped early when
the app closed."

Also here: the red frame and the ring come down the moment the camera
stops, whatever the reason; a clip being saved as the phone locks asks iOS
for the time to finish, and if that is not enough the camera's own file is
kept and saved on the next open (three tries, then it says the video did
not save); and if the camera ever fails to report the end of a recording,
the app ends it four seconds after the time is up and offers what is on
disk (`RecordingRule.isOverdue`). `record_stop` in the audit trail carries
how long the clip ran, what ended it and whether it was kept.

Control Centre, the notification shade and a low-battery alert do not stop
the camera: the clip runs on and the ring keeps true time.

### The office changes the route

The phone asks for the day's list again when the app comes to the front,
after every video that reaches the server (or is refused, or deleted), when
a notification arrives, and every two minutes while My Route is showing
(every 15 seconds while it has no list). It also asks as My Route comes on
screen and when a stop is tapped, if the list was last asked for more than
half a minute ago (`AppModel.refreshTodayIfStale`); the stop opens at once
from the list the phone has, and the fresh one follows. There is no
pull-to-refresh: no swipes. (The server's own copy can be three minutes
behind FieldRoutes.)

- **A stop added** appears at the next look, ready to record. **A stop
  taken off** disappears; **put back**, it returns, with its video if it
  had one. A video already made is never touched by a change of route.
- **The stop he has open** keeps its screen. Confirm, camera, "Save this
  video?" and the stop's video screen all work from the stop as it was
  when he picked it. If it has left the list while "Is this the stop?" is
  up, one line says "This stop is no longer on your list. You can still
  record it." (the work may be done already).
- **A video for a stop that has left the list** (filmed from a stale list
  with no signal, or while the office was changing it) is sent exactly as
  it was filmed: the customer he picked, the appointments the list had.
  The server takes it and the office decides what becomes of it
  (`docs/video/API.md`). It stays on My Videos under the customer's name,
  with the stop's address, though the stop is off the list.
- **A video the server holds for the office** (`withOffice: true` on the
  video in the day's answer: the visit left the day after it was filmed,
  and no email went). The row says SENT TO OFFICE, not SENT ✓; its screen
  says "The office is looking at this video. Call the office to change
  it."; nothing says it went to the customer and nothing is offered that
  the server would refuse. When the server stops saying so (the office
  sent it, or the stop is back on the route) the row says SENT ✓ again.
  A server that never sends the field changes nothing: a plain `no_email`
  still reads SENT ✓.
- **If the server says no to that stop** (`400`, "That stop is not on the
  list for that day. Use Not On My List.": a customer who was never a stop
  that day, or a server from before the rule above), the app does what the
  answer says, by itself: the same clip, under the same ID, goes to the
  office as not-on-my-list (`QueueEvent.refused`). The row says SENT TO
  OFFICE under the customer's name, My Route says "Smith, Jane is no
  longer on your list. The video goes to the office.", and `upload_failed`
  in the audit trail says why and names the customer he picked. (Before,
  the app asked again every five minutes for ever and the row said
  WAITING.)
- **A video the server will not take at all** (a second `400`) stays on
  the phone. Its row says NOT SENT · CALL OFFICE, its screen says "This
  video could not be sent. It is safe on this phone. Please call the
  office.", and it is offered again once an hour and each time the app is
  opened, never every few seconds.
- **RECORD AGAIN on a video whose stop has left the list** is still for
  the same customer and replaces that video. (It used to become a
  not-on-my-list recording, which replaced nothing.)
- **Never the wrong customer.** A video carries the customer the tech
  picked and confirmed, by number, from the moment he picks it. A list
  that changes afterwards cannot change that; the tests check that no call
  ever names another customer.

### The route is driven in another order

Nothing in the app depends on the order in FieldRoutes. The list puts the
stop he is standing at first (an open check-in that is believable, else
the nearest by GPS); stops with a video drop to the bottom; every stop is
always on the list. Only with no location and no check-in does the planned
order show, as "Next stop", and then a stop he has just completed still
comes first.

**The number on a card** (`RouteNumbers`) is the stop's place on his own
route as the office planned it, counting from 1. It is the stop's label,
not where the card sits and not an instruction: a tech working back to
front sees 9 on top, then 8. Filming, checking in or finishing a stop
never changes a number. When the office takes a stop off, the later stops
move up by one, as they do in FieldRoutes. A stop with a video shows its
state in the badge instead of its number. VoiceOver reads the number
after the rest of the card ("Stop 3").

Skipping a stop and coming back, doing the last stop first, two customers
at one address (both are offered; the name he says tells them apart) and
going back to a finished stop are each a test in `RouteDayTests`.

## Build and run

Needs a Mac with Xcode 16 or later and [XcodeGen](https://github.com/yonaskolb/XcodeGen).

```bash
brew install xcodegen
```

```bash
cd ios && xcodegen generate && open ZestVideo.xcodeproj
```

Set your team under *Signing & Capabilities* (or put the Team ID in
`Config/App.xcconfig`), pick an iPhone, and run. The simulator has no
camera: the record screen says so instead of crashing, and everything else
works.

Point the app at a server by changing `ZEST_SERVER_URL` in
`Config/App.xcconfig` (write `https:/$()/host`; a plain `//` starts a comment
in that file).

## Tests

```bash
cd ios/ZestCore && swift test
```

runs the logic tests anywhere, Linux included (295 tests). Among them are
rules about the app's own source, checked by reading the files
(`AppSourceRulesTests`): no photo-library key or call, no share sheet,
Apple frameworks only, and every pair of colours the screens use has the
contrast to be read in bright sun (the colours are read from `Theme.swift`).

The app's own tests (27, in `ZestVideoTests/`) run in Xcode (⌘U) or in CI,
inside the app on a simulator: the queue on disk, the wire models, that the
videos folder is out of backup and protected (`FilesTests`), that an
unsaved clip carries what it needs to be checked and sent on a later day
(`DraftStoreTests`), and the settings of the background upload session, so
a later edit cannot quietly make uploads wait for Wi-Fi
(`UploadSessionTests`).

## UI tests

`ZestVideoUITests/` drives the real app in the simulator with XCUITest: real
sign-in, real storage on disk, the real upload queue, real screens. Every
test starts from a clean app and a clean server, and checks **both** what is
on the screen and what the server got.

**The clock.** No test leans on how long the run took. (The twenty-video
test once counted every register call and failed when the run was slow
enough for a seventh try with no signal; it now checks the rule itself.) The practice server
moves its "just finished" stop with the clock, and the tests that take many
steps ask for a 60-minute change window. The few tests that count days
(old clips, the twenty-video day) skip themselves, with a plain message, if
midnight is minutes away.

**The server** is `Scripts/fake_server.py` (standard-library Python). It
speaks `docs/video/API.md`, keeps everything in memory, and can be told to
misbehave: answer 500, drop the connection, sign the phone out, ask for an
update, expire an upload address, say "not uploaded", or answer slowly. The
repository's `tests/test_video_contract.py` plays the same day against it
and against the real server and compares the answers, so it cannot drift
from what a real phone will meet. It
also plays a second tech who already recorded a stop (`/__test/other-tech`),
with the signed-in tech as the lead or as a helper there. And it plays
the office changing the day's route (`/__test/route`): a stop cancelled,
given to another tech, put back, or added, as often as a test wants; a
video for a cancelled stop is taken, as the contract says, or with the
`offList` switch refused (`400`) as an older server refused it; and
`/__test/email-due` plays the moment the email would go, which for a
cancelled stop holds the video for the office (`withOffice`). The list of
switches is at the top of the file. It checks itself with:

```bash
python3 ios/Scripts/fake_server.py --self-test
```

**The app's UI-test mode** (`App/UITestMode.swift`) is simulator-only, like
screenshot mode: the whole file is behind `#if targetEnvironment(simulator)`.
Launched with `-ZestUITest 1`, the app is the real app with these differences:

1. It talks to the test server (`ZEST_TEST_SERVER` in the launch environment).
2. The camera is a stand-in recorder (the simulator has none). It writes a
   real, playable video file where the camera would have, and ends through
   the same code as the real one. The re-wrap to `.mp4`, the draft, the queue
   and the upload are all the real code.
3. Speech-to-text does not run; a test supplies the words it "heard" with
   `ZEST_TEST_TRANSCRIPT`.
4. Uploads go through an ordinary `URLSession`, not a background one. The
   upload manager is the same code line for line; only the session's
   configuration differs. `ZEST_TEST_BACKGROUND_UPLOADS=1` turns the real
   background session on (one test uses it, see below), and then iOS is not
   asked to start the app by itself when an upload finishes (it would start
   without the launch arguments, as the ordinary app pointed at the real
   server).
5. Every upload task's start and end, with the system's error if there was
   one, is posted to the test server, so a failed test's report says why.
6. The stand-in recorder can be interrupted the way a call interrupts the
   real camera: `ZEST_TEST_INTERRUPT_AT` (seconds into the first clip),
   `ZEST_TEST_INTERRUPT_FOR` (how long the camera stays away) and
   `ZEST_TEST_INTERRUPT_AS` (`call_or_alarm`, `camera_taken`...). It
   is also taken away when the app leaves the screen, as the real one is.
   Everything after the hand-over is the real code. `ZEST_TEST_SILENT_CAMERA=1`
   makes it never report the end of a recording.
7. Each change of screen is posted to the test server ("screen review to
   record"), and so are a finger coming down on a big button ("press
   RECORD AGAIN") and any tap the double-tap shield kept. A failure report
   lists them under "the app's screens", so a tap that did nothing can be
   told apart: the app moved, the button felt it and nothing happened,
   the shield kept it, or the touch never reached the app.

`-ZestUITestReset 1` wipes what the app has stored. With `ZEST_TEST_TOKEN` in
the environment that clean install starts already signed in (the fake server
issues the token when a test resets it), so only the sign-in tests type a
number and a code. A test that kills the app starts it again *without* the
reset: what the app kept is what is being tested.

**What is covered** (99 tests):

| Class | What it proves |
| --- | --- |
| `SignInUITests` | the number and the code typed one key at a time arrive whole and are sent once; a country code in front; too short and too long; wrong and right code; the loading line right after sign-in; email instead and back; a new code; both ways back; Terms and Privacy; sign out with its question |
| `RouteUITests` | header counts, nearest stop first, both tabs, locked stop, WRONG STOP, the far-away warning |
| `RecordUITests` | record → SAVE → one register, the whole file once, one complete; FLIP, BACK, ending early; RECORD AGAIN; the recommend switch; the two-keys warning both ways; another customer's name said where the place and the time fit still asks ("It sounded like…"), a forgotten name at the right house does not; change and delete inside ten minutes; not-on-my-list (SENT TO OFFICE, no promise of an email, deletable for ten minutes and not after); double taps; and that the audit events (record, retake, override) really reach the server for the right customer |
| `KillUITests` | the app killed mid-recording, on review, mid-upload, before "complete" is answered, and right after SAVE: after it is started again there is exactly one video on the server |
| `InterruptionUITests` | a call two seconds into a clip: one line on the camera screen, nothing red left, the button waits and comes back by itself, nothing sent, RECORD works again and one video arrives; a call nine seconds in: "Save this video?" with "A call or alarm stopped this video early.", SAVE sends it once, and `record_stop` says `endedBy: call_or_alarm`; RECORD AGAIN while the call is still on waits for the camera and then records; another app taking the camera is said exactly, with no call blamed; the home button mid-clip keeps what was filmed; a camera that never reports the end is ended by the app |
| `RouteDayUITests` | the office changes the route: a stop cancelled while it is open says so in one line, still records, and its video is sent as filmed (the customer he picked, the list's appointments) and taken; the same from a stale list with no signal; against a server that refuses the stop the same clip goes to the office (two register calls, one upload, SENT TO OFFICE, no more calls after); the list follows a stop cancelled, added and put back, once with no touch, and the numbers follow; a filmed stop taken off and put back keeps its video; a video the server holds for the office (`withOffice`) reads SENT TO OFFICE with "The office is looking at this video.", offers no RECORD AGAIN or DELETE, and reads SENT ✓ again once the stop is back; RECORD AGAIN after the stop left the list replaces the video for the same customer; a video the server refuses outright stays on the phone, says to call the office, and goes when the server takes it; and stops filmed out of order each go to their own customer with the numbers unmoved. No call in any of them names a customer the tech did not pick |
| `NetworkUITests` | 500s, no server, an expired upload address, "not uploaded", signed out by the server, update required, a stop locked by the server, starting with no signal; "Do not delete this app" while a video waits, gone once it is sent |
| `LongDayUITests` | the brief's own number: twenty videos saved in a row with no server, all twenty arriving once each and in order when it is back, the phone left holding no files, and My Videos saying WAITING, never SENT, until each one is really there. What it may cost: with no signal only the oldest video is tried, again after 5, 10, 20, 40... seconds, and those tries reach nothing; once the signal is back each video is one register, one upload, one complete, and nothing is asked twice |
| `StorageUITests` | under 200 MB free the camera is never reached: from THIS IS IT, from RECORD for a stop not on the list, and from both RECORD AGAIN buttons, with the clip already made still there to save; under 500 MB one warning, and RECORD ANYWAY reaches the camera |
| `DraftUITests` | with a clip unsaved, tapping another stop opens the question for the clip's own customer; a clip left since yesterday is asked about on the next open (SAVE or DELETE VIDEO, the day it was filmed, no RECORD AGAIN), saved for that day, and does not count for today's stop; a week-old one deleted; only one older than 14 days is dropped by itself, and logged |
| `WithoutLocationUITests` | location refused: the stops show in route order, a video is recorded and arrives once with the GPS key "unknown" and no location, and nothing crashes |
| `TeamUITests` | one video per tech per property. The lead is not shown another tech's video, his own is taken with no 409, and RECORD AGAIN replaces only his own. The helper sees DONE BY TEAM, gets no RECORD AGAIN, DELETE or CALL OFFICE on the lead's video, records his own beside it, and deleting his own leaves the lead's alone and the stop reads DONE BY TEAM again. After a delete the app asks the server for the day again; for the lead the stop simply reopens |
| `BackgroundUploadUITests` | informational: what the simulator does with the real background upload session. Skipped, with the recorded error, if the upload never arrives |
| `ScreensUITests` | every screen (19), opened with made-up data: its main actions are on the screen, tappable and not overlapping, nothing runs off the side; the gloves rule (every main button at least 76 points tall and as wide as the screen less its gutters, the record button and ring at least 120 across, and after sign-in nothing to type, pick, slide or swipe); every page that is taller than its room scrolls every line of it clear of the bars (a single card taller than the room must be readable in two scrolls: its first line and its last each come into view); and Apple's accessibility audit (contrast, 44-point tap targets, clipped text, descriptions, Dynamic Type) finds nothing unexplained. The audit moves the page while it runs and flags rows it saw half under a bar, so a contrast issue on a line of text is settled by scrolling that line whole on to the screen and measuring its pixels, a tap target by its size once whole, and anything else flagged on a whole row fails. The YES / NO switch is looked at in both states. Run by the `sizes` jobs on several simulators and text sizes |
| `OddNamesUITests` | the route of awkward customers (an apostrophe, accents, a hyphen, a 45-letter business, all capitals, an emoji, one word, a unit, a PO box, a 24-letter last name) on every screen that shows a name: nothing cut off, every action still there. Seven tests of a few minutes each (it was one, and on the smallest iPhone it was stopped at fifteen minutes a third short of done). Run on the smallest iPhone by the `sizes` job |
| `PerformanceUITests` | informational: launch time, time to My Route, memory on each screen and over ten videos, what is left on disk. Run by the `performance` job |

Timing a tap to the tenth of a second is not something a UI test does well,
so the "ring only ends the clip after three seconds" rule and "a kill at any
step still ends with one sent video" are also checked as plain logic in
ZestCore (`testRingTapOnlyEndsTheClipAfterThreeSeconds`,
`testAKillAtAnyPointStillEndsWithOneSentVideo`). So is the long day with no
signal (`UploadPlannerTests`): what the upload engine does next is decided
by `UploadPlanner` in ZestCore (oldest video first, one register call at a
time, two uploads at once, no signal holds the line) and the app only
carries it out.

**To run them on a Mac**, with the fake server in one terminal:

```bash
python3 ios/Scripts/fake_server.py
```

```bash
cd ios && xcodegen generate && xcodebuild build-for-testing -project ZestVideo.xcodeproj -scheme ZestVideoUITests -destination "platform=iOS Simulator,name=iPhone 17" -derivedDataPath build/DerivedData CODE_SIGNING_ALLOWED=NO
```

The fake server is plain HTTP on `localhost`, which iOS only allows with one
key in the app's `Info.plist`. The project has **no** such exception, in any
configuration; it is added to the test build only, after it is built:

```bash
/usr/libexec/PlistBuddy -c "Add :NSAppTransportSecurity dict" -c "Add :NSAppTransportSecurity:NSAllowsLocalNetworking bool true" ios/build/DerivedData/Build/Products/Debug-iphonesimulator/ZestVideo.app/Info.plist
```

```bash
cd ios && xcodebuild test-without-building -project ZestVideo.xcodeproj -scheme ZestVideoUITests -destination "platform=iOS Simulator,name=iPhone 17" -derivedDataPath build/DerivedData CODE_SIGNING_ALLOWED=NO
```

The UI tests have their own scheme, so the ordinary build and unit tests
(scheme `ZestVideo`) never need the server.

**When a test fails** it prints one block: the test's name, the assertion,
what was on the screen, every request the server got during that test, the
screens the app went through, and what the app said about its uploads. It also saves a picture of the screen.
Only a test that failed prints a FAILED block; something the system merely
noted during a test that passed (on an iPad, Apple's note about Info.plist
keys) is printed once, as a `NOTE`.
In CI the block is in the step "What failed, and what the server saw", and
the pictures, the logs and the result summary are in the `ui-tests-a`,
`ui-tests-b` and `ui-tests-d` artifacts (a few MB: no screen recordings, no
result bundle).

The tests can also tell the app two things a simulator cannot be made to
do: how much free space the phone "has" (`ZEST_TEST_FREE_MB`), and that the
unsaved clip found at launch is some days old (`ZEST_TEST_DRAFT_AGE_DAYS`).
Both exist only in a simulator build, and what the app then does is its
real code.

CI job `c` is informational. It builds the app ad-hoc signed and runs only
`BackgroundUploadUITests`, to show whether the simulator's background
transfer service works for a signed build. It cannot turn the run red.

**What a simulator cannot show**, and so is not covered here:

- the real camera and encoder (the clip is a stand-in file);
- an upload carrying on while the app is closed. The tests upload through
  an ordinary session, which dies with the app. What they do prove is that
  a kill at any point ends, once the app is started again, with exactly one
  video on the server;
- iOS suspending the app during a background upload;
- Low Power Mode and Low Data Mode;
- a real force-quit by the user (swiping the app away), which iOS treats
  differently from a crash: it cancels the app's background uploads until the
  app is next opened. The tests kill the app the way a crash does;
- how well speech recognition hears a name outdoors;
- iOS itself interrupting the real camera: a real call (declined, answered,
  FaceTime, WhatsApp), Siri, an alarm. The tests interrupt the stand-in
  recorder; that iOS sends the notices the app listens for, with the
  reasons it expects, that the cut file plays, and that the camera
  restarts when the call ends are all untested until a real phone is rung;
- saving a clip while the phone is locked or a call has the screen (the
  export runs in background time iOS may cut short);
- AirPods, CarPlay or a headset joining or leaving in the middle of a clip.

Those need a real phone.

## CI

`.github/workflows/ios.yml`:

| Job | Runs on | Does |
| --- | --- | --- |
| `core` | Linux | `swift test` for ZestCore |
| `app` | macOS | generates the project, builds for an iPhone simulator, runs the unit tests |
| `screens` | macOS | takes a picture of every screen in the simulator; download the `screens` artifact |
| `ui-tests` | macOS, jobs `a`, `b`, `d` and `e` side by side (plus `c`, informational) | starts the fake server, runs the UI tests in the simulator; on a failure, the log names the test, the assertion and what the server saw |
| `sizes` | macOS, four jobs side by side | pictures of every screen on the smallest iPhone (plus the awkward names), a 6.1-inch iPhone, an iPad, and at the largest text sizes; then `ScreensUITests` on each. Artifacts `screens-small`, `screens-odd-names`, `screens-mid`, `screens-ipad`, `screens-large-text`, `screens-largest-accessibility` |
| `performance` | macOS, informational | `PerformanceUITests`, plus the size of a Release build for a phone; prints a table and uploads it as `performance.txt` |
| `testflight` | macOS, manual | archives and uploads to App Store Connect |

## Pictures of every screen

The simulator build has a screenshot mode. Launch it with `-ZestScreen <name>`
and the app opens straight on that screen with made-up data: no sign-in, no
network, no Keychain, no permission prompts. Where the camera or the clip
would be, it draws a simple house on a lawn (the simulator has no camera).

```bash
xcrun simctl launch --terminate-running-process booted com.zestlawnpest.video -ZestScreen recording
```

Names: `login`, `code`, `today`, `today2` (the same list scrolled to the end),
`confirm`, `far`, `record`, `recording`, `review`, `warning`, `saved`,
`stopvideo`, `locked`, `queue`, `update`, `team` (a stop a teammate already
recorded), `lowspace` and `full` (the phone almost full, and full),
`olddraft` (a clip left unsaved since yesterday). That is 19.
(`recording` is held still on the first thing to say, "Hi Jane".)

All of it sits behind `#if targetEnvironment(simulator)`
(`App/ScreenshotMode.swift`, `UI/StandInPicture.swift`, and a few small hooks),
so none of it exists in a build for a real phone. The screens themselves are
the real ones: the mode only hands them their starting state.

The `screens` CI job runs every name on the largest iPhone simulator and
uploads the pictures, plus `00-contact-sheet.png` with all of them on one
page (`Scripts/contact_sheet.py`, standard-library Python).

It does not guess how long a launch takes: in screenshot mode the app writes
a small file once the screen is up, and the job waits for it. A picture that
is still one flat colour (`Scripts/is_blank.py`) is taken again, and the job
warns, with the app's last log lines, if it stays blank.

### Other sizes, larger text, awkward names

The `sizes` jobs take the same pictures on other simulators
(`Scripts/screens_job.sh`, which uses `Scripts/take_screens.sh`):

| Artifact | What |
| --- | --- |
| `screens-small` | the smallest iPhone the newest iOS runs (an SE if there is one; a simulator is made for it if none exists) |
| `screens-odd-names` | the same phone, with a route of awkward customers |
| `screens-mid` | a 6.1-inch iPhone |
| `screens-ipad` | an iPad, where this iPhone-only app runs in an iPhone-sized window, as Apple's reviewer may see it |
| `screens-large-text` | the big iPhone with the text size at the largest the app follows |
| `screens-largest-accessibility` | the same phone at the very largest accessibility size (which should look the same: the app stops growing one step sooner) |

`Scripts/pick_simulator.py` chooses each simulator from what the Mac has, by
its real screen size rather than its name.

The awkward customers come from `-ZestFixture odd`, and `-ZestStop <n>` (0 to
9) picks which of them the screen is about: an apostrophe, accents, a
hyphen, "van der", a 45-letter business, all capitals, an emoji, one word, a
PO box, and a 24-letter last name; a long address with a unit among them.

```bash
xcrun simctl launch --terminate-running-process booted com.zestlawnpest.video -ZestScreen confirm -ZestFixture odd -ZestStop 4
```

After the pictures, each job runs `ScreensUITests` on the same simulator.
Its failures name the screen and the element. Every issue Apple's audit
raises is also printed as a table in the step "Apple's accessibility audit,
every issue": `FAILED` lines fail the test, `known` lines give the reason
they do not, and `listed` lines are shown but do not fail.

**The audit moves the page while it runs.** Put at the end of My Route at
the largest text, it comes back with the page a card and a half further
up; on the warning screen it comes back with the page at its top, every
time. (The table has a `moved` line whenever it happens.) So a row cannot
be "scrolled clear of the bars and audited again": by the time the audit
looks, the row is somewhere else, often half under a bar, and that is when
it says "Contrast failed" about plain dark words on white. Nothing in the
checks relies on the audit seeing a row where the test put it.

**One drag that moves nothing is not a page that cannot scroll.** The
checks drag every page from its top to its end. Once (the odd names on My
Route, opened at its end, on the smallest iPhone) one drag out of 71 moved
nothing: the page had just been dragged from its end to its top in 14
drags, was dragged 56 more times afterwards, and was the same height as in
the run before, which passed. The check said "does not scroll" because its
first step down had moved nothing. Now a drag that moves nothing is made
again; only two in a row mean the page goes no further, the top and the end
of a page are read from where its pieces are (no drag is needed to find
them), and the table gets an `astray` line each time, so the log says how
often it happens.

**Contrast is settled by the pixels.** Apple's audit says "Contrast failed"
with no numbers. So for every line of text it flags, the test scrolls the
line whole on to the screen itself, takes a picture of just that line, and
reads the two colours off it (`ZestCore/PixelContrast.swift`: the most common colour is what the
words sit on; the colour that stands out most from it is the words). 4.5:1
or better, inside the screen, no other text across it: the line is `known`,
with the measured numbers. Fainter, cut off or covered: it fails, with the
numbers. The pixels are kept (`contrast-*.ppm`, and a `.png` to look at),
and `Scripts/measure_contrast.py` reads them again in the job and prints the
table; a line the tests let through that reads under 4.5:1 there fails the
job. To measure a rectangle of any screenshot by hand (points, not pixels):

```bash
python3 ios/Scripts/measure_contrast.py --png 01-login.png --rect 18,225,397,41 --scale 3
```

**"Text clipped" and emoji.** Apple's audit says "Text clipped" about a
line with an emoji in it (the sunflower in one made-up customer's name),
though the line is whole: the emoji comes from another font, which claims
more height than it draws. For such a line the test measures the air round
the drawn text in the line's own frame (`PixelContrast.air`): clear
background above it, below it and at its end, or it fails. Those rows are
in the same table and are read again by `measure_contrast.py`.

**A customer's name, street and town are never squeezed.** The same audit
was right twice. Held to one line, the town on a route card was drawn at
about 80% of its size on the smallest iPhone; held to two, a 45-letter
business name on the review screen at about 90% (and on the camera screen
too, which the audit did not flag). Neither was cut, but one more word and
they would have ended in "…". Now everything that comes from a customer's
record is drawn one way, `customerText()` in `Controls.swift`: at its full
size, on as many lines as it needs; the card or page grows and scrolls.
The one case in which it is drawn smaller is a single word too long for
the line ("Christopherson," in the largest type on the smallest phone):
that word is shrunk, to three quarters at the least, rather than broken in
two. A ZestCore test reads the screens' source and fails if a name, street
or town is given a line limit of its own.

**The clock holds still in screenshot mode.** "7 MIN LEFT" on a card stays
"7 MIN LEFT" for as long as the screen is open, so a check that takes two
minutes is looking at the same words at the end as at the start.

**A test that is stuck is stopped and explained.** Apple's audit does not
always come back. `screens_job.sh` watches each pass: if a running test has
written nothing for 75 seconds it prints `######## STUCK <test>`, saves a
stack sample of the app and of the test runner (the main thread is printed;
the whole sample is in the reports), and closes the app. The test then opens
the screen again and runs the audit once more a little further along the
page; a second time is a failure that says so. A pass that fails with no
failed test prints why (`######## TIMED OUT ...`, or the runner's own
error); a pass whose runner never started is run once more on a simulator
started afresh.

## Ship to TestFlight

One-time setup, in Zest's Apple Developer account:

1. Create the app record in App Store Connect with bundle ID
   `com.zestlawnpest.video`.
2. Create an App Store Connect API key with the **Admin** role
   (Users and Access → Integrations) and download the `.p8` file.
3. Add four repository secrets on GitHub (Settings → Secrets and variables →
   Actions): `APPLE_TEAM_ID`, `ASC_KEY_ID`, `ASC_ISSUER_ID`, `ASC_KEY_P8`
   (the whole text of the `.p8` file).

Then, on GitHub: Actions → **iOS** → Run workflow → tick *Also archive and
upload this build to TestFlight*. The build number is the run number (a
re-run of the same run gets `.2`, `.3`), so an upload is never refused as a
duplicate. Both the build number and the version can be typed in on that
form when a specific one is wanted, for example version `1.0.1` after `1.0.0`
is on sale.

The same archive can be made by hand in Xcode: Product → Archive →
Distribute App → App Store Connect.

Before App Review: set `ZEST_APP_STORE_URL` once the listing exists, and give
the reviewer the demo sign-in (the server marks that account `demo`, so its
videos never reach a customer). Say in the review notes whether the demo
sign-in is a mobile number or an email: the sign-in screen opens on mobile
number, with USE EMAIL INSTEAD underneath.

The app declares no background modes. Pushes from the server are ordinary
visible alerts; uploads continue in the background through a background
`URLSession`, which needs no mode.
