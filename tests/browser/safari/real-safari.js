#!/usr/bin/env node
// REAL Safari, driven through Apple's own safaridriver: Safari on the Mac, and Mobile Safari
// in the iOS Simulator on a small iPhone, a current iPhone, a Pro Max and an iPad.
// Playwright's "webkit" is the same engine built by Playwright; this is the browser itself,
// with Apple's video stack. macOS only. CI: the "safari-mac" job in .github/workflows/web.yml.
//
//     node safari/real-safari.js                 # all targets
//     SAFARI_TARGETS=desktop node safari/real-safari.js
//     LIVE_WATCH_URL=https://.../w/<token> node safari/real-safari.js    # also plays the real video
//
// For each target it: opens a fresh watch page from the local server (serve.py with
// --bucket same-origin: real Safari cannot be told to accept the stand-in bucket's throwaway
// certificate), checks the page fits, starts the video, checks time moves, takes a picture
// (safari-shots/<target>.png), and then probes what Safari does when the bucket link has
// expired (reported, never failed).
//
// Results are one of three, and they are not the same thing:
//   PASS                the video played in that Safari and time was measured moving.
//   FAIL                it was driven and something was wrong.
//   SEEN, NOT MEASURED  safaridriver would not open a session on that simulator, so Safari was
//                       opened on the page with "xcrun simctl openurl" and photographed with
//                       "xcrun simctl io screenshot". The server's own record says Safari
//                       fetched the page and the start of the video; nobody pressed play.
//                       That is a picture, not a measurement, and it is never called a pass.
//                       (SAFARI_STRICT=1 makes it fail the run.)
//
// Getting a session on a simulator is the fragile part (first CI run: two to four attempts
// per iPhone, about two minutes each, and none on the iPad). What this does about it:
//   * ONE safaridriver for the whole run, started here and asked for its /status before use
//     (selenium starting a new one per attempt was what "Timed out waiting for the WebDriver
//     server" meant: three of every four failures);
//   * the simulator is booted and waited for (simctl bootstatus -b), then Safari is opened in
//     it once with simctl openurl and the script waits until the SERVER has seen that Safari
//     fetch a page: only then is safaridriver asked;
//   * the form of the request that worked for the last iPhone (or iPad) is tried first;
//   * if every form fails, the simulator is shut down and booted again, safaridriver is
//     restarted, and everything is tried once more;
//   * and if that fails too: SEEN, NOT MEASURED, as above.
//
// Everything it does is printed, step by step, because the first runs will need reading.

const fs = require('fs');
const path = require('path');
const { spawn, execFileSync } = require('child_process');

const HERE = path.join(__dirname, '..');
const SHOTS = path.join(HERE, 'safari-shots');
const PORT = Number(process.env.BROWSER_TEST_PORT || 19300);
const LIVE = process.env.LIVE_WATCH_URL || '';
const ONLY = (process.env.SAFARI_TARGETS || '').split(',').map((s) => s.trim()).filter(Boolean);
const SESSION_TIMEOUT = Number(process.env.SAFARI_SESSION_TIMEOUT_MS || 90_000);
const STRICT = process.env.SAFARI_STRICT === '1';
// Only for checking this script's own plumbing on a machine without Safari (stand-ins for
// xcrun and safaridriver on the PATH). Never set in CI.
const SELFTEST = process.env.REAL_SAFARI_SELFTEST === '1';

const lines = [];
function say(text = '') {
  const stamp = new Date().toISOString().slice(11, 19);
  for (const line of String(text).split('\n')) {
    console.log(`[${stamp}] ${line}`);
    lines.push(line);
  }
}
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const within = (promise, ms, what) => Promise.race([
  promise,
  new Promise((_, reject) => setTimeout(() => reject(new Error(`timed out after ${Math.round(ms / 1000)} s: ${what}`)), ms)),
]);

// ---------------------------------------------------------------------------------------------
// The local server
// ---------------------------------------------------------------------------------------------

