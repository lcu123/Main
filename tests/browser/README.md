# Customer pages in real browsers

The customer side of Zest Visit Video (the watch page, the dead-link page, unsubscribe,
privacy, terms, support, and the email with the button) opened in real browser engines at the
screen sizes customers own. The pytest suite (`tests/test_video_web.py`) proves the server
answers correctly; this proves a customer can actually **watch the video** and **send a score**
on an iPhone, an Android phone, an iPad, a desktop and a TV.

Nothing here changes `src/`. It runs the real server with FieldRoutes, mail and the bucket
faked exactly as pytest fakes them (`serve.py`).

## Run it

```sh
cd tests/browser
npm ci                      # once. Does NOT download browsers.
npx playwright test         # every project this machine has an engine for
```

Python comes from the repo's `.venv` if there is one, else `python3` (override with `PYTHON=`).
The suite starts and stops `serve.py` itself on port 19300 (and 19301 for the stand-in bucket;
override with `BROWSER_TEST_PORT=`).

**On a machine with only Chromium** (this is the usual case outside CI) the config says which
projects it left out and runs the rest:

```sh
npx playwright test --project="Desktop Chrome"
npx playwright test --project="Pixel 7" --project="320x568 small phone"
npx playwright test --project="TV Samsung Tizen 1080p"
npx playwright test --project=static                     # no browser: ES5 and CSS support
npx playwright test specs/expiring-links.spec.js --project="Desktop Chrome"
npx playwright show-report report/html
```

Runs here (Chromium): Desktop Chrome, Desktop Edge, Galaxy S8, Galaxy S24 (and landscape),
Pixel 7, the five hand-made sizes, the three TV projects, and `static`.

**Needs CI** (`.github/workflows/web.yml`), or `npx playwright install firefox webkit` on your
own machine: Desktop Firefox, Desktop Safari, every iPhone and iPad project (WebKit), and real
Safari (macOS only).

`ALL_ROWS_ON=chromium npx playwright test specs/watch.spec.js -g "fits the screen"` lays the
iPhone and iPad sizes out in Chromium. That previews their **layout** (the pictures are named
`PREVIEW-on-chromium--...`). It says nothing about Safari's player.

What a run leaves behind (all git-ignored): `report/html` (the report), `report/known-gaps.txt`,
`report/compat.txt`, `screenshots/<project>.png` (one full-page picture of the watch page per
project, plus a few named ones), and `test-results/` (a trace and a picture of each failure).

## What each spec proves

Every project (every row of the table in `playwright.config.js`):

