// Real browsers against the customer side of Zest Visit Video. See README.md in this folder.
//
// THE TABLE. One row is one "project": a browser engine at a screen size, with the way the
// customer drives it. To add the owner's next phone, add ONE line here.
//
//   name                          engine      size: a Playwright preset name, or [width, height]   how it is driven   extras
//
// engine   chromium = Chrome, Edge, Samsung Internet, Android WebView, most smart TVs
//          webkit   = Safari on iPhone, iPad and Mac, and EVERY browser and mail app on iOS
//          firefox  = Firefox
// input    touch = taps, mouse = clicks, dpad = a TV remote: arrow keys and OK only
// extras   'deep' = also runs the slower specs (script off, expiring links, slow link, a file
//          that is not faststart, colour schemes, zoom, the email); 'tv' = also runs tv.spec.js
const TABLE = [
  // Desktop: the three engines.
  ['Desktop Chrome',               'chromium', 'Desktop Chrome',               'mouse', 'deep'],
  ['Desktop Firefox',              'firefox',  'Desktop Firefox',              'mouse', 'deep'],
  ['Desktop Safari',               'webkit',   'Desktop Safari',               'mouse', 'deep'],
  ['Desktop Edge',                 'chromium', 'Desktop Edge',                 'mouse'],

  // iPhones, oldest to newest, upright and on their side. Always WebKit, as on the real phone.
  ['iPhone SE',                    'webkit',   'iPhone SE',                    'touch'],
  ['iPhone SE landscape',          'webkit',   'iPhone SE landscape',          'touch'],
  ['iPhone 8',                     'webkit',   'iPhone 8',                     'touch'],
  ['iPhone 8 landscape',           'webkit',   'iPhone 8 landscape',           'touch'],
  ['iPhone 11',                    'webkit',   'iPhone 11',                    'touch'],
  ['iPhone 11 landscape',          'webkit',   'iPhone 11 landscape',          'touch'],
  ['iPhone 13',                    'webkit',   'iPhone 13',                    'touch', 'deep'],
  ['iPhone 13 landscape',          'webkit',   'iPhone 13 landscape',          'touch'],
  ['iPhone 15 Pro Max',            'webkit',   'iPhone 15 Pro Max',            'touch'],
  ['iPhone 15 Pro Max landscape',  'webkit',   'iPhone 15 Pro Max landscape',  'touch'],
  ['iPhone 17',                    'webkit',   'iPhone 17',                    'touch'],
  ['iPhone 17 landscape',          'webkit',   'iPhone 17 landscape',          'touch'],
  ['iPhone 17 Pro Max',            'webkit',   'iPhone 17 Pro Max',            'touch'],
  ['iPhone 17 Pro Max landscape',  'webkit',   'iPhone 17 Pro Max landscape',  'touch'],

  // iPads. The last row is an iPad as iPadOS 13 and later present it: a desktop-class user agent.
  ['iPad Mini',                    'webkit',   'iPad Mini',                    'touch'],
  ['iPad Mini landscape',          'webkit',   'iPad Mini landscape',          'touch'],
  ['iPad (gen 7)',                 'webkit',   'iPad (gen 7)',                 'touch', 'deep'],
  ['iPad (gen 7) landscape',       'webkit',   'iPad (gen 7) landscape',       'touch'],
  ['iPad Pro 11',                  'webkit',   'iPad Pro 11',                  'touch'],
  ['iPad Pro 11 landscape',        'webkit',   'iPad Pro 11 landscape',        'touch'],
  ['iPad desktop-class Safari',    'webkit',   [820, 1180],                    'touch', '', { deviceScaleFactor: 2, userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15' }],

  // Android phones: Chrome, Samsung Internet and the WebView inside mail apps are all Chromium.
  ['Galaxy S8',                    'chromium', 'Galaxy S8',                    'touch'],
  ['Galaxy S24',                   'chromium', 'Galaxy S24',                   'touch'],
  ['Galaxy S24 landscape',         'chromium', 'Galaxy S24 landscape',         'touch'],
  ['Pixel 7',                      'chromium', 'Pixel 7',                      'touch', 'deep'],

  // Hand-made sizes: the smallest phones still in use, and desktop monitors.
  ['320x568 small phone',          'chromium', [320, 568],                     'touch', '', { deviceScaleFactor: 2, isMobile: true }],
  ['360x640 small Android',        'chromium', [360, 640],                     'touch', '', { deviceScaleFactor: 3, isMobile: true }],
  ['1280x720 laptop',              'chromium', [1280, 720],                    'mouse'],
  ['1920x1080 monitor',            'chromium', [1920, 1080],                   'mouse'],
  ['2560x1440 monitor',            'chromium', [2560, 1440],                   'mouse'],

  // Televisions: no touch, no mouse. The remote's arrows move the focus and OK presses it.
  ['TV Samsung Tizen 1080p',       'chromium', [1920, 1080],                   'dpad',  'tv', { userAgent: 'Mozilla/5.0 (SMART-TV; LINUX; Tizen 6.5) AppleWebKit/537.36 (KHTML, like Gecko) 85.0.4183.93/6.5 TV Safari/537.36' }],
  ['TV LG webOS 4K',               'chromium', [3840, 2160],                   'dpad',  'tv', { userAgent: 'Mozilla/5.0 (Web0S; Linux/SmartTV) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/79.0.3945.79 Safari/537.36 WebAppManager' }],
  ['TV Fire TV Silk 960x540',      'chromium', [960, 540],                     'dpad',  'tv', { deviceScaleFactor: 2, userAgent: 'Mozilla/5.0 (Linux; Android 11; AFTKA) AppleWebKit/537.36 (KHTML, like Gecko) Silk/138.4.1 like Chrome/138.0.7204.179 Safari/537.36' }],
];

// ---------------------------------------------------------------------------------------------

const fs = require('fs');
const path = require('path');
const { defineConfig, devices } = require('@playwright/test');
const playwright = require('@playwright/test');

const CI = !!process.env.CI;
const LIVE = process.env.LIVE_WATCH_URL || '';
const PORT = Number(process.env.BROWSER_TEST_PORT || 19300);
const BUCKET = process.env.BROWSER_TEST_BUCKET || 'https'; // or same-origin
const isMainProcess = process.env.TEST_WORKER_INDEX === undefined;
// ALL_ROWS_ON=chromium runs every row on one engine. On a machine without WebKit that previews
// the LAYOUT of the iPhone and iPad rows (sizes, wrapping, what is below the fold). It says
// nothing about Safari's own player; only the real engine does.
const FORCE = process.env.ALL_ROWS_ON || '';

// Which engines can run on this machine? CI installs all three. A laptop or a sandbox may
// only have Chromium: there the Firefox and WebKit rows are left out, loudly, not failed.
function installed(engine) {
  try {
    return fs.existsSync(playwright[engine].executablePath());
  } catch (e) {
    return false;
  }
}
const have = { chromium: installed('chromium'), firefox: installed('firefox'), webkit: installed('webkit') };
const missing = Object.keys(have).filter((engine) => !have[engine]);

// Everything every row runs, and what the 'deep' and 'tv' rows add.
const CORE = ['watch.spec.js', 'other-pages.spec.js'];
const DEEP = ['noscript.spec.js', 'expiring-links.spec.js', 'slow-link.spec.js',
  'non-faststart.spec.js', 'page-weight.spec.js', 'colour-schemes.spec.js', 'email.spec.js', 'old-engines.spec.js'];
const DESK = ['keyboard.spec.js', 'zoom-reflow.spec.js'];
const TV = ['keyboard.spec.js', 'tv.spec.js', 'old-engines.spec.js'];

function project([name, rowEngine, size, input, extras = '', more = {}]) {
  const engine = FORCE || rowEngine;
  const preset = typeof size === 'string' ? devices[size] : { viewport: { width: size[0], height: size[1] } };
  if (!preset) throw new Error(`playwright.config.js: no device preset called "${size}"`);
  const specs = LIVE ? ['live.spec.js'] : [
    ...CORE,
    ...(extras === 'deep' ? DEEP : []),
    ...(extras === 'deep' && input === 'mouse' ? DESK : []), // a keyboard, and a window that can be zoomed
    ...(extras === 'tv' ? TV : []),
  ];
  const use = {
    ...preset,
    ...more,
    browserName: engine,
    hasTouch: input === 'touch',
    input,
  };
  if (engine === 'firefox') delete use.isMobile; // Firefox has no mobile emulation
  if (input === 'dpad') {
    // What a TV browser does with the remote's arrows: move the focus to the nearest control.
    use.launchOptions = { args: ['--enable-spatial-navigation'] };
  }
  return { name, testDir: './specs', testMatch: specs, use, metadata: { engine, input, extras } };
}

let rows = TABLE;
if (!CI && missing.length) {
  rows = TABLE.filter((row) => have[FORCE || row[1]]);
  if (isMainProcess && rows.length < TABLE.length) {
    const skipped = TABLE.filter((row) => !have[FORCE || row[1]]).map((row) => row[0]);
    console.warn(
      `\n  NOT RUN HERE: ${skipped.length} of ${TABLE.length} projects need ${missing.join(' and ')}, which ` +
      `${missing.length > 1 ? 'are' : 'is'} not installed on this machine:\n    ${skipped.join(', ')}\n` +
      '  They run in CI (.github/workflows/web.yml). To install them here: npx playwright install firefox webkit\n',
    );
  }
}
if (process.env.ONLY_ENGINE) rows = rows.filter((row) => row[1] === process.env.ONLY_ENGINE); // the macOS job: webkit

const python = process.env.PYTHON
  || (fs.existsSync(path.join(__dirname, '..', '..', '.venv', 'bin', 'python')) ? path.join(__dirname, '..', '..', '.venv', 'bin', 'python') : 'python3');

// A live run keeps its own folders, so it never overwrites the local run's report.
const KIND = LIVE ? 'live-' : '';

module.exports = defineConfig({
  outputDir: `./${KIND}test-results`,
  fullyParallel: true, // every test films its own fresh video, so nothing is shared
  forbidOnly: CI,
  retries: CI ? 1 : 0,
  workers: process.env.BROWSER_TEST_WORKERS || (CI ? '100%' : undefined),
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reportSlowTests: null,
  reporter: [
    [CI ? 'github' : 'list'],
    ...(CI ? [['list']] : []),
    ['html', { outputFolder: `./report/${KIND}html`, open: 'never' }],
    ['json', { outputFile: `./report/${KIND}results.json` }],
    ['./lib/gaps-reporter.js'],
  ],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    ignoreHTTPSErrors: !LIVE, // the stand-in bucket's throwaway certificate; a live run checks real certificates
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
    locale: 'en-US',
    timezoneId: 'America/Los_Angeles',
  },
  projects: [
    // No browser: is watch.js old-engine JavaScript, and which CSS do old engines not know?
    ...(LIVE ? [] : [{ name: 'static', testDir: './static', testMatch: '*.spec.js' }]),
    ...rows.map(project),
  ],
  // One server for the whole run (see serve.py). In live mode nothing local is started.
  webServer: LIVE ? undefined : {
    command: `"${python}" serve.py --port ${PORT} --bucket ${BUCKET}`,
    cwd: __dirname,
    url: `http://127.0.0.1:${PORT}/__test/health`,
    reuseExistingServer: !CI,
    timeout: 90_000,
    stdout: 'ignore',
    stderr: 'pipe',
    gracefulShutdown: { signal: 'SIGTERM', timeout: 3000 },
  },
});

module.exports.TABLE = TABLE;