async function startServer() {
  const already = await fetch(`http://127.0.0.1:${PORT}/__test/info`).then((r) => r.json()).catch(() => null);
  if (already) {
    if (already.bucketMode !== 'same-origin') throw new Error(`a server is already on port ${PORT} but with --bucket ${already.bucketMode}; real Safari needs --bucket same-origin`);
    say(`using the server already running on port ${PORT} (bucket: same-origin)`);
    return { info: already, stop: () => {} };
  }
  const venv = path.join(HERE, '..', '..', '.venv', 'bin', 'python');
  const python = process.env.PYTHON || (fs.existsSync(venv) ? venv : 'python3');
  say(`starting: ${python} serve.py --port ${PORT} --bucket same-origin`);
  const child = spawn(python, ['serve.py', '--port', String(PORT), '--bucket', 'same-origin'], { cwd: HERE, stdio: ['ignore', 'pipe', 'pipe'] });
  let out = '';
  let err = '';
  child.stdout.on('data', (d) => { out += d; });
  child.stderr.on('data', (d) => { err += d; });
  const deadline = Date.now() + 90_000;
  while (!out.includes('\n')) {
    if (child.exitCode !== null) throw new Error(`serve.py stopped at once:\n${err}`);
    if (Date.now() > deadline) { child.kill(); throw new Error(`serve.py did not start in 90 s:\n${err}`); }
    await sleep(200);
  }
  const info = JSON.parse(out.split('\n')[0]);
  say(`server ready: ${info.base} (videos served from ${info.bucket})`);
  return { info, stop: () => child.kill('SIGTERM') };
}

async function control(pathAndQuery, options) {
  // A request that never reaches the server is tried again: after the two silent minutes a
  // simulator takes to boot, the first request sometimes went out on a connection the
  // server had long closed ("fetch failed"), and that one line failed a whole phone.
  let failure;
  for (let attempt = 1; attempt <= 4; attempt += 1) {
    let response;
    try {
      response = await fetch(`http://127.0.0.1:${PORT}/__test${pathAndQuery}`, { ...options, signal: AbortSignal.timeout(20_000) });
    } catch (error) {
      failure = error;
      await sleep(500 * attempt);
      continue;
    }
    if (!response.ok) throw new Error(`serve.py ${pathAndQuery}: ${response.status} ${await response.text()}`);
    return response.json();
  }
  throw new Error(`serve.py ${pathAndQuery} could not be reached in 4 tries: ${failure && (failure.cause ? failure.cause.message || failure.cause.code : failure.message)}`);
}
const newVideo = (options = {}) => control('/video?' + new URLSearchParams(options), { method: 'POST' });
const stateOf = (token) => control('/state?token=' + token);

// ---------------------------------------------------------------------------------------------
// Which simulators does this Mac have?
// ---------------------------------------------------------------------------------------------

function simulators() {
  let listing;
  try {
    listing = JSON.parse(execFileSync('xcrun', ['simctl', 'list', 'devices', 'available', '-j'], { encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 }));
  } catch (error) {
    say(`could not list simulators (xcrun simctl list devices available): ${error.message.split('\n')[0]}`);
    return [];
  }
  const all = [];
  for (const [runtime, devices] of Object.entries(listing.devices || {})) {
    const m = /SimRuntime\.iOS-(\d+)-(\d+)/.exec(runtime);
    if (!m) continue; // watchOS, tvOS, visionOS
    for (const d of devices) {
      if (d.isAvailable === false) continue;
      all.push({ name: d.name, udid: d.udid, state: d.state, ios: `${m[1]}.${m[2]}`, major: Number(m[1]), minor: Number(m[2]) });
    }
  }
  all.sort((a, b) => b.major - a.major || b.minor - a.minor || a.name.localeCompare(b.name));
  return all;
}

