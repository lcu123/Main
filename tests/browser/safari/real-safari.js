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
// expired (reported, never failed: that answer is what lib/known-gaps.js is waiting for).
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
const SESSION_TIMEOUT = Number(process.env.SAFARI_SESSION_TIMEOUT_MS || 240_000);

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
  const response = await fetch(`http://127.0.0.1:${PORT}/__test${pathAndQuery}`, options);
  if (!response.ok) throw new Error(`serve.py ${pathAndQuery}: ${response.status} ${await response.text()}`);
  return response.json();
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

function boot(device) {
  if (device.state === 'Booted') { say(`simulator already booted: ${device.name}`); return; }
  say(`booting the simulator: ${device.name} (iOS ${device.ios}, ${device.udid}) ...`);
  try {
    execFileSync('xcrun', ['simctl', 'boot', device.udid], { stdio: 'pipe' });
  } catch (error) {
    const text = String(error.stderr || error.message);
    if (!/current state: Booted/.test(text)) say(`  simctl boot said: ${text.trim().split('\n')[0]}`);
  }
  try {
    execFileSync('xcrun', ['simctl', 'bootstatus', device.udid, '-b'], { stdio: 'pipe', timeout: 300_000 });
    say('  booted.');
  } catch (error) {
    say(`  simctl bootstatus did not confirm the boot: ${String(error.message).split('\n')[0]} (carrying on; safaridriver may still find it)`);
  }
}

function shutdown(device) {
  try { execFileSync('xcrun', ['simctl', 'shutdown', device.udid], { stdio: 'pipe' }); } catch (e) { /* already off */ }
}

// ---------------------------------------------------------------------------------------------
// Sessions
// ---------------------------------------------------------------------------------------------

async function openSession(target) {
  const { Builder, Capabilities } = require('selenium-webdriver');
  const attempts = [];
  if (!target.device) attempts.push({ browserName: 'safari' });
  else {
    const d = target.device;
    attempts.push({ browserName: 'safari', platformName: 'iOS', 'safari:useSimulator': true, 'safari:deviceUDID': d.udid });
    attempts.push({ browserName: 'safari', platformName: 'iOS', 'safari:useSimulator': true, 'safari:deviceName': d.name, 'safari:platformVersion': d.ios });
    attempts.push({ browserName: 'safari', platformName: 'iOS', 'safari:useSimulator': true, 'safari:deviceName': d.name });
    attempts.push({ browserName: 'safari', platformName: 'iOS', 'safari:useSimulator': true, 'safari:deviceType': /iPad/.test(d.name) ? 'iPad' : 'iPhone' });
  }
  let last;
  for (const wanted of attempts) {
    say(`asking safaridriver for a session: ${JSON.stringify(wanted)}`);
    try {
      const driver = await within(new Builder().withCapabilities(new Capabilities(wanted)).build(), SESSION_TIMEOUT, 'safaridriver creating the session');
      const got = await driver.getCapabilities();
      say(`  session open: ${got.get('browserName')} ${got.get('browserVersion')} on ${got.get('platformName')}${got.get('safari:deviceName') ? `, ${got.get('safari:deviceName')}` : ''}`);
      await driver.manage().setTimeouts({ script: 60_000, pageLoad: 90_000 });
      return driver;
    } catch (error) {
      last = error;
      say(`  no session: ${String(error.message).split('\n')[0]}`);
    }
  }
  throw new Error(`safaridriver would not open a session for ${target.name}: ${String(last && last.message).split('\n')[0]}. `
    + 'On a new Mac run "sudo safaridriver --enable" once (or Safari > Settings > Developer > Allow remote automation).');
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
  if (meta.pageWidth > meta.windowWidth + 1 && meta.windowWidth >= 326) problems.push(`the page scrolls sideways (${meta.pageWidth} px in a ${meta.windowWidth} px window)`);
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
  const result = { name: target.name, ok: false, problems: [], probe: '', live: '' };
  let driver;
  try {
    if (target.device) boot(target.device);
    driver = await openSession(target);
    const clip = await newVideo();
    result.problems.push(...await watchOne(driver, clip.watch, 'a local watch page'));
    let state = await stateOf(clip.token);
    for (let i = 0; i < 12 && state.played < 1; i += 1) { await sleep(250); state = await stateOf(clip.token); }
    say(`  the server recorded: "played" ${state.played} time(s); /video asked ${state.hits.video} time(s); bucket asked ${state.bucket.length} time(s): `
      + `${state.bucket.map((r) => r.range || 'whole file').slice(0, 10).join(', ')}${state.bucket.length > 10 ? ', ...' : ''}`);
    if (state.played !== 1) result.problems.push(`the server was told "played" ${state.played} times, not once`);
    if (state.bucket.some((r) => r.status >= 400)) result.problems.push('the bucket refused a request');

    fs.mkdirSync(SHOTS, { recursive: true });
    const file = path.join(SHOTS, `${target.name.replace(/[^A-Za-z0-9]+/g, '-')}.png`);
    fs.writeFileSync(file, Buffer.from(await driver.takeScreenshot(), 'base64'));
    say(`  picture: ${path.relative(HERE, file)}`);

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
    result.ok = result.problems.length === 0;
  } catch (error) {
    result.problems.push(String(error.message).split('\n')[0]);
    say(`  FAILED: ${error.message}`);
  } finally {
    if (driver) await within(driver.quit(), 30_000, 'closing the session').catch((e) => say(`  could not close the session cleanly: ${e.message}`));
    if (target.device) shutdown(target.device);
  }
  say(result.ok ? `  RESULT: ${target.name}: PASS` : `  RESULT: ${target.name}: FAIL: ${result.problems.join(' | ')}`);
  return result;
}

async function main() {
  if (process.platform !== 'darwin') {
    console.error('real-safari.js needs macOS (Safari and safaridriver). In CI it runs in the "safari-mac" job.');
    process.exit(2);
  }
  say(`macOS ${execFileSync('sw_vers', ['-productVersion'], { encoding: 'utf8' }).trim()}`);
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
    for (const target of targets) results.push(await runTarget(target));
  } finally {
    server.stop();
  }

  say('');
  say('===== SUMMARY =====');
  for (const r of results) {
    say(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}${r.ok ? '' : `: ${r.problems.join(' | ')}`}`);
    if (r.probe) say(`      expired link: ${r.probe}`);
    if (r.live) say(`      live: ${r.live}`);
  }
  if (missing.length && !ONLY.length) say(`NOT COVERED: this Mac has no simulator for: ${missing.join(', ')}`);
  fs.mkdirSync(SHOTS, { recursive: true });
  fs.writeFileSync(path.join(SHOTS, 'log.txt'), lines.join('\n') + '\n');
  if (process.env.GITHUB_STEP_SUMMARY) {
    const table = ['### Real Safari', '', '| Target | Result | Expired link | Live |', '| --- | --- | --- | --- |',
      ...results.map((r) => `| ${r.name} | ${r.ok ? 'pass' : `FAIL: ${r.problems.join('; ')}`} | ${r.probe || '-'} | ${r.live || '-'} |`),
      ...(missing.length && !ONLY.length ? ['', `No simulator on this runner for: ${missing.join(', ')}`] : [])];
    fs.appendFileSync(process.env.GITHUB_STEP_SUMMARY, table.join('\n') + '\n');
  }
  const failed = results.filter((r) => !r.ok).length;
  // A size class with no simulator is a hole in the proof, so it fails too.
  process.exit(failed || (missing.length && !ONLY.length) || !results.length ? 1 : 0);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
