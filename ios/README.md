# Zest Video (iPhone app)

A tech records one short video per stop. The app saves it on the phone, sends
it by itself, and deletes it the moment the server confirms. The server
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
                         contact_sheet.py and is_blank.py.
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
- **AppModel** is the one router. A screen is a value of `route`; there is no
  navigation stack to swipe back through.
- **A video's life**: record to `.mov` → re-wrap as a fast-start `.mp4`
  (H.264 + AAC, about 5 MB) → saved as a *draft* → the tech taps SAVE → a
  queue row is written to disk → `POST /v1/videos` → background upload
  straight to the bucket → `POST /complete` → only then is the file deleted.
  Each step is a `QueueEvent`; the queue file on disk is the truth, so a
  crash, a restart or a day with no signal loses nothing.
- **Uploads** run in a background `URLSession`: they carry on with the app
  closed, on cellular, and the app is woken to tell the server when one ends.
- **Phone storage**: videos live in the app's private folder (never Photos,
  never iCloud backup). The app checks free space before recording, shows
  what it is holding on *My videos*, and sweeps up stray files.

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

runs the logic tests anywhere, Linux included. The app's own tests run in
Xcode (⌘U) or in CI.

## UI tests

`ZestVideoUITests/` drives the real app in the simulator with XCUITest: real
sign-in, real storage on disk, the real upload queue, real screens. Every
test starts from a clean app and a clean server, and checks **both** what is
on the screen and what the server got.

**The server** is `Scripts/fake_server.py` (standard-library Python). It
speaks `docs/video/API.md`, keeps everything in memory, and can be told to
misbehave: answer 500, drop the connection, sign the phone out, ask for an
update, expire an upload address, say "not uploaded", or answer slowly. The
list of switches is at the top of the file. It checks itself with:

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

`-ZestUITestReset 1` wipes what the app has stored. With `ZEST_TEST_TOKEN` in
the environment that clean install starts already signed in (the fake server
issues the token when a test resets it), so only the sign-in tests type a
number and a code. A test that kills the app starts it again *without* the
reset: what the app kept is what is being tested.

**What is covered** (61 tests):

| Class | What it proves |
| --- | --- |
| `SignInUITests` | the number and the code typed one key at a time arrive whole and are sent once; a country code in front; too short and too long; wrong and right code; the loading line right after sign-in; email instead and back; a new code; both ways back; Terms and Privacy; sign out with its question |
| `RouteUITests` | header counts, nearest stop first, both tabs, locked stop, WRONG STOP, the far-away warning |
| `RecordUITests` | record → SAVE → one register, the whole file once, one complete; FLIP, BACK, ending early; RECORD AGAIN; the recommend switch; the two-keys warning both ways; change and delete inside ten minutes; not-on-my-list; double taps |
| `KillUITests` | the app killed mid-recording, on review, mid-upload, before "complete" is answered, and right after SAVE: after it is started again there is exactly one video on the server |
| `NetworkUITests` | 500s, no server, an expired upload address, "not uploaded", signed out by the server, update required, a stop locked by the server, starting with no signal; and a long day: twelve videos saved with no server, all arriving once and in order when it is back |
| `BackgroundUploadUITests` | informational: what the simulator does with the real background upload session. Skipped, with the recorded error, if the upload never arrives |
| `ScreensUITests` | every screen, opened with made-up data: its main actions are on the screen, tappable and not overlapping, nothing runs off the side, and Apple's accessibility audit (contrast, 44-point tap targets, clipped text, descriptions, Dynamic Type) finds nothing unexplained. Also the route of awkward names, where nothing may be cut off. Run by the `sizes` jobs on several simulators and text sizes |
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
what was on the screen, every request the server got during that test, and
what the app said about its uploads. It also saves a picture of the screen.
In CI the block is in the step "What failed, and what the server saw", and
the pictures, the logs and the result summary are in the `ui-tests-a` /
`ui-tests-b` artifacts (a few MB: no screen recordings, no result bundle).

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
- how well speech recognition hears a name outdoors.

Those need a real phone.

## CI

`.github/workflows/ios.yml`:

| Job | Runs on | Does |
| --- | --- | --- |
| `core` | Linux | `swift test` for ZestCore |
| `app` | macOS | generates the project, builds for an iPhone simulator, runs the unit tests |
| `screens` | macOS | takes a picture of every screen in the simulator; download the `screens` artifact |
| `ui-tests` | macOS, jobs `a` and `b` side by side (plus `c`, informational) | starts the fake server, runs the UI tests in the simulator; on a failure, the log names the test, the assertion and what the server saw |
| `sizes` | macOS, three jobs side by side | pictures of every screen on the smallest iPhone (plus the awkward names), a 6.1-inch iPhone, an iPad, and at the largest text sizes; then `ScreensUITests` on each. Artifacts `screens-small`, `screens-odd-names`, `screens-mid`, `screens-ipad`, `screens-large-text` |
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
`stopvideo`, `locked`, `queue`, `update`. (`recording` is held still on the
first thing to say, "Hi Jane".)

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
| `screens-large-text` | the big iPhone with the text size at the largest the app follows, and at the very largest accessibility size (which should look the same: the app stops growing there) |

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
every issue": `FAILED` lines fail the test, `known` lines are deliberate and
give the reason (words over the camera picture, which is a drawing here),
and `listed` lines are shown but do not fail.

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