/** One device per size class, from whatever this Mac has. Newest iOS first. */
function pickDevices(all) {
  const number = (d) => Number((/(\d+)/.exec(d.name) || [0, 0])[1]);
  const newestFirst = (list) => list.slice().sort((a, b) => b.major - a.major || b.minor - a.minor || number(b) - number(a));
  const first = (label, ...tests) => {
    for (const t of tests) {
      const found = newestFirst(all.filter((d) => t.test(d.name)))[0];
      if (found) return { label, ...found };
    }
    return null;
  };
  return [
    first('small iPhone (SE size)', /^iPhone SE/, /^iPhone \d+ mini$/, /^iPhone \d+e$/),
    first('current iPhone', /^iPhone \d+$/, /^iPhone \d+ Pro$/),
    first('iPhone Pro Max', /^iPhone \d+ Pro Max$/, /^iPhone \d+ Plus$/),
    first('iPad', /^iPad \(/, /^iPad Air/, /^iPad Pro/, /^iPad mini/, /^iPad/),
  ].filter(Boolean);
}

function simctl(args, { timeout = 120_000 } = {}) {
  try {
    return { ok: true, out: execFileSync('xcrun', ['simctl', ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], timeout }) };
  } catch (error) {
    return { ok: false, out: String(error.stderr || error.message || '').trim().split('\n')[0] };
  }
}

function boot(device) {
  say(`booting the simulator: ${device.name} (iOS ${device.ios}, ${device.udid}) ...`);
  const started = simctl(['boot', device.udid]);
  if (!started.ok && !/current state: Booted/.test(started.out)) say(`  simctl boot said: ${started.out}`);
  // -b: boot it if it is not, and come back only when the system has finished starting.
  const ready = simctl(['bootstatus', device.udid, '-b'], { timeout: 420_000 });
  say(ready.ok ? '  booted.' : `  simctl bootstatus did not confirm the boot: ${ready.out} (carrying on)`);
}

function shutdown(device) {
  simctl(['shutdown', device.udid]);
}

/**
 * Open Safari in the simulator on a page of ours and wait until the SERVER has seen it fetch
 * that page: Safari is then running, has drawn something and has a working network, which is
 * what safaridriver needs before it can attach ("Finding or launching a compatible remote
 * Safari and waiting for its RWIApplication to appear" timed out when it had to do this itself).
 */
async function openSafariOnce(device) {
  const page = await newVideo();
  const deadline = Date.now() + 150_000;
  let opened = false;
  let last = '';
  while (Date.now() < deadline) {
    if (!opened) {
      const r = simctl(['openurl', device.udid, page.watch], { timeout: 60_000 });
      opened = r.ok;
      if (!r.ok && r.out !== last) { last = r.out; say(`  simctl openurl is not ready yet: ${r.out}`); }
    }
    if (opened) {
      const state = await stateOf(page.token);
      if (state.hits.page >= 1) {
        const ua = (state.web.find((row) => row.path === '/w/TOKEN') || {}).ua || '';
        say(`  Safari is up in the simulator and fetched a page from the server (${ua || 'no user agent recorded'})`);
        await sleep(3000); // let it finish drawing before anything attaches to it
        return true;
      }
    }
    await sleep(2000);
  }
  say(`  Safari in the simulator never fetched the page (${opened ? 'simctl openurl ran' : `simctl openurl kept failing: ${last}`})`);
  return false;
}

// ---------------------------------------------------------------------------------------------
// safaridriver: one for the whole run
// ---------------------------------------------------------------------------------------------

const safaridriver = { child: null, url: '', log: '' };

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = require('net').createServer();
    probe.once('error', reject);
    probe.listen(0, '127.0.0.1', () => { const { port } = probe.address(); probe.close(() => resolve(port)); });
  });
}

async function driverAnswers() {
  if (!safaridriver.url) return false;
  try {
    const response = await fetch(`${safaridriver.url}/status`, { signal: AbortSignal.timeout(5000) });
    return response.ok;
  } catch (error) {
    return false;
  }
}

function stopDriver() {
  if (safaridriver.child && safaridriver.child.exitCode === null) safaridriver.child.kill('SIGKILL');
  safaridriver.child = null;
  safaridriver.url = '';
}

