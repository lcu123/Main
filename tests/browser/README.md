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
| `specs/watch.spec.js` | The watch page opens with no error, fits the screen with the player in the first screen, the video loads through the redirect to the bucket and plays, pauses, seeks and replays, the server hears "played" exactly once, a score is saved once and stays, "This is not my home" takes the video down, every control is 44x44 px and no text is under 16 px. |
| `specs/other-pages.spec.js` | A dead link and an unknown link show the kind message and the office number; unsubscribe, privacy, terms and support open, fit and read. |

Only the rows marked `deep` (the three desktop engines, iPhone 13, iPad (gen 7), Pixel 7):

| Spec | In one line |
| --- | --- |
| `specs/noscript.spec.js` | With script switched off the video, the score and "not my home" still work. |
| `specs/expiring-links.spec.js` | What happens when the 15-minute bucket link has run out before play, after a pause, on a seek. |
| `specs/slow-link.spec.js` | On a slow bucket, slow page files and (Chromium) a throttled connection, the page is usable at once and the video starts when it can. |
| `specs/non-faststart.spec.js` | A video whose index is at the END of the file still plays, and how the browser finds the index. |
| `specs/page-weight.spec.js` | The page without the video stays inside its byte budget, and each file is fetched once. |
| `specs/colour-schemes.spec.js` | Dark mode, forced colours and browser "darken websites": text stays readable and a picked star still looks picked (measured in pixels). |
| `specs/email.spec.js` | The email's one button is big, fits the screen and opens the video. |
| `specs/old-engines.spec.js` | The page with the CSS that iOS 12-14 and 2017-2021 TV browsers do not understand taken away: what is left still fits and works. |

Only where there is a keyboard or a remote (desktop engines and TV rows):

| Spec | In one line |
| --- | --- |
| `specs/keyboard.spec.js` | Tab, Shift+Tab, Space, Enter and arrows (or a TV remote's arrows and OK) reach and work the player, the stars and Send, and the focused control is visibly outlined. |
| `specs/zoom-reflow.spec.js` | At 200% and 400% desktop zoom and iPhone "Page Zoom" 150% and 200%: no sideways scroll, nothing cut off, the form still works. (Desktop engines.) |
| `specs/tv.spec.js` | On a TV: pointer-mode remote plays and scores, full screen works, nothing blocks AirPlay or Chromecast, text and player size from 3 metres. (TV rows.) |

No browser (`--project=static`):

| Spec | In one line |
| --- | --- |
| `static/es5.spec.js` | `watch.js` parses as ES5 (old iOS and TV engines run none of a file with newer syntax) and uses only listed browser features; no inline script. |
| `static/compat.spec.js` | Every CSS and browser feature the pages use that some browser on the support list lacks is printed with the versions, and must be in `static/compat-allowlist.json` with a reason. |
| `static/gaps.spec.js` | `lib/known-gaps.js` has no dead or duplicate entries. |

Live mode only: `specs/live.spec.js` (below).

## Known gaps: a green run does not mean nothing is wrong

`lib/known-gaps.js` is the one list of things the pages get wrong **today**. Each entry is a
product bug waiting for a fix in `src/zest_video`. While it waits the suite stays green, and:

* anything **new** that goes wrong still fails the build;
* the day a listed bug is **fixed**, its test fails with *"listed in known-gaps.js but the page
  now gets it right"* (or *"expected to fail, but passed"*): delete the entry.

Every run ends by printing the gaps it touched and on which projects (also in
`report/known-gaps.txt`, and in the CI run's summary). Two more lists may follow:

* **TO CONFIRM**: an entry marked `'unverified'` for an engine was written without that engine
  at hand. The run says whether it happens there; put `true` or `false` in the entry.
* **DEPENDS ON THE DEVICE**: an entry marked `'varies'` (today: the star row, drawn with
  whatever symbol font the device has) is reported, never failed.

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

## `serve.py`

```sh
python tests/browser/serve.py --port 19300 [--link-seconds N] [--bucket https|same-origin] [--host 0.0.0.0 --base-url http://192.168.1.20:19300]
```

The real server plus a stand-in bucket on another origin (https, throwaway certificate,
byte ranges, links that really expire). `--bucket same-origin` serves the video from the web
server itself, for anything that cannot accept the certificate: real Safari, or your own phone
on the same Wi-Fi (use `--host 0.0.0.0 --base-url http://<this machine's address>:19300`, then
open the printed `watch` link on the phone). `GET /__test/info` lists the URLs;
`POST /__test/video` films a fresh video; `GET /__test/state?token=...` says what the server
and the bucket recorded for a link. The file's own header documents the rest.

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
