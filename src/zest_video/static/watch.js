// The watch page's only script. The page works without it (the browser's own player, plain
// forms); this makes it kinder when something goes wrong.
//
// Plain ES5 on purpose: 2017 smart TVs and old iPhones run it too, and
// tests/browser/static/es5.spec.js fails the build if anything newer slips in.
(function () {
  var video = document.getElementById("v");
  if (!video) return;
  var box = document.getElementById("verr");
  var src = (video.getAttribute("src") || "").split("#")[0];
  var FAILED = box ? box.textContent : "";
  var SLOW = box ? box.getAttribute("data-slow") || "" : "";
  var TRYING = box ? box.getAttribute("data-trying") || "" : "";
  // A little under the life of the storage link the player was sent to.
  var LIFE = (parseInt(video.getAttribute("data-life"), 10) || 810) * 1000;
  var told = false;   // the server has been told play was pressed
  var tries = 0;      // times that message has been sent again by itself
  var wanted = false; // the customer pressed play and has not paused since
  var run = 0;        // automatic retries since the picture last moved
  var total = 0;      // automatic retries on this page, ever (a hard stop)
  var presses = 0;    // presses of the "try again" note in a row
  var pressed = 0;    // when the note was last pressed
  var mark = 0;       // where the picture was at the last error
  var place = 0;      // where the picture was at the last reload (a failed load forgets it)
  var born = now();   // when the link the player holds was signed
  var stuck = -1;     // where the picture was when it was last seen to move (-1: not looking)
  var since = 0;      // when that was
  var shut = false;   // the customer put the weak-signal note away; it stays away until the picture moves
  var slow = 0;
  var late = 0;

  function now() { return new Date().getTime(); }
  function where() { return video.readyState ? video.currentTime || 0 : place; }

  function below() {
    if (!box || box.hidden) return;
    var h = window.innerHeight || document.documentElement.clientHeight;
    // Only when it is under the screen. A customer who has scrolled on to the comment box is
    // left where they are.
    if (box.getBoundingClientRect().bottom > h) box.scrollIntoView(false);
  }
  function say(text) {
    if (!box || !text) return;
    box.textContent = text;
    box.hidden = false;
    if (text !== FAILED) return;
    // A dead player must not hide the way out. Leave full screen; and where the player fills
    // the screen (a phone on its side, a zoomed page) bring the note into view.
    try {
      var p = null;
      if (document.fullscreenElement && document.exitFullscreen) p = document.exitFullscreen();
      else if (document.webkitFullscreenElement && document.webkitExitFullscreen) document.webkitExitFullscreen();
      else if (video.webkitDisplayingFullscreen && video.webkitExitFullscreen) video.webkitExitFullscreen();
      if (p && p["catch"]) p["catch"](function () {});
    } catch (e) {}
    below();
    setTimeout(below, 400); // and once more when full screen has let go
  }
  function hide() {
    if (!box || box.hidden) return;
    // Hiding the note while it holds the focus would drop a keyboard or a TV remote back to
    // the top of the page: hand the focus to the player.
    var held = document.activeElement === box;
    box.hidden = true;
    if (held) { try { video.focus(); } catch (e) {} }
  }
  function quiet(keep) {
    clearTimeout(slow);
    clearTimeout(late);
    stuck = -1;
    if (!keep) hide();
  }

  // A weak signal. While the video is meant to be playing the page looks at it once a second.
  // If the picture has not moved for six seconds it says so; when it moves again the note
  // goes. It does not lean on the browser's own events for this: Firefox sends "waiting" and
  // the picture's last step in orders that left a frozen video with no word at all.
  function look() {
    clearTimeout(slow);
    if (video.error || video.paused || video.ended) { stuck = -1; return; }
    if (video.currentTime !== stuck) {
      stuck = video.currentTime;
      since = now();
      shut = false;
      if (box && !box.hidden && box.textContent === SLOW) hide();
    } else if (!shut && now() - since >= 6000 && box && (box.hidden || box.textContent !== SLOW)) {
      say(SLOW);
    }
    slow = setTimeout(look, 1000);
  }
  function watch() {
    if (stuck < 0) { stuck = video.currentTime; since = now(); shut = false; }
    clearTimeout(slow);
    slow = setTimeout(look, 1000);
  }
  function start() { var p = video.play(); if (p && p["catch"]) p["catch"](function () {}); }

  // Link scanners and mail gateways open the page; only a person presses play. Sent when
  // play is pressed on a video that has loaded, and again when the picture starts. If the
  // message does not arrive it is sent again a few times, so one dropped request does not
  // leave a watched video marked "sent".
  function again() { told = false; if (tries < 3) { tries += 1; setTimeout(tell, 4000); } }
  function tell() {
    if (told) return;
    told = true;
    try {
      var p = fetch(video.getAttribute("data-played"), { method: "POST", keepalive: true });
      if (p && p.then) p.then(function (r) { if (!r.ok) again(); }, again);
    } catch (e) { again(); }
  }

  // The player is redirected to a storage link that lives 15 minutes. Chrome, Edge, Samsung
  // Internet and Android mail apps keep using that link, so a page left open longer fails
  // on the first press of play, or on a seek. Asking our own address again makes the server
  // sign a new link; the picture carries on from the same second.
  function fresh(play, keep) {
    var at = where();
    place = at;
    born = now();
    quiet(keep);
    video.src = src + "#t=" + (at > 0.1 ? at.toFixed(1) : "0.1");
    video.load();
    if (play) start();
  }

  function failed() {
    clearTimeout(slow);
    clearTimeout(late);
    mark = where();
    // Twice in a row without asking, and never more than six times on one page: a file that
    // is really broken must end at the note, not in a loop.
    if (run < 2 && total < 6) { run += 1; total += 1; fresh(wanted); return; }
    say(FAILED);
  }

  // Chrome asks a dead link again and again for half a minute before it reports an error,
  // and the picture stands still all that time. If the player is stuck and its link is past
  // its life, ask for a new one now.
  function overdue() {
    clearTimeout(late);
    if (now() - born < LIFE) return;
    var at = video.currentTime;
    late = setTimeout(function () {
      if (video.error || video.ended || total >= 6 || video.currentTime !== at) return;
      if (video.seeking || video.readyState < 3) { total += 1; fresh(wanted); }
    }, 1500);
  }

  video.addEventListener("play", function () {
    if (video.error) return; // a tap on a dead player is not a viewing
    wanted = true;
    if (video.readyState > 0) tell(); // nor is a press on a player with nothing loaded
    watch();
  });
  video.addEventListener("playing", function () {
    if (video.error) return;
    clearTimeout(late);
    if (box && box.textContent !== SLOW) hide(); // the weak-signal note goes when the picture is seen to move
    presses = 0;
    tell();
    watch();
  });
  video.addEventListener("loadeddata", function () {
    // A try by hand that loaded but was not allowed to start: the player is ready, one tap plays.
    if (!video.error && box && TRYING && box.textContent === TRYING) hide();
  });
  video.addEventListener("pause", function () {
    clearTimeout(slow);
    stuck = -1;
    if (box && box.textContent === SLOW) hide(); // paused, it will not "carry on by itself"
    if (!video.error) wanted = false;
  });
  video.addEventListener("ended", function () { wanted = false; run = 0; });
  video.addEventListener("waiting", function () {
    watch();
    overdue();
  });
  video.addEventListener("seeking", overdue);
  video.addEventListener("timeupdate", function () {
    if (video.error) return;
    if (run && video.currentTime > mark + 3) run = 0; // it really got going again
  });
  video.addEventListener("error", failed);
  // The signal is back: carry on by itself, as the weak-signal note promises.
  window.addEventListener("online", function () {
    if (video.error && total < 6) { total += 1; fresh(wanted); }
  });

  function press() {
    var t = now();
    if (t - pressed < 1000) return; // a double tap is one press
    pressed = t;
    if (!video.error) { shut = true; hide(); return; } // the weak-signal note: put away, nothing else
    presses += 1;
    // A second press in a row opens the page again: if the video was taken down, that page
    // says so. Not while the device says it is offline: that would swap this page for the
    // browser's own "no internet" page.
    if (presses > 1 && navigator.onLine !== false) { location.reload(); return; }
    wanted = true;
    run = 2; // one try by hand; if it fails the note comes straight back
    // The note stays where it is while the try runs, so nothing moves under the finger and
    // a keyboard or a remote keeps its place.
    if (TRYING) box.textContent = TRYING;
    fresh(true, true);
  }
  if (box) box.addEventListener("click", press);

  // A TV remote's OK button is Enter; in a TV browser's focus mode the player ignores it.
  // Play and pause keys on a remote or a keyboard work wherever the focus is. On a player
  // that has failed, each of them tries again.
  function toggle() {
    if (video.error && box && !box.hidden) { press(); return; }
    if (video.paused || video.ended) start(); else video.pause();
  }
  function held(e) { return e.altKey || e.ctrlKey || e.metaKey; }
  // Firefox sends the keys pressed on the player's own buttons (mute, full screen) to the
  // page as if they were pressed on the video. Those belong to the button.
  function own(e) {
    try { var o = e.originalTarget; return !!o && o !== video; } catch (x) { return true; }
  }
  video.addEventListener("keydown", function (e) {
    if (e.keyCode !== 13 || held(e) || own(e)) return;
    e.preventDefault();
    toggle();
  });
  document.addEventListener("keydown", function (e) {
    var k = e.keyCode;
    if (held(e)) return;
    if (k === 415) { if (video.error) toggle(); else start(); }
    else if (k === 19 || k === 413) video.pause();
    else if (k === 179 || k === 10252) toggle();
  });

  // The stars are drawn right to left so that picking one fills those before it with no
  // script. That turns Left and Right around for a keyboard or a remote; put them back. Enter
  // on a star picks it; it must not send the form before the customer has chosen.
  var stars = document.querySelector(".stars");
  if (stars) stars.addEventListener("keydown", function (e) {
    var k = e.keyCode, t = e.target;
    if (!t || t.name !== "stars" || held(e)) return; // Alt+Left is the browser's Back
    if (k === 13) { e.preventDefault(); t.checked = true; return; }
    var step = k === 39 ? 1 : k === 37 ? -1 : 0;
    if (!step) return;
    e.preventDefault();
    var next = t.checked ? document.getElementById("s" + (parseInt(t.value, 10) + step)) : t;
    if (next) { next.checked = true; next.focus(); }
  });

  if (video.error) failed();                          // it failed before this script arrived
  else if (!video.paused) { wanted = true; tell(); }  // play was pressed before this script arrived
})();