/** The address of a safaridriver that answers, starting (or restarting) it if need be. */
async function ensureDriver({ restart = false } = {}) {
  if (!restart && safaridriver.child && safaridriver.child.exitCode === null && await driverAnswers()) return safaridriver.url;
  stopDriver();
  const port = await freePort();
  say(`starting safaridriver on port ${port}`);
  const child = spawn('safaridriver', ['--port', String(port)], { stdio: ['ignore', 'pipe', 'pipe'] });
  child.stdout.on('data', (d) => { safaridriver.log += d; });
  child.stderr.on('data', (d) => { safaridriver.log += d; });
  child.on('error', (error) => { safaridriver.log += `\n${error.message}`; });
  safaridriver.child = child;
  safaridriver.url = `http://127.0.0.1:${port}`;
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    if (await driverAnswers()) { say('  safaridriver answers.'); return safaridriver.url; }
    if (child.exitCode !== null) break;
    await sleep(500);
  }
  const why = child.exitCode !== null ? `it stopped with code ${child.exitCode}` : 'it did not answer /status in 90 s';
  stopDriver();
  throw new Error(`safaridriver would not start: ${why}. ${safaridriver.log.trim().split('\n').slice(-2).join(' ')} `
    + 'On a new Mac run "sudo safaridriver --enable" once (or Safari > Settings > Developer > Allow remote automation).');
}

// ---------------------------------------------------------------------------------------------
// Sessions
// ---------------------------------------------------------------------------------------------

/** The ways safaridriver can be told which simulator to use. All four name the one that is booted. */
function forms(device) {
  const base = { browserName: 'safari', platformName: 'iOS', 'safari:useSimulator': true };
  return [
    ['name and iOS version', { ...base, 'safari:deviceName': device.name, 'safari:platformVersion': device.ios }],
    ['name', { ...base, 'safari:deviceName': device.name }],
    ['kind of device', { ...base, 'safari:deviceType': /iPad/.test(device.name) ? 'iPad' : 'iPhone' }],
    ['UDID', { ...base, 'safari:deviceUDID': device.udid }],
  ];
}

// Which form opened a session last time, per kind of device: tried first on the next one.
const worked = { iPhone: '', iPad: '' };

async function trySession(wanted) {
  const { Builder, Capabilities } = require('selenium-webdriver');
  const url = await ensureDriver();
  say(`asking safaridriver for a session: ${JSON.stringify(wanted)}`);
  const started = Date.now();
  try {
    const driver = await within(new Builder().usingServer(url).withCapabilities(new Capabilities(wanted)).build(), SESSION_TIMEOUT, 'safaridriver creating the session');
    const got = await driver.getCapabilities();
    say(`  session open after ${Math.round((Date.now() - started) / 1000)} s: ${got.get('browserName')} ${got.get('browserVersion')} on ${got.get('platformName')}${got.get('safari:deviceName') ? `, ${got.get('safari:deviceName')}` : ''}`);
    await driver.manage().setTimeouts({ script: 60_000, pageLoad: 90_000 });
    return driver;
  } catch (error) {
    const text = String(error.message).split('\n')[0];
    say(`  no session after ${Math.round((Date.now() - started) / 1000)} s: ${text}`);
    // A request that timed out here is still running inside safaridriver, and it serves one
    // session at a time: start a clean one for the next attempt.
    if (/timed out after/.test(text)) await ensureDriver({ restart: true }).catch((e) => say(`  ${e.message}`));
    throw new Error(text);
  }
}

async function openSession(target) {
  if (!target.device) return trySession({ browserName: 'safari' });
  const kind = /iPad/.test(target.device.name) ? 'iPad' : 'iPhone';
  const ordered = forms(target.device).sort((a, b) => (b[0] === worked[kind]) - (a[0] === worked[kind]));
  let last;
  for (const [name, wanted] of ordered) {
    try {
      const driver = await trySession(wanted);
      worked[kind] = name;
      return driver;
    } catch (error) {
      last = error;
    }
  }
  throw new Error(`safaridriver would not open a session for ${target.name}: ${last && last.message}`);
}

