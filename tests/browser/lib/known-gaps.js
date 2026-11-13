// KNOWN GAPS: things the customer pages get wrong TODAY, found by this suite.
//
// Each one is a product bug waiting for a fix in src/zest_video (templates, watch.js or
// http_web.py). It is listed here so the suite stays green while it waits, and so that:
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
// depends on the device and not on the page alone (which font it draws the stars with): the
// test passes either way and the run prints what this machine showed.

const everywhere = () => true;

module.exports = [
  {
    id: 'stars-wider-than-the-screen',
    // The star is not in the Rubik font file, so each device draws it with a font of its own
    // and its width differs: 56 px on the machine this was written on, about 45 px in others.
    // Five of them, with padding and gaps, need between about 270 and 326 px.
    where: ({ width }) => (width < 250 ? true : (width < 326 ? 'varies' : false)),
    what: 'The five stars do not shrink. They are drawn with whatever symbol font the device has (the star is not in the '
      + 'Rubik file), 45 to 56 px wide each, so the row needs 270 to 326 px. On a 320 px phone (iPhone SE 1st gen, iPhone '
      + '5s) with a wide star the 1-star and 5-star hang off the sides and the page scrolls sideways; zoomed in (Safari '
      + '"Page Zoom" 150% and up on any iPhone, or a desktop at 400%) the outer stars are off screen on every device, '
      + 'and at 200% zoom the 1-star cannot be reached at all. '
      + 'Fix: .stars label{font-size:min(54px,14vw);padding:2px 1px} and no gap under 360 px; better, draw the star as an inline SVG.',
  },
  {
    id: 'not-my-home-is-41px-tall',
    where: everywhere,
    what: '"This is not my home" (the <summary>) is 41 px tall; a finger needs 44. '
      + 'Fix: details summary{padding:10px 0;min-height:44px}.',
  },
  {
    id: 'date-is-15px',
    where: everywhere,
    what: 'The date under the heading (header.top small) is 15 px; nothing should be under 16. Fix: font-size:16px.',
  },
  {
    id: 'last-updated-is-15px',
    where: everywhere,
    what: '"Last updated November 2026." on the privacy and terms pages is 15 px (inline style in info.html). Fix: 16px.',
  },
  {
    id: 'player-below-first-screen',
    // header (86 px) + a player 74% of the window tall only fits when the window is 331 px or taller
    where: ({ height }) => height < 331,
    what: 'On a phone on its side with a window under 331 px tall (iPhone SE: 320) the bottom of the player, '
      + 'where the play button is, is below the first screen. Fix: max-height:calc(100vh - 100px) in landscape.',
  },
  {
    id: 'long-address-pushes-player-down',
    // a long address wraps the heading to two lines (94 px above the player instead of 86).
    // Every project in that range of heights is an iPhone on its side, so WebKit: measured only
    // with Chromium laying out the same sizes (ALL_ROWS_ON=chromium), where two of them miss by 2 to 5 px.
    where: ({ engine, height }) => (height >= 331 && height < 359 ? (engine === 'chromium' ? true : 'unverified') : false),
    what: 'With a long address the heading wraps to two lines and pushes the player down: on a phone on its side '
      + 'with a window under 359 px tall the play button ends up below the first screen.',
  },
  {
    id: 'star-arrows-are-backwards',
    // every engine moves a radio group in document order; only Chromium has been run
    where: ({ engine }) => (engine === 'chromium' ? true : 'unverified'),
    what: 'With a keyboard on the stars, the RIGHT arrow moves one star to the LEFT (3 stars becomes 2) and the left '
      + 'arrow moves right. The radio buttons are in the page as 5,4,3,2,1 and drawn reversed (flex-direction:row-reverse), '
      + 'and arrows follow page order. Fix: put them in the page as 1..5 and colour with :has(), or swap the arrows in script.',
  },
  {
    id: 'tv-remote-ok-does-not-play',
    where: ({ input }) => input === 'dpad',
    what: 'On a TV browser in focus mode (arrows move the focus, OK presses), the focus lands on the player but OK does not '
      + 'start it: Chromium ignores Enter and Space on a focused <video> when spatial navigation is on, and the arrows skip '
      + 'the small buttons inside the player. With the remote in pointer mode (the default on Samsung, LG and Fire TV) a click '
      + 'on the player works. Fix: a real, big "Play" button on the page that calls video.play().',
  },
  {
    id: 'tv-words-too-small',
    // 19 px text on a 1080-line (or taller) window; a TV browser that reports 960x540 doubles everything and is fine
    where: ({ height }) => height >= 1080,
    what: 'On a TV browser the page is a 528 px column in the middle of the screen with 19 px text (15 px for the date): '
      + 'about a third smaller than the 24 px a 1080-line TV needs at 3 metres. The video itself is fine. '
      + 'Fix: a @media (min-width:1500px) and (min-height:900px) block that scales the column and the type up.',
  },
  {
    id: 'tv-4k-player-is-small',
    // only where the browser reports a 3840 px wide window; most 4K sets report 1920x1080 at 2x
    where: ({ width }) => width >= 3000,
    what: 'In a window 3840 px wide the column stays 560 px, so the player is 43% of the screen height. '
      + 'Most 4K TVs report 1920x1080 and are not affected. Full screen fixes it for the viewer.',
  },
  {
    id: 'expired-link-stalls',
    // Chromium keeps the redirected bucket address and asks IT again, never /w/<token>/video,
    // when the redirect goes to ANOTHER origin, as it does in production. (When the bucket is
    // served from the page's own origin, --bucket same-origin, Chromium does re-ask the page's
    // address and the video plays: run and seen.) Firefox is believed to re-ask the page's own
    // address; Safari is not known.
    where: ({ engine, bucket }) => (engine === 'chromium' ? bucket === 'https' : 'unverified'),
    what: 'Open the page, leave it past the life of the bucket link (15 minutes in production), press play: Chrome, Edge, '
      + 'Samsung Internet and Android mail apps play only what was buffered when the page opened (a few seconds on a desktop, '
      + 'under a second on a phone), then spin for ever. The player asks the expired bucket address again and gets 403; it never '
      + 'goes back to /w/<token>/video for a fresh one. Fix: sign the link for a day (VIDEO_WATCH_URL_SECONDS), or in watch.js '
      + 'reload the source on "stalled"/"waiting" and seek back to where it was.',
  },
  {
    id: 'expired-link-seek-after-pause',
    // Not a gap in Chromium on a fast connection: by the time of the pause it holds the whole
    // file, so nothing more is asked of the bucket. Firefox and Safari have not been run.
    where: ({ engine }) => (engine === 'chromium' ? false : 'unverified'),
    what: 'Play, pause, come back after the bucket link has expired and skip ahead: if the browser needs more of the file '
      + 'it asks the expired address and the seek never finishes. Same cause and fix as expired-link-stalls.',
  },
  {
    id: 'stall-shows-no-message',
    where: ({ engine }) => (engine === 'chromium' ? true : 'unverified'), // the test skips itself when nothing was refused
    what: 'When the bucket refuses the player (expired link), no "error" event fires, so the page\'s "The video did not load" '
      + 'note never shows: the customer sees a spinner and nothing else. Fix: also show the note on "stalled" or after N seconds of "waiting".',
  },
  {
    id: 'script-off-player-cannot-be-started',
    // Chromium draws its player without script and works (run). Safari builds its player
    // controls with script of its own and may show none; Firefox is believed to be fine.
    where: ({ engine }) => (engine === 'chromium' ? false : 'unverified'),
    what: 'With JavaScript switched off in the browser, the player may have no working controls, so the video cannot be started.',
  },
  {
    id: 'back-then-send-replaces-the-score',
    // Chromium asks the server again after Back (no-store) and shows the saved score (run).
    // Safari keeps no-store pages in its back/forward cache when they came over plain http,
    // which is how the local test server serves them; production is https.
    where: ({ engine }) => (engine === 'chromium' ? false : 'unverified'),
    what: 'After sending a score, pressing Back may show the old form again from the browser\'s own cache; sending again '
      + 'replaces the first score on our side (FieldRoutes keeps the first). The server allows that on purpose '
      + '("changing their mind"); the page is what stops it, by not offering the form twice.',
  },
  {
    id: 'play-before-script-is-not-counted',
    where: ({ engine }) => (engine === 'chromium' ? true : 'unverified'),
    what: 'On a slow connection the player is usable before watch.js arrives. A customer who presses play in that gap is '
      + 'watching, but the script only listens for LATER "play" events, so the server is never told and the office sees '
      + '"not watched". Fix: in watch.js, after adding the listener, also report at once if the video is already playing (!video.paused).',
  },
  {
    id: 'forced-colours-stars-look-the-same',
    // Chromium and Firefox have forced colours; Safari has none (the test skips itself there)
    where: ({ engine }) => (engine === 'chromium' ? true : 'unverified'),
    what: 'In a Windows contrast theme (forced colours) every star is drawn in the same text colour, so a customer cannot '
      + 'see which stars are picked: the gold/grey difference is the only signal and forced colours remove it. '
      + 'Fix: @media (forced-colors: active){.stars input:checked~label{color:Highlight}} or show ☆ and ★.',
  },
  {
    id: 'stars-are-faint-and-differ-by-hue-only',
    where: everywhere,
    what: 'An empty star (#cdbfe0) is 1.6:1 against the card, where 3:1 is the minimum for a control, and a picked star (gold '
      + '#F5B800) has the same brightness as an unpicked one (1.03:1): they differ by hue alone. In greyscale, to a colour-blind '
      + 'customer, or on a phone in sunlight the picked stars cannot be told from the rest. '
      + 'Fix: darker empty stars (#8a78a8 or an outline star ☆) and a darker gold, or fill versus outline.',
  },
  {
    id: 'player-shrinks-to-nothing-when-zoomed',
    // checked only in the 320x180 window (a desktop at 400%)
    where: everywhere,
    what: 'The player is capped at 74% of the window HEIGHT (max-height:74vh). On a desktop zoomed to 400% the window is '
      + '180 CSS px tall, so the player is 75x133 px: too small to watch, its buttons too small to press. '
      + 'Fix: max-height:max(74vh, 320px), so it scrolls instead of shrinking.',
  },
  {
    id: 'old-engines-logo-touches-heading',
    // measured with the newer CSS taken away from a modern engine (old-engines.spec.js)
    where: everywhere,
    what: 'Where flex "gap" is not understood (iOS before 14.5: every iPhone 5s, 6, 7 and 8 that was not updated; '
      + 'Chromium before 84: 2017-2020 TVs) the logo sits right against the heading text. Fix: header.top img{margin-right:12px}.',
  },
  {
    id: 'old-engines-star-focus-is-invisible',
    // With a TV remote, Chromium's focus mode puts the focus on the star's label and draws its
    // own ring there, so the remote is fine; it is the keyboard (focus on the hidden radio
    // button) that is left with nothing.
    where: ({ input }) => input === 'mouse',
    what: 'Where :focus-visible is not understood (Safari before 15.4, Chromium before 86, Samsung Internet before 14) '
      + 'the star that has the keyboard focus shows no outline at all: the radio button is invisible on purpose and the '
      + 'rule that outlines its label is dropped. '
      + 'Fix: add .stars input:focus+label{outline:3px solid #932FFB;border-radius:8px} before the :focus-visible rule.',
  },
  {
    id: 'old-engines-page-jumps-when-video-loads',
    where: everywhere,
    what: 'Where aspect-ratio is not understood (iOS before 15, Chromium before 88) the player is a 300x150 box until the '
      + "video's details arrive, then grows to full height and pushes the score form down under the customer's finger. "
      + 'Fix: <video width="720" height="1280">.',
  },
  {
    id: 'safari-player-keys',
    // Safari's player decides its own keys; never run here. Chrome, Edge and Firefox must pass.
    where: ({ engine }) => (engine === 'webkit' ? 'unverified' : false),
    what: 'In Safari the player may not be reachable with Tab, or the space bar and arrows may not work it.',
  },
  {
    id: 'page-jumps-when-video-loads',
    // Chromium gives the player its final size before the video's details arrive (run, on a
    // phone, a tablet and a desktop window). Whether Firefox and Safari size an empty <video>
    // with width:auto and aspect-ratio the same way has not been run.
    where: ({ engine }) => (engine === 'chromium' ? false : 'unverified'),
    what: 'While the video is still on its way the player may be a different size than once it has loaded, so the score '
      + 'form moves under the customer\'s finger. Fix: <video width="720" height="1280">.',
  },
];
