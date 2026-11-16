// KNOWN GAPS: things the customer pages get wrong TODAY, found by this suite.
//
// Each one is a product bug waiting for a fix in src/zest_video (templates, watch.js or
// http_web.py), or a limit of one browser that the page cannot remove. It is listed here so
// the suite stays green while it waits, and so that:
//   * a NEW problem still fails the build (only what is listed here is excused), and
//   * the day one of these is fixed, the test that covers it FAILS with
//     "listed in known-gaps.js but the page now gets it right": then delete the entry.
//
// `where` says on which projects it happens: it gets { project, engine, input, width, height,
// bucket } (the project's name, 'chromium' | 'webkit' | 'firefox', 'mouse' | 'touch' | 'dpad',
// the window size in CSS px, and 'https' | 'same-origin' for where the stand-in bucket is).
// It answers true (happens here), false (does not), or 'unverified': written from how that
// engine is believed to behave, without having been run on it. Where it is 'unverified' the
// test passes either way and the run prints "TO CONFIRM: ... it HAPPENS / does NOT happen":
// then replace 'unverified' for that engine with true or false. 'varies' is for a gap that
// depends on the device and not on the page alone: the test passes either way and the run
// prints what this machine showed.
//
// History. The first list (2026-11-13, morning) had 24 entries. The page was fixed the same
// day and 18 of them are gone: the stars (now drawn: they shrink, and are hollow or filled),
// the 15 px date and "last updated", the 41 px "not my home", the arrows on the stars, OK on a
// TV remote, the 4K player, the zoomed player, the three old-engine layout faults, the page
// jumping as the video loads, a play pressed before the script arrived, the player that spun
// for ever on an expired link with no message, and two that the first CI run showed happen
// in no engine (Back after Send, a skip after a pause). Six remained, and eight were new: what
// the fixed page showed once its new parts had tests of their own. A second round the same
// evening fixed six of those fourteen (the half-minute wait on an expired link in Chrome,
// "try again" counted as watched, the focus lost when the note is pressed, the stars on a
// page the browser has darkened, the office number sticking out of its button at 200% zoom,
// the small link shown when script is off). The first run of that page in Firefox and Safari's
// engine then settled two entries that had been written without those engines at hand: Enter
// on the player works in both (entry deleted), and in a Windows contrast theme Firefox showed
// a picked star too close to an unpicked one (fixed in the page: a picked star is filled with
// the theme's text colour; entry deleted). The first full run on a Mac settled a third: Safari
// does not wait for the whole file before it shows a faststart video (entry deleted). Five
// are left.

const everywhere = () => true;

module.exports = [
  {
    id: 'player-below-first-screen',
    // header (86 px) + a player that is 74% of the window tall, but never under 240 px, only
    // fits when the window is 331 px or taller. The one row that small is the iPhone SE on its side.
    where: ({ height }) => height < 331,
    what: 'On a phone on its side with a window under 331 px tall (iPhone SE: 320) the bottom 6 px of the player, where its '
      + 'buttons are, is below the first screen (15 px with a long address). Most of each button is still on screen. '
      + 'Fix, if wanted: max-height:calc(100vh - 100px) for short landscape windows.',
  },
  {
    id: 'long-address-pushes-player-down',
    // A long address wraps the heading to two lines: 95 px above the player instead of 86 (it
    // was 94 before the date grew from 15 to 16 px). 95 + 74% of the window fits from 364 px up.
    // Run: Chromium at 780x360 misses by 1 px; WebKit at 750x342 and 756x352 (first CI run).
    // The iPhone 11 on its side is 364 px: it fits, with 0.14 px to spare (run on WebKit).
    where: ({ height }) => height >= 331 && height < 364,
    what: 'With a long address the heading wraps to two lines and pushes the player down: on a phone on its side with a window '
      + 'under 364 px tall the last 1 to 9 px of the player are below the first screen. Its buttons can still be pressed.',
  },
  {
    id: 'tv-words-too-small',
    // 19 px text on a 1080-line (or taller) window; a TV browser that reports 960x540 doubles everything and is fine
    where: ({ height }) => height >= 1080,
    what: 'On a TV browser the words are 16 to 22 px in the middle of the screen: about a third smaller than the 24 px a '
      + '1080-line TV needs at 3 metres (half that on a browser that reports 3840x2160). LEFT AS IT IS ON PURPOSE in the fix of '
      + '2026-11-13: the review judged that the video is what a TV is for, and the player fills 74% of the screen height on '
      + 'every TV size now. Listed so that the decision stays visible; '
      + 'if it is ever revisited: a @media (min-width:1500px) and (min-height:900px) block that scales the type up.',
  },
  {
    id: 'script-off-player-cannot-be-started',
    // Chromium and Firefox draw their player without script and it works (run). WebKit builds
    // its player controls with script of its own: with script off in the browser there are
    // none (first CI run: Desktop Safari, iPhone 13 and iPad rows, on Linux and on macOS).
    where: ({ engine }) => engine === 'webkit',
    what: 'With JavaScript switched off in Safari the player inside the page shows no buttons, so the video cannot be started '
      + 'there. The page now gives such a customer a way out: the link "Open the video by itself" under the player (it shows only '
      + 'when script is off) opens the video in Safari\'s own full-window player. Nothing more can be done from the page.',
  },
  {
    id: 'safari-player-keys',
    // Safari's player decides its own keys (first CI run: it happens on Desktop Safari, on Linux
    // and macOS). Chrome, Edge and Firefox must pass.
    where: ({ engine }) => engine === 'webkit',
    what: 'In Safari on a Mac the space bar and the arrow keys do not work the player the way they do in Chrome, Edge and '
      + 'Firefox (Safari\'s own player has its own keys, and on a Mac a plain Tab does not stop on every control). A click works.',
  },
];