/** Boot, wake Safari, get a session. If that fails: shut down, boot again, restart safaridriver, try once more. */
async function sessionOnSimulator(target) {
  for (let round = 1; round <= 2; round += 1) {
    if (round === 2) {
      say('  second round: shutting the simulator down, booting it again and restarting safaridriver');
      shutdown(target.device);
      await sleep(3000);
      await ensureDriver({ restart: true }).catch((e) => say(`  ${e.message}`));
    }
    boot(target.device);
    await openSafariOnce(target.device);
    try {
      return await openSession(target);
    } catch (error) {
      say(`  round ${round} of 2 failed: ${error.message}`);
    }
  }
  return null;
}

/**
 * The fallback when Safari in a simulator cannot be driven: open the page in it, wait for the
 * server to see its Safari fetch the page and the start of the video, and photograph the
 * screen. Nothing is pressed and no time is read: the result says so.
 */
async function seenNotMeasured(target) {
  const clip = await newVideo();
  say(`fallback: opening ${clip.watch.replace(/[0-9a-f]{32}/, '<token>')} with simctl openurl and taking a picture`);
  const opened = simctl(['openurl', target.device.udid, clip.watch], { timeout: 60_000 });
  if (!opened.ok) return { seen: false, text: `simctl openurl failed: ${opened.out}` };
  let state = await stateOf(clip.token);
  for (let i = 0; i < 45 && !(state.hits.page >= 1 && state.bucket.some((r) => r.status === 200 || r.status === 206)); i += 1) {
    await sleep(2000);
    state = await stateOf(clip.token);
  }
  await sleep(4000); // the first frame is drawn a moment after the bytes arrive
  fs.mkdirSync(SHOTS, { recursive: true });
  const file = path.join(SHOTS, `${target.name.replace(/[^A-Za-z0-9]+/g, '-')}--SEEN-not-measured.png`);
  const shot = simctl(['io', target.device.udid, 'screenshot', file], { timeout: 60_000 });
  const ua = (state.web.find((row) => row.path === '/w/TOKEN') || {}).ua || 'no user agent recorded';
  const fetched = state.bucket.filter((r) => r.status === 200 || r.status === 206);
  const facts = [
    state.hits.page >= 1 ? `the server saw the page fetched by "${ua}"` : 'the server never saw the page fetched',
    fetched.length ? `its player fetched the video (${fetched.map((r) => r.range || 'whole file').slice(0, 4).join(', ')})` : 'its player never asked for the video',
    shot.ok && fs.existsSync(file) ? `picture: ${path.relative(HERE, file)}` : `no picture (simctl io screenshot: ${shot.out})`,
  ];
  say(`  ${facts.join('; ')}`);
  say('  play was NOT pressed and time was NOT read: this is a picture of the page, not a measurement of playback.');
  return { seen: state.hits.page >= 1 && fetched.length > 0 && shot.ok, text: facts.join('; ') };
}

const STATE = `
  var v = document.getElementById('v');
  if (!v) return null;
  var doc = document.documentElement;
  return {
    paused: v.paused, ended: v.ended, seeking: v.seeking, muted: v.muted, time: v.currentTime, duration: v.duration,
    width: v.videoWidth, height: v.videoHeight, ready: v.readyState, error: v.error ? v.error.code : 0,
    noteShown: !document.getElementById('verr').hidden,
    pageWidth: Math.max(doc.scrollWidth, document.body.scrollWidth), windowWidth: doc.clientWidth, windowHeight: window.innerHeight,
    box: (function (r) { return { x: r.left, y: r.top, w: r.width, h: r.height }; })(v.getBoundingClientRect()),
    ua: navigator.userAgent
  };`;

async function until(driver, test, ms, what) {
  const deadline = Date.now() + ms;
  let s;
  for (;;) {
    s = await driver.executeScript(STATE);
    if (s && test(s)) return s;
    if (Date.now() > deadline) throw new Error(`${what} (last seen: ${JSON.stringify(s && { time: s.time, ready: s.ready, paused: s.paused, error: s.error })})`);
    await sleep(250);
  }
}

