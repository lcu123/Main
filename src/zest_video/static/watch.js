// Tells the server the customer really pressed play (link scanners that only open
// the page must not count as "watched"). Nothing else on the page needs script.
(function () {
  var video = document.getElementById("v");
  if (!video) return;
  var told = false;
  video.addEventListener("play", function () {
    if (told) return;
    told = true;
    try {
      fetch(video.getAttribute("data-played"), { method: "POST", keepalive: true });
    } catch (e) {}
  });
  video.addEventListener("error", function () {
    var box = document.getElementById("verr");
    if (box) box.hidden = false;
  });
})();
