"""The customer side, server half: what the watch page, its player and the email are sent.

The browsers themselves are driven in tests/browser (Chromium, Firefox, WebKit, real
Safari). These tests pin the server behaviour those pages lean on, each of which came
out of the cross-browser review of Nov 13, 2026.
"""

from __future__ import annotations

import asyncio
import re
import secrets

import httpx
import pytest
import sqlalchemy as sa

from video_helpers import Env, film, login, make_env, office_login, video_body

from zest_video import db, videos
from zest_video.videos import video_format


@pytest.fixture
async def env(tmp_path) -> Env:
    e = await make_env(tmp_path)
    yield e
    await e.http.aclose()
    await e.ctx.engine.dispose()


async def sent(env: Env, customer_id: int = 501):
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    env.mailer.sent.clear()
    video_id = await film(env, headers, customer_id)
    await env.window_closed(video_id)
    await videos.send_due(env.ctx)
    return await env.video(video_id), headers


def mp4(video: str = "avc1", *, at_end: bool = False, pad: int = 4000) -> bytes:
    """Just enough of an MP4 for the format check: two sample tables, sound and picture."""
    def table(fmt: str) -> bytes:
        return b"\x00\x00\x00\x30stsd" + b"\x00" * 4 + b"\x00\x00\x00\x01" + b"\x00\x00\x00\x20" + fmt.encode() + b"\x00" * 16
    tables = b"moov" + table("mp4a") + table(video)
    body = b"\x01\x02\x03\x04" * (pad // 4)
    return b"\x00\x00\x00\x18ftypmp42" + (body + tables if at_end else tables + body)


async def upload(env: Env, headers: dict[str, str], data: bytes, customer_id: int = 501) -> str:
    body = video_body(customer_id, sizeBytes=len(data))
    r = await env.http.post("/v1/videos", json=body, headers=headers)
    assert r.status_code == 200, r.text
    env.storage.objects[(await env.video(body["id"])).object_key] = data
    r = await env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": len(data)}, headers=headers)
    assert r.status_code == 200, r.text
    return body["id"]


# -- a video some customers could not play ---------------------------------------------


def test_the_format_check_reads_the_picture_codec_and_ignores_the_sound() -> None:
    assert video_format(mp4("avc1")) == "avc1"
    assert video_format(mp4("hvc1")) == "hvc1"
    assert video_format(mp4("hev1", at_end=True)) == "hev1"
    assert video_format(b"") is None and video_format(b"v" * 5000) is None
    assert video_format(b"xxstsd") is None, "the word at the very end of a slice, nothing after it"
    assert video_format(b"...stsd" + b"\xff" * 40) is None, "the letters turning up by chance in picture data"


async def test_a_video_recorded_as_hevc_is_flagged_for_the_office_and_still_goes_out(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    env.mailer.sent.clear()
    good = await upload(env, headers, mp4("avc1"), 501)
    assert await env.flags("bad_format") == [], "H.264 is what the app records: no flag"

    bad = await upload(env, headers, mp4("hvc1"), 502)
    (flag,) = await env.flags("bad_format")
    assert flag.video_id == bad and "HEVC" in flag.detail and "black picture" in flag.detail
    assert (await env.video(bad)).state == "held", "flagged, never held back: most phones and every Apple device play it"

    for video_id in (good, bad):
        await env.window_closed(video_id)
    while await videos.send_due(env.ctx):
        pass
    assert len(env.mailer.sent) == 2

    csrf = await office_login(env)
    page = (await env.http.get("/office")).text
    assert "Video may not play for every customer" in page and csrf


async def test_the_format_check_finds_the_tables_at_the_end_of_a_big_file_and_never_fails_an_upload(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    late = await upload(env, headers, mp4("hvc1", at_end=True, pad=600_000), 501)
    assert [f.video_id for f in await env.flags("bad_format")] == [late]

    async def broken(key: str, start: int, length: int) -> bytes | None:
        raise httpx.ConnectError("bucket unreachable")

    env.storage.read_range = broken  # type: ignore[method-assign]
    fine = await upload(env, headers, mp4("hvc1"), 502)
    assert (await env.video(fine)).state == "held", "storage trouble during the peek must not fail a confirmed upload"
    assert len(await env.flags("bad_format")) == 1


# -- the player's own requests ---------------------------------------------------------


async def test_playing_a_video_does_not_use_up_the_allowance_for_opening_the_page(env: Env) -> None:
    video, _ = await sent(env)
    env.ctx.limiter.reset()
    for _ in range(300):  # a player on a weak link asks for the video again and again
        assert (await env.http.get(f"/w/{video.token}/video")).status_code == 302
    assert (await env.http.get(f"/w/{video.token}")).status_code == 200, "the page still opens"


async def test_an_address_that_opens_the_link_too_fast_is_asked_to_wait_not_told_the_video_is_gone(env: Env) -> None:
    video, _ = await sent(env)
    env.ctx.limiter.reset()
    for _ in range(120):
        assert (await env.http.get(f"/w/{video.token}")).status_code == 200
    r = await env.http.get(f"/w/{video.token}")
    assert r.status_code == 429 and 55 <= int(r.headers["retry-after"]) <= 61
    assert "Please wait a minute" in r.text and "no longer available" not in r.text
    assert "Wait a minute, then open your link again." in r.text
    assert "tel:" in r.text, "and the office number is still there"
    env.ctx.limiter.reset()
    assert (await env.http.get(f"/w/{video.token}")).status_code == 200, "the video itself was never touched"


async def test_the_wait_page_blames_the_connection_not_the_link_and_gives_the_real_wait(env: Env) -> None:
    """The count is per address. A neighbour on the same connection who has never opened
    their own link must not read that "this link" was opened many times."""
    mine, _ = await sent(env)
    env.ctx.limiter.reset()
    for _ in range(120):
        await env.http.get(f"/w/{mine.token}")
    r = await env.http.get("/w/" + "ab" * 16)  # another link, same address
    assert r.status_code == 429
    assert "from your internet connection" in r.text and "This link has been opened" not in r.text

    env.ctx.limiter.reset()
    for _ in range(20):  # twenty links that do not exist: ten minutes, not one
        assert (await env.http.get("/w/" + secrets.token_hex(16))).status_code == 404
    r = await env.http.get("/w/" + secrets.token_hex(16))
    assert r.status_code == 429 and 590 <= int(r.headers["retry-after"]) <= 601
    assert "Please wait a few minutes" in r.text and "Wait 10 minutes, then open your link again." in r.text


async def test_the_player_is_sent_to_a_fresh_signed_link_every_time_it_asks(env: Env) -> None:
    video, _ = await sent(env)
    first = await env.http.get(f"/w/{video.token}/video")
    again = await env.http.get(f"/w/{video.token}/video", headers={"Range": "bytes=1114112-"})
    for r in (first, again):
        assert r.status_code == 302 and r.headers["cache-control"] == "no-store"
        assert r.headers["location"].startswith("https://"), "media-src allows https: only"
    head = await env.http.head(f"/w/{video.token}/video")
    assert head.status_code == 302


# -- addresses that only take a form post ----------------------------------------------


async def test_opening_the_not_my_home_address_never_takes_a_video_down(env: Env) -> None:
    """A link scanner, a reload or a Back press sends a GET. Only the form's POST may act."""
    video, _ = await sent(env)
    for method in ("GET", "HEAD"):
        r = await env.http.request(method, f"/w/{video.token}/not-my-home")
        assert r.status_code == 303 and r.headers["location"] == f"/w/{video.token}", method
    assert (await env.video(video.id)).state == "sent" and await env.flags("not_my_home") == []

    r = await env.http.post(f"/w/{video.token}/not-my-home")
    assert r.status_code == 200 and "Thank you" in r.text
    assert (await env.video(video.id)).state == "pulled"
    # Reloading that address afterwards lands on the styled page for a dead link.
    r = await env.http.get(f"/w/{video.token}/not-my-home", follow_redirects=True)
    assert r.status_code == 410 and "no longer available" in r.text and "tel:" in r.text


async def test_opening_the_unsubscribe_link_again_says_done_instead_of_asking_twice(env: Env) -> None:
    video, _ = await sent(env)
    first = await env.http.get(f"/u/{video.token}")
    assert "Stop video emails?" in first.text
    assert (await env.http.post(f"/u/{video.token}")).status_code == 200
    again = await env.http.get(f"/u/{video.token}")
    assert "Done" in again.text and "Stop video emails?" not in again.text
    assert len(await env.all(sa.select(db.email_optouts))) == 1
    assert (await env.http.post(f"/u/{video.token}")).status_code == 200, "a second post is harmless"
    assert len(await env.all(sa.select(db.email_optouts))) == 1


# -- two Sends in the same instant -----------------------------------------------------


async def test_two_sends_at_the_same_instant_save_one_score_and_show_no_error(env: Env) -> None:
    video, _ = await sent(env)
    answers = await asyncio.gather(*(
        env.http.post(f"/w/{video.token}/score", data={"stars": "5", "comment": "Great"}) for _ in range(6)
    ))
    assert {r.status_code for r in answers} == {303}, [r.status_code for r in answers]
    (score,) = await env.all(sa.select(db.scores))
    assert score.stars == 5
    reviews = [r for r in await env.all(sa.select(db.fr_outbox)) if r.kind == "review"]
    assert len(reviews) == 1, "FieldRoutes gets one review however many Sends arrived"


# -- what the page itself is made of ---------------------------------------------------


async def test_the_watch_page_carries_everything_the_browsers_need(env: Env) -> None:
    video, _ = await sent(env)
    r = await env.http.get(f"/w/{video.token}")
    page = r.text
    csp = r.headers["content-security-policy"]
    assert "img-src 'self' data: blob:;" in csp, "Safari's own player draws some icons from blob: addresses"
    assert "script-src 'self'" in csp and "default-src 'none'" in csp and "media-src 'self' https:" in csp
    assert "'unsafe-eval'" not in csp and "script-src 'self' " not in csp.replace("script-src 'self';", "")

    player = re.search(r"<video[^>]*>", page).group(0)
    for needed in ('id="v"', "controls", "playsinline", 'preload="metadata"', 'width="720"', 'height="1280"',
                   f'src="/w/{video.token}/video#t=0.1"', f'data-played="/w/{video.token}/played"'):
        assert needed in player, needed
    # The script is told how long the player's storage link lives, a little under the real life.
    life = int(re.search(r'data-life="(\d+)"', player).group(1))
    assert 0.8 * env.ctx.cfg.watch_url_seconds <= life < env.ctx.cfg.watch_url_seconds
    assert "autoplay" not in player and "disableremoteplayback" not in player.lower() and "x-webkit-airplay" not in player
    # The note is a real button with both its messages in the HTML (the script holds no words).
    # It sits in a region that is always there, so a screen reader says it when it appears.
    note = re.search(r'<div role="status"><button type="button" id="verr"[^>]*>([^<]*)</button></div>', page)
    assert note and "hidden" in note.group(0) and "try again" in note.group(1)
    assert "data-slow=" in note.group(0) and "data-trying=" in note.group(0)
    assert f'<noscript><p class="gap"><a href="/w/{video.token}/video">' in page
    # Five drawn stars, written 5 to 1, each a labelled radio.
    assert re.findall(r'<input type="radio" name="stars" id="s(\d)"', page) == ["5", "4", "3", "2", "1"]
    assert page.count("<svg") == 5 and "★" not in page, "no typed star: its width depends on the device's fonts"
    # Each star's name is text inside its label (a label may not carry aria-label), hidden from sight.
    assert page.count('<span class="vh">1 star</span></label>') == 1 and page.count('<span class="vh">5 stars</span></label>') == 1
    assert "<label" in page and "aria-label=" not in "".join(re.findall(r"<label[^>]*>", page))
    assert ".stars label{" in page and "color:transparent" in page, "an unpicked star has no fill, so a darkened page keeps it hollow"
    # The office number is on the button, in one piece, and still dials on a phone.
    assert '<a class="btn ghost gap" href="tel:+19165550100">Call our office <span class="nb">(916) 555-0100</span></a>' in page
    assert 'src="/static/watch.js' in page and " defer>" in page and "<script>" not in page, "no inline script"
    # Nothing on the page asks who is looking.
    assert "navigator.userAgent" not in (await env.http.get("/static/watch.js")).text


async def test_the_dead_link_page_shows_the_number_too(env: Env) -> None:
    r = await env.http.get("/w/" + "0" * 32)
    assert r.status_code == 404
    assert 'href="tel:+19165550100">Call our office <span class="nb">(916) 555-0100</span></a>' in r.text


async def test_the_email_button_survives_outlook_and_phones(env: Env) -> None:
    await sent(env)
    html = env.mailer.sent[-1].html
    assert html.count("<!--[if mso]>") == html.count("<![endif]-->") == 2, "conditional comments are balanced"
    button = re.search(r'<td align="center" bgcolor="#6E20B9"[^>]*>\s*<a href="([^"]+)"[^>]*>([^<]*)</a>', html)
    assert button, "the coloured cell wraps the link, so classic Outlook draws a bar, not highlighted words"
    assert "mso-padding-alt:18px 20px" in button.group(0)
    assert button.group(1).startswith("https://video.test/w/") and "Watch your video" in button.group(2)
    assert "&#9658;" in html and "&#9654;" not in html and "▶" not in html, "U+25BA never turns into a colour emoji"
    assert "<style" not in html and "<script" not in html and "<link" not in html