/** Start the video: a real click first, then script with the sound off. Says which one worked. */
async function start(driver) {
  const { By } = require('selenium-webdriver');
  try {
    await driver.findElement(By.id('v')).click();
    await sleep(1500);
    const s = await driver.executeScript(STATE);
    if (s && !s.paused) return 'a WebDriver click on the player';
    say('  a click on the player did not start it (Safari shows its own play button first); starting it by script, muted');
  } catch (error) {
    say(`  could not click the player: ${String(error.message).split('\n')[0]}; starting it by script, muted`);
  }
  await driver.executeScript("var v = document.getElementById('v'); v.muted = true; var p = v.play(); if (p && p.catch) p.catch(function () {});");
  return 'script: video.muted = true; video.play()';
}

async function watchOne(driver, url, label) {
  say(`opening ${label}: ${url.replace(/[0-9a-f]{32}/, '<token>')}`);
  await driver.get(url);
  const meta = await until(driver, (s) => s.ready >= 1 && s.duration > 0, 45_000, 'the video never loaded its metadata');
  say(`  page loaded. Safari says: ${meta.ua}`);
  say(`  window ${meta.windowWidth}x${meta.windowHeight} CSS px; page is ${meta.pageWidth} px wide; player ${Math.round(meta.box.w)}x${Math.round(meta.box.h)} at y=${Math.round(meta.box.y)}`);
  say(`  video: ${meta.width}x${meta.height}, ${meta.duration.toFixed(1)} s`);
  const problems = [];
  if (meta.pageWidth > meta.windowWidth + 1) problems.push(`the page scrolls sideways (${meta.pageWidth} px in a ${meta.windowWidth} px window)`);
  if (meta.box.y + meta.box.h > meta.windowHeight + 1 && meta.windowHeight >= 331) problems.push(`the player ends ${Math.round(meta.box.y + meta.box.h - meta.windowHeight)} px below the first screen`);
  const how = await start(driver);
  const playing = await until(driver, (s) => s.time > 1.0 || s.ended, 30_000, 'the video did not play: currentTime never passed 1 s');
  say(`  PLAYS: started by ${how}; currentTime is ${playing.time.toFixed(2)} s`);
  if (playing.error) problems.push(`the player reports error ${playing.error}`);
  if (playing.noteShown) problems.push('the "did not load" note is showing although the video plays');
  return problems;
}

async function expiredLinkProbe(driver) {
  const clip = await newVideo({ file: 'stretched', link_seconds: 4 });
  say('probe: a full-size file whose bucket link dies after 4 s; open, wait 6.5 s, then play');
  await driver.get(clip.watch);
  await until(driver, (s) => s.ready >= 1, 45_000, 'metadata');
  await sleep(6500);
  await driver.executeScript("var v = document.getElementById('v'); v.muted = true; var p = v.play(); if (p && p.catch) p.catch(function () {});");
  await sleep(12_000);
  const s = await driver.executeScript(STATE);
  const state = await stateOf(clip.token);
  const refused = state.bucket.filter((r) => r.status === 403).length;
  const verdict = s.ended || s.time > 7 ? 'PLAYED TO THE END' : `STALLED at ${s.time.toFixed(1)} of ${s.duration.toFixed(0)} s`;
  say(`  EXPIRED-LINK PROBE: ${verdict}. Safari asked /w/<token>/video ${state.hits.video} time(s); the bucket refused ${refused} request(s); `
    + `error ${s.error}; "did not load" note ${s.noteShown ? 'shown' : 'hidden'}.`);
  say(`  bucket was asked: ${state.bucket.map((r) => `${r.status} ${r.range || 'whole file'}`).slice(0, 14).join(', ')}${state.bucket.length > 14 ? ', ...' : ''}`);
  return `${verdict} (/video asked ${state.hits.video}x, ${refused} refused)`;
}