| Spec | In one line |
| --- | --- |
| `specs/watch.spec.js` | The watch page opens with no error, fits the screen with the player in the first screen, the picture sits whole inside a player as wide as the column (room for the browser's control bar), the video loads through the redirect to the bucket and plays, pauses, seeks and replays, the server hears "played" exactly once, a score is saved once and stays, "This is not my home" takes the video down and a plain GET of its address never does, every control is 44x44 px, no text is under 16 px, and the office number can be read on its button. |
| `specs/other-pages.spec.js` | A dead link and an unknown link show the kind message and the office number; unsubscribe asks once and says "Done" ever after; privacy, terms and support open, fit and read; a link opened more than 120 times in a minute says "wait a minute" (not "no longer available"), and the player's own requests do not use up page opens. |

Only the rows marked `deep` (the three desktop engines, iPhone 13, iPad (gen 7), Pixel 7):

| Spec | In one line |
| --- | --- |
| `specs/noscript.spec.js` | With script switched off the video, the score and "not my home" still work, and a link under the player opens the video by itself. |
| `specs/expiring-links.spec.js` | The 15-minute bucket link has run out before play, on a skip while paused, after a pause: the page fetches a fresh one by itself and the picture carries on from the same second, with one "played". |
| `specs/recovery.spec.js` | A video that will not load: two automatic tries, never more than six on a page, then one note that is a button (one press tries again, a second opens the page afresh); a tap on a dead player is not a viewing; a signal that drops for more than 6 s says so and a 2 s hiccup does not. |
| `specs/slow-link.spec.js` | On a slow bucket, slow page files and (Chromium) a throttled connection, the page is usable at once, nothing jumps when the video arrives, and a play pressed before the script arrived is still counted. |
| `specs/non-faststart.spec.js` | A video whose index is at the END of the file still plays, and how long each browser needs for the index, measured against the same file with the index at the front. |
| `specs/page-weight.spec.js` | The page without the video stays inside its byte budget, and each file is fetched once. |
| `specs/colour-schemes.spec.js` | Light, dark, forced colours and browser "darken websites": text stays readable, a picked star is filled and one that is not is hollow with a 3:1 outline, the thank-you can be read (all measured in pixels). |
| `specs/email.spec.js` | The email's one button is big, fits the screen, opens the video, and carries what classic Outlook needs. |
| `specs/old-engines.spec.js` | The page with the CSS that iOS 12-14 and 2017-2021 TV browsers do not understand taken away: it still fits and works, the player's place is kept before the video loads, the logo keeps its distance, a focused star is outlined. |

Only where there is a keyboard or a remote (desktop engines and TV rows):

| Spec | In one line |
| --- | --- |
| `specs/keyboard.spec.js` | Tab, Shift+Tab, Space, Enter and arrows (or a TV remote's arrows, OK and play/pause keys) reach and work the player, the stars, Send and the "try again" note; the arrows on the stars go the way they point; the focused control is visibly outlined. On a Mac, Safari is driven with Option+Tab, as its users do. |
| `specs/zoom-reflow.spec.js` | At 200% and 400% desktop zoom and iPhone "Page Zoom" 150% and 200%: no sideways scroll, nothing cut off, the player keeps its size, five stars fit inside the card, the form still works. (Desktop engines.) |
| `specs/tv.spec.js` | On a TV: pointer-mode remote plays and scores, full screen works, nothing blocks AirPlay or Chromecast, the picture fills 74% of the screen height on every TV size, text size from 3 metres. (TV rows.) |

No browser (`--project=static`):

| Spec | In one line |
| --- | --- |
| `static/es5.spec.js` | `watch.js` parses as ES5 (old iOS and TV engines run none of a file with newer syntax), uses only listed browser features and listed methods and properties, never trusts `play()` to return a promise; no inline script. |
| `static/compat.spec.js` | Every CSS and browser feature the pages use that some browser on the support list lacks is printed with the versions, and must be in `static/compat-allowlist.json` with a reason. |
| `static/gaps.spec.js` | `lib/known-gaps.js` has no dead or duplicate entries. |

Live mode only: `specs/live.spec.js` (below).

## Known gaps: a green run does not mean nothing is wrong

`lib/known-gaps.js` is the one list of things the pages get wrong **today**, or that one
browser does that the page cannot change. Each entry says what the customer gets and the
smallest fix. While it waits the suite stays green, and:

* anything **new** that goes wrong still fails the build;
* the day a listed bug is **fixed**, its test fails with *"listed in known-gaps.js but the page
  now gets it right"* (or *"expected to fail, but passed"*): delete the entry.

Every run ends by printing the gaps it touched and on which projects (also in
`report/known-gaps.txt`, and in the CI run's summary). Two more lists may follow:

* **TO CONFIRM**: an entry marked `'unverified'` for an engine was written without that engine
  at hand. The run says whether it happens there; put `true` or `false` in the entry.
* **DEPENDS ON THE DEVICE**: an entry marked `'varies'` is reported, never failed. (None today:
  the stars used to be drawn with whatever symbol font the device had; they are drawings now.)

The list on 2026-11-14, after two fix rounds and the first runs in Firefox, in Safari's engine and on a Mac: 5 entries. (The first list had 24; 18 were fixed
or shown not to happen and deleted, and 8 new ones were found on the fixed page. The second
round fixed 6 of those 14: the half-minute wait on an expired link in Chrome, "try again"
counted as watched, the focus lost when the note is pressed, the stars on a browser-darkened
page, the office number sticking out of its button at 200% zoom, and the small script-off link.)

| Entry | Where | In short |
| --- | --- | --- |
| `player-below-first-screen` | windows under 331 px tall (iPhone SE on its side) | The last 6 px of the player are below the first screen. |
| `long-address-pushes-player-down` | windows 331 to 363 px tall | With a two-line heading the last 1 to 9 px of the player are below the first screen. |
| `tv-words-too-small` | TV windows 1080 px and taller | Left as it is on purpose; listed so the decision stays visible. |
| `script-off-player-cannot-be-started` | WebKit | Safari with script off draws no player buttons; the noscript link is the way out. |
| `safari-player-keys` | WebKit | Space and arrows on the player are Safari's own. |

`static/compat-allowlist.json` is the same idea for old-browser support.

## Add a device

One line in the table at the top of `playwright.config.js`:

```js
['iPhone 18',            'webkit',   'iPhone 18',     'touch'],          // a Playwright preset
['Galaxy Fold cover',    'chromium', [344, 882],      'touch', '', { deviceScaleFactor: 3, isMobile: true }],
```

Columns: a name; the engine the real device uses (`webkit` for anything from Apple, `chromium`
for Android, Samsung Internet and TVs, `firefox`); the name of a Playwright device preset
(`node -e "console.log(Object.keys(require('@playwright/test').devices).join('\n'))"`) or
`[width, height]` in CSS px; `touch`, `mouse` or `dpad` (a TV
remote); optionally `'deep'` or `'tv'` for the extra specs; optionally more Playwright options
(a user agent, a pixel ratio). Then run it: `npx playwright test --project="iPhone 18"`.

## Live mode: a real link, read-only

```sh
LIVE_WATCH_URL='https://.../w/<token>' npx playwright test
```

Runs **only** `specs/live.spec.js`, on every project: the real page opens with no error and
fits, and the real video plays through the real bucket. It never presses Send, never opens
"This is not my home", and the only request it makes that is not a GET is the page's own
"played" ping, so the office will see that video as watched. Use a test video: the link is
that customer's key and ends up in reports and pictures. In CI: run the workflow by hand and
fill in `live_url`.

## Real Safari (macOS)

```sh
sudo safaridriver --enable       # once per Mac
node safari/real-safari.js       # or: SAFARI_TARGETS=desktop node safari/real-safari.js
```

Drives Safari itself through Apple's `safaridriver`: Safari on the Mac, then Mobile Safari in
the iOS Simulator on a small iPhone, a current iPhone, a Pro Max and an iPad, picked at run
time from `xcrun simctl list devices available`. For each: loads a watch page, starts the
video, checks time moves, takes a picture (`safari-shots/`), and reports what Safari does once
the bucket link has expired. With `LIVE_WATCH_URL` set it also plays the real video. It prints
every step.

The run of 2026-11-14 (workflow run 43541391450): Safari 27.2 on macOS, and Mobile Safari
on iOS 26.2 on an iPhone SE (3rd generation), an iPhone 17, an iPhone 17 Pro Max and an iPad
(A16): all five PASS. In each the video played, the server was told "played" once, and a page
whose bucket link had expired played to the end with no note. In the same run WebKit on macOS
passed all 596 checks on the iPhone, iPad and desktop Safari rows.

Each target ends as one of three, and they are not the same thing:

* **PASS**: the video played in that Safari and time was measured moving.
* **FAIL**: it was driven and something was wrong.
* **SEEN, NOT MEASURED**: `safaridriver` would not open a session on that simulator (in the
  first CI run, the iPad), so the page was opened there with `xcrun simctl openurl` and
  photographed with `xcrun simctl io screenshot`. The server's own record says that Safari
  fetched the page and the start of the video; nobody pressed play. It is never called a pass.
  The run stays green with a warning; `SAFARI_STRICT=1` makes it fail.

Getting a session is the fragile part. The script starts one `safaridriver` for the whole run,
waits for the simulator to finish booting, opens Safari in it once and waits until the server
has seen it fetch a page, tries first the form of request that worked for the last device of
that kind, and, if every form fails, shuts the simulator down, boots it again and tries once
more before falling back.

## `serve.py`

```sh
python tests/browser/serve.py --port 19300 [--link-seconds N] [--bucket https|same-origin] [--rate-limit off|real] [--host 0.0.0.0 --base-url http://192.168.1.20:19300]
```

The real server plus a stand-in bucket on another origin (https, throwaway certificate,
byte ranges, links that really expire, and the refusal a real bucket gives: a 403 with an XML
body). `--bucket same-origin` serves the video from the web server itself, for anything that
cannot accept the certificate: real Safari, or your own phone on the same Wi-Fi (use
`--host 0.0.0.0 --base-url http://<this machine's address>:19300`, then open the printed
`watch` link on the phone). A public https `--base-url` (a tunnel, to put a real TV or a
phone off the Wi-Fi in front of it) makes the server take itself for a real deployment, so
it starts only with `VIDEO_ALLOW_THROWAWAY_STORAGE=1` set.

The control API, all under `/__test/`:

| Call | What it does |
| --- | --- |
| `GET info` | The URLs. |
| `POST video?file=...&throttle=...&first_byte_ms=...&link_seconds=...&address=...&pulled=1&refusal=visible` | Films a fresh video. Files: `sample`, `moov-at-end`, `stretched` (the size of a real visit video), `stretched-moov-at-end`, `cut-short` (breaks off part-way). |
| `GET state?token=...` | What the server and the bucket recorded for a link. |
| `POST expire?token=...` | Every bucket link handed out so far for it is dead. |
| `POST pull?token=...` | The office takes the video down, now. |
| `POST stall?token=...&on=1` | The bucket stops sending in mid-answer (a dropped signal); `on=0` lets it flow. |
| `GET email?token=...` | The HTML email that was "sent". |

The rate limiter is off for the test browsers (forty of them on 127.0.0.1 are not one
customer) and REAL for any client that says, in `X-Forwarded-For`, that it comes from
198.18.0.0/15: a test that wants to hit the limit picks an address of its own there. The
file's own header documents the rest.

Tests share one server and never share a video: each test films its own, so they run in
parallel.

## What this cannot prove

* Real Tizen, webOS and Fire TV browsers, and iOS 12 to 14: no test machine can run them. The
  TV projects are current Chromium with a TV's window, user agent and a remote's keys;
  `old-engines.spec.js` and the static checks cover what those engines lack.
* Mail apps' built-in browsers (Gmail, Outlook, Yahoo): on iOS they are WebKit and on Android
  Chromium, so the engine is covered; the app around it is not.
* A real AirPlay or Chromecast session. `tv.spec.js` checks what they need from the page.
* A real phone in a hand. `serve.py --host 0.0.0.0 --bucket same-origin` puts the page on one.
