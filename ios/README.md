# Zest Video (iPhone app)

A tech records one short video per stop. The app saves it on the phone, sends
it by itself, and deletes it the moment the server confirms. The server
contract is in [`docs/video/API.md`](../docs/video/API.md); the app talks to
nothing else.

Built for a 75-year-old in gloves in bright sun: very large type, full-width
buttons, one question per screen, no swipes, no menus, no typing after setup.
Every size in the app comes from one table in `UI/Theme.swift`, written for a
430 pt wide phone and scaled down gently for narrower ones.

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
  Scripts/               contact_sheet.py, used by CI to tile the screenshots.
```

No third-party code. Apple frameworks only, system font, one bundled image
(the icon).

## How the pieces fit

- **ZestCore** holds everything that can be wrong in a subtle way, as pure
  functions with tests: the wire models, which stop to suggest
  (`StopRanker`), the spoken check (`SpokenMatcher`), the two-keys rule and
  the 10-minute undo window (`TwoKeys`, `HoldWindow`), the upload queue state
  machine (`QueueMachine`), the storage thresholds and the ring's clock maths.
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

## CI

`.github/workflows/ios.yml`:

| Job | Runs on | Does |
| --- | --- | --- |
| `core` | Linux | `swift test` for ZestCore |
| `app` | macOS | generates the project, builds for an iPhone simulator, runs the unit tests |
| `screens` | macOS | takes a picture of every screen in the simulator; download the `screens` artifact |
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
`stopvideo`, `locked`, `queue`, `update`.

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