async function runTarget(target) {
  say('');
  say(`===== ${target.name} =====`);
  // status: 'pass' | 'fail' | 'seen' (opened and photographed, playback not measured)
  const result = { name: target.name, status: 'fail', problems: [], probe: '', live: '', seen: '' };
  let driver;
  try {
    driver = target.device ? await sessionOnSimulator(target) : await openSession(target);
    if (!driver) {
      const fallback = await seenNotMeasured(target);
      result.seen = fallback.text;
      if (fallback.seen) result.status = 'seen';
      else result.problems.push(`safaridriver would not open a session, and the fallback did not show the page either: ${fallback.text}`);
      return result;
    }
    const clip = await newVideo();
    result.problems.push(...await watchOne(driver, clip.watch, 'a local watch page'));
    // The page sends "played" when the picture starts, and again by itself (after 4 s, up to
    // three times) if that message did not arrive; a simulator's network can be that slow.
    let state = await stateOf(clip.token);
    for (let i = 0; i < 30 && state.played < 1; i += 1) { await sleep(500); state = await stateOf(clip.token); }
    say(`  the server recorded: "played" ${state.played} time(s); /video asked ${state.hits.video} time(s); bucket asked ${state.bucket.length} time(s): `
      + `${state.bucket.map((r) => r.range || 'whole file').slice(0, 10).join(', ')}${state.bucket.length > 10 ? ', ...' : ''}`);
    if (state.played !== 1) result.problems.push(`the server was told "played" ${state.played} times, not once`);
    if (state.bucket.some((r) => r.status >= 400)) result.problems.push('the bucket refused a request');

    fs.mkdirSync(SHOTS, { recursive: true });
    const file = path.join(SHOTS, `${target.name.replace(/[^A-Za-z0-9]+/g, '-')}.png`);
    try {
      // With a limit: on the iPad simulator this call once never came back, and the step sat
      // there until the job's own 50 minutes ran out.
      fs.writeFileSync(file, Buffer.from(await within(driver.takeScreenshot(), 60_000, 'taking a picture'), 'base64'));
      say(`  picture: ${path.relative(HERE, file)}`);
    } catch (error) {
      say(`  no picture: ${String(error.message).split('\n')[0]}`);
    }

    try {
      result.probe = await expiredLinkProbe(driver);
    } catch (error) {
      result.probe = `probe could not run: ${String(error.message).split('\n')[0]}`;
      say(`  ${result.probe}`);
    }

    if (LIVE) {
      try {
        const live = await watchOne(driver, LIVE, 'the LIVE watch page (read-only: only play is pressed)');
        result.problems.push(...live.map((p) => `LIVE: ${p}`));
        result.live = live.length ? `problems: ${live.join('; ')}` : 'plays through the real bucket';
        const shot = path.join(SHOTS, `${target.name.replace(/[^A-Za-z0-9]+/g, '-')}--LIVE.png`);
        fs.writeFileSync(shot, Buffer.from(await driver.takeScreenshot(), 'base64'));
      } catch (error) {
        result.live = `FAILED: ${String(error.message).split('\n')[0]}`;
        result.problems.push(`LIVE: ${String(error.message).split('\n')[0]}`);
      }
    }
    if (!result.problems.length) result.status = 'pass';
  } catch (error) {
    result.problems.push(String(error.message).split('\n')[0]);
    say(`  FAILED: ${error.message}`);
  } finally {
    if (driver) await within(driver.quit(), 30_000, 'closing the session').catch((e) => say(`  could not close the session cleanly: ${e.message}`));
    if (target.device) shutdown(target.device);
    const line = { pass: 'PASS', seen: `SEEN, NOT MEASURED: ${result.seen}`, fail: `FAIL: ${result.problems.join(' | ')}` }[result.status];
    say(`  RESULT: ${target.name}: ${line}`);
  }
  return result;
}

async function main() {
  if (process.platform !== 'darwin' && !SELFTEST) {
    console.error('real-safari.js needs macOS (Safari and safaridriver). In CI it runs in the "safari-mac" job.');
    process.exit(2);
  }
  try { say(`macOS ${execFileSync('sw_vers', ['-productVersion'], { encoding: 'utf8' }).trim()}`); } catch (e) { say('sw_vers failed: is this a Mac?'); }
  try { say(execFileSync('safaridriver', ['--version'], { encoding: 'utf8' }).trim()); } catch (e) { say('safaridriver --version failed: is Safari installed?'); }

  const all = simulators();
  say(`iOS simulators on this Mac (${all.length}): ${all.map((d) => `${d.name} [iOS ${d.ios}]`).join(', ') || 'none'}`);
  const picked = pickDevices(all);
  say(`chosen: ${picked.map((d) => `${d.label} = ${d.name} [iOS ${d.ios}]`).join('; ') || 'none'}`);

  let targets = [{ name: 'Safari on macOS' }, ...picked.map((device) => ({ name: `Mobile Safari, ${device.name} (iOS ${device.ios})`, device }))];
  if (ONLY.length) targets = targets.filter((t) => ONLY.some((o) => (o === 'desktop' ? !t.device : t.name.toLowerCase().includes(o.toLowerCase()))));
  const missing = ['small iPhone (SE size)', 'current iPhone', 'iPhone Pro Max', 'iPad'].filter((label) => !picked.some((d) => d.label === label));

  const server = await startServer();
  const results = [];
  try {
    // Started now, while the Mac is idle: a booting simulator takes every core for a minute.
    await ensureDriver();
    for (const target of targets) {
      // No one browser may take the whole step: nine minutes each, then on to the next.
      results.push(await within(runTarget(target), 9 * 60_000, `everything for ${target.name}`).catch((error) => {
        say(`  RESULT: ${target.name}: FAIL: ${error.message}`);
        if (target.device) shutdown(target.device);
        return { name: target.name, status: 'fail', problems: [error.message], probe: '', live: '', seen: '' };
      }));
    }
  } finally {
    stopDriver();
    server.stop();
  }

  const word = { pass: 'PASS', fail: 'FAIL', seen: 'SEEN, NOT MEASURED' };
  say('');
  say('===== SUMMARY =====');
  for (const r of results) {
    say(`${word[r.status]}  ${r.name}${r.status === 'fail' ? `: ${r.problems.join(' | ')}` : ''}`);
    if (r.status === 'seen') say(`      Safari could not be driven there. ${r.seen}. Playback was not measured.`);
    if (r.probe) say(`      expired link: ${r.probe}`);
    if (r.live) say(`      live: ${r.live}`);
  }
  if (missing.length && !ONLY.length) say(`NOT COVERED: this Mac has no simulator for: ${missing.join(', ')}`);
  const seenOnly = results.filter((r) => r.status === 'seen');
  if (seenOnly.length) {
    say(`NOT MEASURED: ${seenOnly.map((r) => r.name).join(', ')}. A picture was taken; playback there rests on the WebKit-on-macOS run of the same sizes.`);
    // Shown at the top of the CI run, so that a green step is not read as "the iPad played".
    if (process.env.GITHUB_ACTIONS) console.log(`::warning title=Real Safari was seen but not measured::${seenOnly.map((r) => r.name).join(', ')}: safaridriver would not open a session; the page was opened and photographed instead. See the step summary.`);
  }
  fs.mkdirSync(SHOTS, { recursive: true });
  fs.writeFileSync(path.join(SHOTS, 'log.txt'), lines.join('\n') + '\n');
  if (safaridriver.log.trim()) fs.writeFileSync(path.join(SHOTS, 'safaridriver.txt'), safaridriver.log);
  if (process.env.GITHUB_STEP_SUMMARY) {
    const cell = (r) => ({ pass: 'pass', seen: `SEEN, NOT MEASURED (a picture; play was not pressed): ${r.seen}`, fail: `FAIL: ${r.problems.join('; ')}` }[r.status]);
    const table = ['### Real Safari', '', '| Target | Result | Expired link | Live |', '| --- | --- | --- | --- |',
      ...results.map((r) => `| ${r.name} | ${cell(r)} | ${r.probe || '-'} | ${r.live || '-'} |`),
      ...(missing.length && !ONLY.length ? ['', `No simulator on this runner for: ${missing.join(', ')}`] : [])];
    fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, table.join('\n') + '\n');
  }
  const failed = results.filter((r) => r.status === 'fail').length;
  // A size class with no simulator is a hole in the proof, so it fails too. A target that was
  // only seen fails when SAFARI_STRICT=1; otherwise it is reported loudly and does not.
  process.exit(failed || (missing.length && !ONLY.length) || !results.length || (STRICT && seenOnly.length) ? 1 : 0);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
