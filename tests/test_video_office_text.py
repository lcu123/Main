"""Office sign-in by text: type a mobile number, get a 6-digit code through Quo, type the code.

The owner's ask (Nov 16, 2026): the office page sent its sign-in code by email only, and
no mail service is connected on the live server, so no code ever reached him. "Login via
Quo. Not email." He types his mobile number, the server texts the code over the same Quo
line that texts a technician's code, and the code opens the same office session as before.

What these tests hold it to:

- one text, with the code, and the code signs him in;
- nothing is given away: a number nobody has, the number of a user who was removed and a
  listed number get the same page and the same answers, at both steps, in the same time,
  and only the listed one gets a text;
- it is the same kind of code as the emailed one: newest only, 15 minutes, locked after 5
  wrong tries, and a code asked for one way retires one asked for the other. Each is taken
  only at the code step of the way it was asked for, so neither step tells on the other;
- only an owner gives out a number, changes it or takes it away, a number belongs to one
  user, and taking it away stops it at once;
- the owner's own number comes from VIDEO_OWNER_PHONES at every start, safe to repeat, and
  a bad entry there never stops a start;
- when a text cannot be sent the page says so, the log says so in one line, and the page
  points to the email way, which works as it always did. It points there only on a server
  where an emailed code reaches an inbox: the live server had no mail service when this
  was built, and a page that promised an email would have sent the owner back to where
  he began;
- the office's own line, the one the codes are texted from, is refused in plain words;
- no whole phone number in a log line or an audit entry, and no code in a log line;
- a server started on a database from before this change starts and works.

Every number here is a made-up 555-01xx one and every address is made up.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
import sqlalchemy as sa

from conftest import _client_for
from video_helpers import (
    BASE, OWNER, QUO_KEY, QUO_LINE, STAFF, Env, FakeQuo, code_from, login, make_env, office_login, use_quo,
)

from zest_video import app as video_app
from zest_video import auth, config, db, security
from zest_video.app import build_app, build_ctx
from zest_video.notify import DeliveryError, LogMailer, Mailer, MemoryTexter, Texter

OWNER_PHONE = "9165550142"  # the owner's mobile, from VIDEO_OWNER_PHONES
STAFF_PHONE = "9165550143"  # a number the owner gives a staff user on the Users page
OTHER_PHONE = "9165550144"
STRANGER = "9165550177"  # a number nobody has
SECOND = "second@zest.test"  # one more staff user


@pytest.fixture(autouse=True)
def not_this_machines_own_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """A start is run in these tests, so Railway's own variables and the start-up overrides
    of the machine the tests run on must not be taken for the server's."""
    for name in list(os.environ):
        if name.startswith("RAILWAY_") or name in (
            "VIDEO_ALLOW_THROWAWAY_STORAGE", "VIDEO_START_WITH_FILES_MISSING", "VIDEO_START_WITH_NEW_DATABASE",
        ):
            monkeypatch.delenv(name)


@pytest.fixture
async def env(tmp_path) -> Env:
    """The server as it is meant to run live: one owner, his number from the variable."""
    e = await make_env(tmp_path, owner_phones=((OWNER, OWNER_PHONE),))
    yield e
    await e.http.aclose()
    await e.ctx.engine.dispose()


def shown(number: str) -> str:
    return f"({number[:3]}) {number[3:6]}-{number[6:]}"


def every_way_of_writing(number: str) -> tuple[str, ...]:
    """A whole number, however it is written. None of these may be in a log or an audit entry."""
    return (number, number[3:], f"{number[3:6]}-{number[6:]}", f"{number[:3]}-{number[3:6]}", shown(number))


async def ask(env: Env, typed: str, *, ip: str = "", field: str = "contact") -> httpx.Response:
    """Type something in the one box on the sign-in page and press the button."""
    return await env.http.post("/office/login", data={field: typed}, headers={"x-forwarded-for": ip} if ip else {})


async def enter(env: Env, number: str, code: str, *, ip: str = "") -> httpx.Response:
    """The code step of the text way, as the page posts it."""
    return await env.http.post(
        "/office/verify", data={"phone": number, "code": code}, headers={"x-forwarded-for": ip} if ip else {}
    )


def texted_code(env: Env) -> str:
    return code_from(env.texter.sent[-1][1])


async def csrf_of(env: Env) -> str:
    page = await env.http.get("/office")
    assert page.status_code == 200, page.text
    match = re.search(r'name="csrf" value="([0-9a-f]{64})"', page.text)
    assert match
    return match.group(1)


async def text_login(env: Env, number: str = OWNER_PHONE) -> str:
    """Sign in to the office page by text; returns the CSRF token for forms."""
    r = await ask(env, number)
    assert r.status_code == 200, r.text
    r = await enter(env, number, texted_code(env))
    assert r.status_code == 303, r.text
    return await csrf_of(env)


async def add_user(env: Env, csrf: str, email: str = STAFF, role: str = "staff") -> None:
    r = await env.http.post("/office/users", data={"csrf": csrf, "email": email, "role": role})
    assert r.status_code == 303 and "err=" not in r.headers["location"], r.headers["location"]


async def give(env: Env, csrf: str, email: str, number: str) -> httpx.Response:
    return await env.http.post("/office/users/phone", data={"csrf": csrf, "email": email, "phone": number})


async def take_away(env: Env, csrf: str, email: str) -> httpx.Response:
    return await env.http.post("/office/users/phone", data={"csrf": csrf, "email": email, "phone": "", "remove": "1"})


def row_of(page: str, email: str) -> str:
    """One person's row on the Users page."""
    return next(part.split("</tr>")[0] for part in page.split("<tr>") if f'data-l="Email">{email}' in part)


def said(r: httpx.Response) -> str:
    """What a form answered: the message in the address it sends the browser back to."""
    assert r.status_code == 303, r.text
    return unquote(r.headers["location"])


async def numbers(env: Env) -> dict[str, str]:
    return {row.email: row.phone for row in await env.all(sa.select(db.office_phones))}


async def entries(env: Env, action: str, by: str = "") -> list[Any]:
    """Audit entries of one kind, oldest first. `by`: only those of that kind of actor
    ("office": a person on a page; "system": the server's own start)."""
    query = sa.select(db.events).where(db.events.c.action == action).order_by(db.events.c.id)
    if by:
        query = query.where(db.events.c.actor_type == by)
    return await env.all(query)


def no_texting(env: Env) -> None:
    """The server as it is with no Quo key: texting is not set up."""
    env.reconfigure(quo_api_key="", quo_from="")
    env.ctx.texter = Texter(env.ctx.cfg)
    assert not env.ctx.texter.configured


def no_mail_service(env: Env, backend: str = "log") -> None:
    """The live server as it was when the owner asked for this: no mail service connected.
    "log": an emailed code is only printed in the server's log. "off": no email at all."""
    env.reconfigure(mail_backend=backend)
    env.ctx.mailer = LogMailer() if backend == "log" else Mailer()
    assert not env.ctx.mailer.delivers


def our_lines(err: str) -> list[str]:
    """The lines the office text way itself wrote to the log."""
    return [line for line in err.splitlines() if "office sign-in code text" in line]


async def restart(env: Env, **changes: Any) -> None:
    """Stop the server and start a new one over the same database, as a deploy does. What
    it holds in memory is gone; the office's browser keeps its cookie."""
    cookies = env.http.cookies
    await env.http.aclose()
    await env.ctx.engine.dispose()
    env.ctx = build_ctx(
        replace(env.ctx.cfg, **changes), fr_client=_client_for(env.fake), storage=env.storage, mailer=env.mailer,
        texter=env.texter, pusher=env.pusher,
    )
    env.app = build_app(env.ctx, run_jobs=False)
    env.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url=BASE, cookies=cookies)
    await video_app.start_up(env.ctx)


# == one text, and the code signs him in =================================================


async def test_the_owner_types_his_number_gets_one_text_and_the_code_signs_him_in(env: Env) -> None:
    assert (await env.http.get("/office")).status_code == 303, "not signed in yet"
    r = await ask(env, "(916) 555-0142")
    page = html.unescape(r.text)
    assert r.status_code == 200
    assert f"If <b>{shown(OWNER_PHONE)}</b> is on the office list, we just texted it a 6-digit code." in page
    assert 'autocomplete="one-time-code"' in page, "the phone offers the code from the text above the keyboard"

    ((to, text),) = env.texter.sent
    assert to == OWNER_PHONE, "one text, to the number set for him"
    assert env.mailer.sent == [], "and no email: he asked for a text"
    code = code_from(text)
    assert text == f"{code} is your Zest Lawn & Pest office code. It works once, for 15 minutes. Do not share it."
    assert len(text) <= 160, "one text message, not two"
    assert "Zest Video code" not in text, "he can tell it from a technician's code"

    r = await enter(env, OWNER_PHONE, code)
    assert r.status_code == 303 and r.headers["location"] == "/office"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie and "path=/office" in cookie
    session = await env.one(sa.select(db.office_sessions))
    assert session.email == OWNER and len(session.token_hash) == 64, "the same office session as the email way"

    page = await env.http.get("/office")
    assert page.status_code == 200 and "Sign out" in page.text and OWNER in page.text
    assert (await env.http.get("/office/settings")).status_code == 200, "and he is the owner in it"

    (asked,) = await entries(env, "code_requested")
    assert (asked.actor, asked.detail) == (OWNER, {"by": "text", "ends": "0142", "delivered": ["text"]})
    (signed_in,) = await entries(env, "login")
    assert (signed_in.actor, signed_in.detail) == (OWNER, {"by": "text", "ends": "0142"})


async def test_a_code_works_once(env: Env) -> None:
    await ask(env, OWNER_PHONE)
    code = texted_code(env)
    assert (await enter(env, OWNER_PHONE, code)).status_code == 303
    env.http.cookies.clear()
    again = await enter(env, OWNER_PHONE, code)
    assert again.status_code == 401 and "not right or it ran out" in again.text
    assert len(await env.all(sa.select(db.office_sessions))) == 1


@pytest.mark.parametrize("typed", [
    "9165550142", "916-555-0142", "916.555.0142", " 916 555 0142 ", "+1 (916) 555-0142", "1 916 555 0142",
])
async def test_the_number_may_be_typed_every_way_a_tech_may_type_his(env: Env, typed: str) -> None:
    r = await ask(env, typed)
    assert r.status_code == 200 and [to for to, _ in env.texter.sent] == [OWNER_PHONE]
    assert (await enter(env, OWNER_PHONE, texted_code(env))).status_code == 303


async def test_the_sign_in_page_asks_for_one_thing(env: Env) -> None:
    page = html.unescape((await env.http.get("/office/login")).text)
    boxes = re.findall(r"<input[^>]*>", page)
    assert len(boxes) == 1, "one box"
    (box,) = boxes
    assert 'name="contact"' in box and 'type="text"' in box, "a number or an email: so not an email-only box"
    assert "required" in box and "autofocus" in box
    assert '<label for="contact">Mobile number or email</label>' in page
    assert "Send me a code" in page
    # A phone and a desktop alike: the page sets its own width, and a box of 16px or more
    # does not make an iPhone zoom in when it is tapped.
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in page
    assert ".login{max-width:420px" in page and ".login input{width:100%;font-size:18px" in page


async def test_the_boxes_old_name_still_works_for_a_number_and_for_an_email(env: Env) -> None:
    """A page left open from before this change posts the box as `email`."""
    r = await ask(env, OWNER_PHONE, field="email")
    assert r.status_code == 200 and "we just texted it" in r.text and len(env.texter.sent) == 1
    r = await ask(env, OWNER, field="email")
    assert r.status_code == 200 and "we just emailed it a 6-digit code" in r.text and len(env.mailer.sent) == 1


@pytest.mark.parametrize("typed", ["", "   ", "the owner", "x" * 300])
async def test_something_that_is_neither_a_number_nor_an_email_is_asked_for_again(env: Env, typed: str) -> None:
    r = await ask(env, typed)
    assert r.status_code == 400 and "Type your mobile number or your email." in r.text
    assert "we just" not in r.text and env.texter.sent == [] and env.mailer.sent == []
    assert await env.all(sa.select(db.login_codes)) == []


@pytest.mark.parametrize("typed", [
    "916-555-014",  # one digit short
    "916-555-01422",  # one too many
    "555-0142",  # no area code
    "91655501420000",
    "916 555 0142 ext 5",
])
async def test_a_slip_in_the_number_is_named_and_stays_in_the_box_to_be_put_right(env: Env, typed: str) -> None:
    """The first wrong turn a person takes. The page used to answer with the label he had
    just followed, and an empty box, so the slip could not be seen."""
    r = await ask(env, typed)
    page = html.unescape(r.text)
    assert r.status_code == 400
    assert "That is not a mobile number. Type all 10 digits and nothing else, like 916-555-0142." in page
    (box,) = re.findall(r"<input[^>]*>", page)
    assert f'value="{typed}"' in box, "what he typed is still there"
    assert "we just" not in page and env.texter.sent == [] and env.mailer.sent == []
    assert await env.all(sa.select(db.login_codes)) == [], "and it says nothing about anybody: no one was looked up"


async def test_a_good_number_is_not_put_back_in_the_box_with_a_message_about_the_server(env: Env) -> None:
    no_texting(env)
    r = await ask(env, OWNER_PHONE)
    (box,) = re.findall(r"<input[^>]*>", r.text)
    assert r.status_code == 503 and 'value=""' in box, "the box is empty, for the email the message asks for"


@pytest.mark.parametrize("pasted", [
    "\u202a+1\u00a0(916)\u00a0555-0142\u202c",  # copied from the iPhone's Contacts: marks nobody can see
    "916\u00a0555\u00a00142",  # spaces that are not the space bar's
    "916\u2011555\u20110142",  # dashes that are not the minus key's
    "\ufeff9165550142\u200b",
])
async def test_a_number_pasted_from_a_phones_contacts_works_though_it_carries_marks_nobody_sees(
    env: Env, pasted: str
) -> None:
    r = await ask(env, pasted)
    assert r.status_code == 200 and [to for to, _ in env.texter.sent] == [OWNER_PHONE]
    # And on the Office users page, where an owner pastes a staff user's number the same way.
    csrf = await text_login(env)
    await add_user(env, csrf)
    assert "Saved" in said(await give(env, csrf, STAFF, pasted.replace("0142", "0143")))
    assert (await numbers(env))[STAFF] == STAFF_PHONE


# == nothing is given away ===============================================================


async def a_listed_a_removed_and_a_strangers_number(env: Env) -> tuple[str, str, str]:
    """The owner's own number, the number of a staff user who has since been removed, and
    a number nobody has. Everyone is signed out afterwards."""
    csrf = await text_login(env)
    await add_user(env, csrf)
    assert "Saved" in said(await give(env, csrf, STAFF, STAFF_PHONE))
    r = await env.http.post("/office/users", data={"csrf": csrf, "email": STAFF, "role": "staff", "remove": "1"})
    assert r.status_code == 303
    assert (await numbers(env))[STAFF] == STAFF_PHONE, "a removed user's number stays on file, and does nothing"
    env.http.cookies.clear()
    env.texter.sent.clear()
    env.ctx.limiter.reset()
    return OWNER_PHONE, STAFF_PHONE, STRANGER


def without_the_number(r: httpx.Response, number: str) -> tuple[int, str]:
    """An answer with the number that was typed taken out, to lay beside another."""
    return r.status_code, r.text.replace(html.escape(shown(number)), "NUMBER").replace(number, "NUMBER")


async def test_a_listed_number_a_removed_users_and_a_strangers_get_the_same_page_and_one_text(
    env: Env, monkeypatch
) -> None:
    listed, removed, stranger = await a_listed_a_removed_and_a_strangers_number(env)
    answers = {number: await ask(env, number) for number in (listed, removed, stranger)}

    assert [to for to, _ in env.texter.sent] == [listed], "only the listed one gets a text"
    assert env.mailer.sent == []
    pages = {number: without_the_number(r, number) for number, r in answers.items()}
    assert pages[listed] == pages[removed] == pages[stranger], "and nothing on the page tells them apart"
    assert pages[listed][0] == 200 and "we just texted it a 6-digit code" in pages[listed][1]
    assert dict(answers[listed].headers) == dict(answers[stranger].headers) == dict(answers[removed].headers)


async def test_at_the_code_step_a_number_that_signs_nobody_in_fails_exactly_like_a_wrong_code(
    env: Env, monkeypatch
) -> None:
    listed, removed, stranger = await a_listed_a_removed_and_a_strangers_number(env)
    monkeypatch.setattr(security, "new_code", lambda: "123456")  # so that 000000 is surely wrong
    sessions = await env.all(sa.select(db.office_sessions))
    for number in (listed, removed, stranger):
        assert (await ask(env, number)).status_code == 200

    # Six wrong tries each: four "not right", then the lock, and the lock again.
    tries = {
        number: [without_the_number(await enter(env, number, "000000"), number) for _ in range(6)]
        for number in (listed, removed, stranger)
    }
    assert tries[listed] == tries[removed] == tries[stranger]
    assert [status for status, _ in tries[listed]] == [401, 401, 401, 401, 423, 423]
    assert "That code is not right. Try again." in html.unescape(tries[listed][0][1])
    assert "Too many wrong tries. Ask for a new code." in html.unescape(tries[listed][4][1])
    assert await env.all(sa.select(db.office_sessions)) == sessions, "and nobody got in"


async def test_with_no_code_asked_for_every_number_is_answered_alike_too(env: Env) -> None:
    listed, removed, stranger = await a_listed_a_removed_and_a_strangers_number(env)
    answers = [without_the_number(await enter(env, number, "123456"), number) for number in (listed, removed, stranger)]
    assert answers[0] == answers[1] == answers[2] and answers[0][0] == 401
    assert "That code is not right or it ran out. Ask for a new code." in html.unescape(answers[0][1])


async def test_the_code_nobody_is_sent_opens_nothing_even_when_it_is_guessed(env: Env, monkeypatch) -> None:
    """For a number nobody has, a code is saved and sent to no one, so that guesses at it are
    counted like guesses at a real one. One guess in a million will match it."""
    monkeypatch.setattr(security, "new_code", lambda: "123456")
    assert (await ask(env, STRANGER)).status_code == 200
    r = await enter(env, STRANGER, "123456")
    assert r.status_code == 401 and "not right or it ran out" in r.text
    assert await env.all(sa.select(db.office_sessions)) == [] and env.texter.sent == []


async def test_a_number_a_stranger_typed_is_not_kept(env: Env) -> None:
    assert (await ask(env, STRANGER)).status_code == 200
    (row,) = await env.all(sa.select(db.login_codes))
    assert row.purpose == "decoy" and re.fullmatch(r"[0-9a-f]{64}", row.subject), "filed under a hash of it"
    # A bare hash of ten digits is no hiding place: all of them can be tried in minutes. So
    # it is keyed with the server's secret, and nobody holding the table alone can do that.
    bare = {hashlib.sha256(text.encode()).hexdigest() for text in (STRANGER, f"decoy:{STRANGER}", f"+1{STRANGER}")}
    assert row.subject not in bare
    another = SimpleNamespace(cfg=replace(env.ctx.cfg, secret="another-secret"))
    assert row.subject == auth._decoy(env.ctx, STRANGER) != auth._decoy(another, STRANGER)
    (entry,) = await entries(env, "code_request_unknown")
    assert entry.detail == {"kind": "office_phone"}, "and the audit entry has no number at all"
    assert await numbers(env) == {OWNER: OWNER_PHONE}


async def test_the_time_an_answer_takes_does_not_tell_a_listed_number_from_any_other(env: Env, monkeypatch) -> None:
    """Only a listed number's answer waits for Quo. Without a floor under every answer, one
    ask for each number would tell them apart by the clock, whatever the page says."""

    class SlowTexter(MemoryTexter):
        fail = False

        async def send(self, to_digits: str, content: str) -> None:
            await asyncio.sleep(0.2)  # the round trip to Quo
            if self.fail:
                raise DeliveryError("text provider said 500: no", status=500)
            await super().send(to_digits, content)

    env.ctx.texter = slow = SlowTexter()

    async def seconds(number: str) -> float:
        started = time.monotonic()
        await ask(env, number)
        return time.monotonic() - started

    # (A hair under each number below: a sleep may come back a few thousandths early.)
    monkeypatch.setattr(auth, "TEXT_ANSWER_SECONDS", 0.0)
    assert await seconds(OWNER_PHONE) >= 0.19, "with no floor, the listed number is the slow one"

    monkeypatch.setattr(auth, "TEXT_ANSWER_SECONDS", 0.7)
    listed, stranger = await seconds(OWNER_PHONE), await seconds(STRANGER)
    assert listed >= 0.69 and stranger >= 0.69, "every answer takes at least the floor"
    assert abs(listed - stranger) < 0.3, f"and so they take the same time ({listed:.2f}s, {stranger:.2f}s)"
    assert len(slow.sent) == 2, "the wait is not a second text"
    slow.fail = True
    assert await seconds(OWNER_PHONE) >= 0.69, "an answer that says the text failed is not let out early either"


def test_the_floor_under_every_answer_is_two_seconds_on_a_real_server() -> None:
    """`make_env` takes the floor away so that the tests do not wait for it. This reads the
    value as it is shipped."""
    assert re.search(r"^TEXT_ANSWER_SECONDS = 2\.0$", Path(auth.__file__).read_text(), re.M)


async def test_a_bad_number_at_the_code_step_is_one_more_wrong_code(env: Env) -> None:
    for number in ("", "555", "x" * 5000, "916 555 0142 ext 5"):
        r = await env.http.post("/office/verify", data={"phone": number or " ", "code": "123456"})
        assert r.status_code == 401 and "not right or it ran out" in r.text
    assert await env.all(sa.select(db.office_sessions)) == []


# == it is the same code as the emailed one ==============================================


async def test_five_wrong_tries_lock_the_code_and_the_right_one_no_longer_works(env: Env) -> None:
    await ask(env, OWNER_PHONE)
    code = texted_code(env)
    wrong = "000000" if code != "000000" else "111111"
    statuses = [(await enter(env, OWNER_PHONE, wrong)).status_code for _ in range(5)]
    assert statuses == [401, 401, 401, 401, 423]
    locked = await enter(env, OWNER_PHONE, code)
    assert locked.status_code == 423 and "Too many wrong tries" in locked.text, "the right code is too late"
    assert await env.all(sa.select(db.office_sessions)) == []
    # The lock is in the database, so a restart does not lift it. (The email way's code step
    # never takes a texted code at all: see the tests further down.)
    await restart(env)
    assert (await enter(env, OWNER_PHONE, code)).status_code == 423

    await ask(env, OWNER_PHONE)
    assert (await enter(env, OWNER_PHONE, texted_code(env))).status_code == 303, "a new code is a new start"


async def test_a_code_dies_after_15_minutes(env: Env, monkeypatch) -> None:
    await ask(env, OWNER_PHONE)
    code = texted_code(env)
    real = db.now
    monkeypatch.setattr(db, "now", lambda: real() + timedelta(minutes=security.CODE_MINUTES, seconds=1))
    r = await enter(env, OWNER_PHONE, code)
    assert r.status_code == 401 and "not right or it ran out" in r.text


async def test_a_new_code_retires_the_old_one_whichever_way_each_was_asked_for(env: Env) -> None:
    await ask(env, OWNER_PHONE)
    first = texted_code(env)
    await ask(env, OWNER)  # the same person now asks by email
    second = code_from(env.mailer.sent[-1].text)
    if first == second:
        pytest.skip("one time in a million the two codes are the same")
    assert (await enter(env, OWNER_PHONE, first)).status_code == 401, "the texted one is retired"
    assert (await env.http.post("/office/verify", data={"email": OWNER, "code": first})).status_code == 401
    live = await env.all(sa.select(db.login_codes).where(db.login_codes.c.used_at.is_(None)))
    assert [(row.purpose, row.subject) for row in live] == [("office", OWNER)], "one live code, filed under his email"

    await ask(env, OWNER_PHONE)  # and by text again
    third = texted_code(env)
    if third == second:
        pytest.skip("one time in a million the two codes are the same")
    assert (await env.http.post("/office/verify", data={"email": OWNER, "code": second})).status_code == 401
    assert (await enter(env, OWNER_PHONE, third)).status_code == 303


RAN_OUT = "That code is not right or it ran out. Ask for a new code."


async def by_email(env: Env, email: str, code: str) -> httpx.Response:
    """The code step of the email way."""
    return await env.http.post("/office/verify", data={"email": email, "code": code})


async def test_a_code_is_taken_only_at_the_code_step_of_the_way_it_was_asked_for(env: Env) -> None:
    await ask(env, OWNER_PHONE)
    texted = texted_code(env)
    r = await by_email(env, OWNER, texted)
    assert r.status_code == 401 and RAN_OUT in r.text, "the email way's code step does not know a texted code"
    assert (await enter(env, OWNER_PHONE, texted)).status_code == 303, "and trying it there did not use it up"

    env.http.cookies.clear()
    await ask(env, OWNER)
    emailed = code_from(env.mailer.sent[-1].text)
    r = await enter(env, OWNER_PHONE, emailed)
    assert r.status_code == 401 and RAN_OUT in r.text, "nor the text way's an emailed one"
    assert (await by_email(env, OWNER, emailed)).status_code == 303

    marks = {row.issued_by for row in await env.all(sa.select(db.login_codes).where(db.login_codes.c.purpose == "office"))}
    assert marks == {"text", None}, "told apart by a column that was there already, not by a new one"


async def test_wrong_tries_at_the_other_ways_code_step_do_not_count_against_a_code(env: Env) -> None:
    """So someone who knows only a number cannot lock a code that was emailed, and someone
    who knows only an email cannot lock one that was texted."""
    await ask(env, OWNER)
    emailed = code_from(env.mailer.sent[-1].text)
    wrong = "000000" if emailed != "000000" else "111111"
    for _ in range(6):
        assert (await enter(env, OWNER_PHONE, wrong)).status_code == 401, "never the lock: there is no code here"
    assert (await by_email(env, OWNER, emailed)).status_code == 303

    env.http.cookies.clear()
    await ask(env, OWNER_PHONE)
    texted = texted_code(env)
    wrong = "000000" if texted != "000000" else "111111"
    for _ in range(6):
        assert (await by_email(env, OWNER, wrong)).status_code == 401
    assert (await enter(env, OWNER_PHONE, texted)).status_code == 303


async def test_the_email_ways_code_step_does_not_tell_whether_a_text_just_went_to_a_number(env: Env) -> None:
    """Someone who knows an owner's email (it is easy to guess) asks for a text to a number
    he is curious about, then types a wrong code at the email way's code step. "Try again"
    in place of "it ran out" would mean that user now has a live code: the number is his."""
    answers = {}
    for number in (STRANGER, OWNER_PHONE):
        assert (await ask(env, number)).status_code == 200
        r = await by_email(env, OWNER, "000000")
        answers[number] = (r.status_code, r.text)
    assert answers[STRANGER] == answers[OWNER_PHONE]
    assert answers[OWNER_PHONE][0] == 401 and RAN_OUT in answers[OWNER_PHONE][1]


async def test_the_text_ways_code_step_does_not_tell_whether_a_user_has_an_emailed_code(env: Env) -> None:
    """The other way round, with no text asked for at all: the owner has just asked by email."""
    await ask(env, OWNER)
    answers = [without_the_number(await enter(env, number, "000000"), number) for number in (OWNER_PHONE, STRANGER)]
    assert answers[0] == answers[1] and answers[0][0] == 401 and RAN_OUT in html.unescape(answers[0][1])


async def test_a_code_texted_in_the_very_moment_its_number_is_taken_away_opens_nothing(env: Env, monkeypatch) -> None:
    """The number is looked up, and before the code is made an owner takes the number away.
    The text still goes: it was on its way. It must not sign anybody in, at either step,
    and not later either, when that user is given a number again."""
    csrf = await text_login(env)
    await add_user(env, csrf)
    await give(env, csrf, STAFF, STAFF_PHONE)
    env.http.cookies.clear()
    looked_up = auth._office_user_by_phone

    async def then_it_is_taken_away(ctx: Any, phone: str) -> Any:
        user = await looked_up(ctx, phone)
        if phone == STAFF_PHONE and user is not None:
            assert await auth.remove_office_phone(ctx, STAFF, OWNER)
        return user

    monkeypatch.setattr(auth, "_office_user_by_phone", then_it_is_taken_away)
    await ask(env, STAFF_PHONE)
    monkeypatch.setattr(auth, "_office_user_by_phone", looked_up)
    assert env.texter.sent[-1][0] == STAFF_PHONE and await numbers(env) == {OWNER: OWNER_PHONE}
    code = texted_code(env)

    assert (await enter(env, STAFF_PHONE, code)).status_code == 401
    assert (await by_email(env, STAFF, code)).status_code == 401, "the email way's code step does not take it either"
    csrf = await text_login(env)
    await give(env, csrf, STAFF, OTHER_PHONE)  # she gets a new number a minute later
    env.http.cookies.clear()
    assert (await enter(env, OTHER_PHONE, code)).status_code == 401, "the code from the old phone is dead"
    assert (await enter(env, STAFF_PHONE, code)).status_code == 401
    assert await env.all(sa.select(db.office_sessions).where(db.office_sessions.c.email == STAFF)) == []


async def test_one_users_code_does_not_sign_in_another(env: Env) -> None:
    csrf = await text_login(env)
    await add_user(env, csrf)
    await give(env, csrf, STAFF, STAFF_PHONE)
    env.http.cookies.clear()
    await ask(env, STAFF_PHONE)
    staff_code = texted_code(env)
    await ask(env, OWNER_PHONE)
    if texted_code(env) == staff_code:
        pytest.skip("one time in a million the two codes are the same")
    assert (await enter(env, OWNER_PHONE, staff_code)).status_code == 401
    assert (await enter(env, STAFF_PHONE, staff_code)).status_code == 303
    assert (await env.one(sa.select(db.office_sessions).order_by(db.office_sessions.c.created_at.desc()))).email == STAFF
    assert (await env.http.get("/office/settings")).status_code == 403, "staff by text is still staff"


# == limits ==============================================================================


async def test_a_sixth_text_to_one_number_in_15_minutes_is_refused_wherever_it_is_asked_from(env: Env) -> None:
    pages = {}
    for number in (OWNER_PHONE, STRANGER):  # listed or not: the same limit, the same answer
        for n in range(5):
            assert (await ask(env, number, ip=f"203.0.113.{n}")).status_code == 200
        r = await ask(env, number, ip="203.0.113.99")
        assert r.status_code == 429 and "we just" not in r.text, "it does not say a text went"
        assert ("No new code was sent: this number has been asked for too often. Type the newest code it got, or "
                "wait 15 minutes.") in r.text, "how long, in minutes"
        pages[number] = without_the_number(r, number)
    assert pages[OWNER_PHONE] == pages[STRANGER]
    assert len(env.texter.sent) == 5


async def test_a_stranger_who_uses_up_a_numbers_asks_does_not_keep_its_owner_out(env: Env) -> None:
    """Anybody who knows the number can ask for it five times. The owner's own ask is then
    refused, but the newest of the five codes on his phone is good, and the page that
    refuses him has the box to type it in. It used to send him back to the first page."""
    for n in range(5):
        assert (await ask(env, OWNER_PHONE, ip=f"203.0.113.{n}")).status_code == 200  # the stranger
    newest = texted_code(env)

    r = await ask(env, "(916) 555-0142", ip="198.51.100.7")  # the owner, from his own address
    page = html.unescape(r.text)
    assert r.status_code == 429 and len(env.texter.sent) == 5
    assert 'name="code"' in page and f'<input type="hidden" name="phone" value="{OWNER_PHONE}">' in page
    assert f"Code for {shown(OWNER_PHONE)}" in page
    assert (await enter(env, OWNER_PHONE, newest, ip="198.51.100.7")).status_code == 303
    assert (await env.http.get("/office")).status_code == 200


async def test_too_many_asks_from_one_address_says_how_long_and_that_the_email_way_is_open(env: Env) -> None:
    for n in range(10):
        await ask(env, f"91655501{50 + n}", ip="203.0.113.7")
    r = await ask(env, OWNER_PHONE, ip="203.0.113.7")
    assert r.status_code == 429
    assert "Too many tries. Wait 15 minutes, or type your email and the code comes by email." in r.text
    assert 'name="contact"' in r.text, "on the first page, where the email is typed"
    r = await ask(env, OWNER, ip="203.0.113.7")
    assert r.status_code == 200 and "we just emailed it" in r.text, "and it is: the email way has its own count"

    no_mail_service(env)
    r = await ask(env, OWNER_PHONE, ip="203.0.113.7")
    assert r.status_code == 429 and '<div class="banner err">Too many tries. Wait 15 minutes.</div>' in r.text


@pytest.mark.parametrize("seconds, words", [(1, "1 minute"), (60, "1 minute"), (61, "2 minutes"), (899, "15 minutes"), (900, "15 minutes")])
def test_a_wait_is_said_in_whole_minutes_rounded_up(seconds: int, words: str) -> None:
    assert auth._wait(seconds) == words


async def test_an_eleventh_ask_from_one_address_in_15_minutes_is_refused(env: Env) -> None:
    for n in range(10):
        assert (await ask(env, f"91655501{50 + n}", ip="203.0.113.7")).status_code == 200
    r = await ask(env, OWNER_PHONE, ip="203.0.113.7")
    assert r.status_code == 429 and env.texter.sent == []
    assert (await ask(env, OWNER_PHONE, ip="203.0.113.8")).status_code == 200, "another address is not held up"


async def test_guessing_codes_from_one_address_is_cut_off(env: Env) -> None:
    for _ in range(30):
        assert (await enter(env, STRANGER, "000000", ip="203.0.113.7")).status_code in (401, 423)
    r = await enter(env, OWNER_PHONE, "000000", ip="203.0.113.7")
    assert r.status_code == 429 and "Too many tries. Wait 15 minutes." in r.text


async def test_both_ways_together_give_twice_the_codes_and_twice_the_guesses(env: Env) -> None:
    """Known, and written down in docs/video/README.md: the two ways have limits of their
    own, so someone who knows a user's number and his email can have ten codes made in 15
    minutes where the email way alone gave five, and has five guesses at each. Every code
    is new, so a guess never carries over, and each guess goes to its own code step."""
    made = 0
    for n in range(6):
        made += (await ask(env, OWNER_PHONE, ip=f"203.0.113.{n}")).status_code == 200
        made += (await ask(env, OWNER, ip=f"203.0.113.{n}")).status_code == 200
    assert made == 10 and len(env.texter.sent) == 5 and len(env.mailer.sent) == 5
    codes = await env.all(sa.select(db.login_codes).where(db.login_codes.c.purpose == "office"))
    assert len(codes) == 10 and sum(row.used_at is None for row in codes) == 1, "and only the newest is live"


async def test_nothing_done_on_the_text_way_uses_up_the_email_ways_tries(env: Env) -> None:
    """The email way is the way back in. Its limits are its own."""
    for n in range(11):
        last = await ask(env, f"91655501{50 + n}")
    assert last.status_code == 429
    for _ in range(31):
        last = await enter(env, STRANGER, "000000")
    assert last.status_code == 429

    r = await ask(env, OWNER)
    assert r.status_code == 200 and "we just emailed it a 6-digit code" in r.text
    r = await env.http.post("/office/verify", data={"email": OWNER, "code": code_from(env.mailer.sent[-1].text)})
    assert r.status_code == 303


# == only an owner gives out a number ====================================================


async def test_the_owner_gives_a_staff_user_a_number_and_she_signs_in_by_text(env: Env) -> None:
    csrf = await text_login(env)
    await add_user(env, csrf)
    r = await ask(env, STAFF_PHONE)
    assert r.status_code == 200 and len(env.texter.sent) == 1, "on the list, but no number yet: no text"

    assert "Saved. The number ending 0143 now gets that person's sign-in code by text." in said(
        await give(env, csrf, STAFF, "(916) 555-0143"))
    row = await env.one(sa.select(db.office_phones).where(db.office_phones.c.email == STAFF))
    assert (row.phone, row.added_by) == (STAFF_PHONE, OWNER) and row.added_at is not None
    (entry,) = await entries(env, "office_phone_set", by="office")
    assert (entry.actor, entry.detail) == (OWNER, {"email": STAFF, "ends": "0143"})

    env.http.cookies.clear()
    env.ctx.limiter.reset()
    await text_login(env, STAFF_PHONE)
    assert env.texter.sent[-1][0] == STAFF_PHONE
    assert (await env.http.get("/office")).status_code == 200
    assert (await env.http.get("/office/users")).status_code == 403


async def test_staff_cannot_give_change_or_take_away_a_number(env: Env) -> None:
    csrf = await text_login(env)
    await add_user(env, csrf)
    await give(env, csrf, STAFF, STAFF_PHONE)
    env.http.cookies.clear()
    staff_csrf = await text_login(env, STAFF_PHONE)
    before = await numbers(env)

    for data in (
        {"email": STAFF, "phone": OTHER_PHONE},  # her own
        {"email": OWNER, "phone": OTHER_PHONE},  # the owner's
        {"email": OWNER, "phone": "", "remove": "1"},
        {"email": STAFF, "phone": "", "remove": "1"},
    ):
        r = await env.http.post("/office/users/phone", data={"csrf": staff_csrf, **data})
        assert r.status_code == 403 and "Owners only" in r.text
    assert await numbers(env) == before
    assert len(await entries(env, "office_phone_set", by="office")) == 1, "the owner's one, and none of hers"
    assert await entries(env, "office_phone_removed") == []


async def test_the_number_form_needs_a_session_and_its_own_form_token(env: Env) -> None:
    r = await env.http.post("/office/users/phone", data={"csrf": "x", "email": OWNER, "phone": OTHER_PHONE})
    assert r.status_code == 303 and r.headers["location"] == "/office/login", "nobody is signed in"

    csrf = await text_login(env)
    await add_user(env, csrf)
    for data in ({}, {"csrf": "0" * 64}):
        r = await env.http.post("/office/users/phone", data={**data, "email": STAFF, "phone": STAFF_PHONE})
        assert r.status_code == 400 and "stale" in r.text
    assert await numbers(env) == {OWNER: OWNER_PHONE}
    assert (await env.http.get("/office/users/phone")).status_code == 405, "a form only"


async def test_a_number_belongs_to_one_user_and_a_user_has_one_number(env: Env) -> None:
    csrf = await text_login(env)
    await add_user(env, csrf)
    await add_user(env, csrf, SECOND)
    await give(env, csrf, STAFF, STAFF_PHONE)

    r = await give(env, csrf, SECOND, STAFF_PHONE)
    assert "err=That number is already on another office user. Take it away there first." in said(r)
    # The owner's own number sits on him by the variable. "Take it away there first" would
    # be no help: his row has nothing to take it away with.
    assert "err=That number is an owner's, set in the server's settings. Use another number." in said(
        await give(env, csrf, SECOND, OWNER_PHONE))
    assert await numbers(env) == {OWNER: OWNER_PHONE, STAFF: STAFF_PHONE}

    # A second number for the same user takes the place of the first.
    assert "Saved" in said(await give(env, csrf, STAFF, OTHER_PHONE))
    assert await numbers(env) == {OWNER: OWNER_PHONE, STAFF: OTHER_PHONE}
    # Giving her the number she has changes nothing and writes nothing.
    assert "Saved" in said(await give(env, csrf, STAFF, "916-555-0144"))
    assert [e.detail for e in await entries(env, "office_phone_set", by="office")] == [
        {"email": STAFF, "ends": "0143"}, {"email": STAFF, "ends": "0144", "was": "0143"}]

    # And the table itself will not hold two users on one number, or two numbers for one user.
    for row in (dict(phone=OTHER_PHONE, email=SECOND), dict(phone="9165550145", email=STAFF)):
        with pytest.raises(sa.exc.IntegrityError):
            await env.run(sa.insert(db.office_phones).values(added_at=db.now(), added_by="test", **row))


async def test_two_owners_giving_out_one_number_in_the_same_second(env: Env) -> None:
    csrf = await text_login(env)
    await add_user(env, csrf)
    await add_user(env, csrf, SECOND)
    await give(env, csrf, SECOND, OTHER_PHONE)
    results = await asyncio.gather(
        auth.set_office_phone(env.ctx, STAFF, STAFF_PHONE, OWNER), auth.set_office_phone(env.ctx, SECOND, STAFF_PHONE, OWNER),
        return_exceptions=True,
    )
    held = await numbers(env)
    assert sorted(held.values()) in ([OWNER_PHONE, STAFF_PHONE, OTHER_PHONE], [OWNER_PHONE, STAFF_PHONE]), held
    assert list(held.values()).count(STAFF_PHONE) == 1, "one of them has it"
    refused = [r for r in results if isinstance(r, BaseException)]
    assert len(refused) == 1 and "already on another office user" in str(refused[0])
    if held.get(STAFF) == STAFF_PHONE:
        assert held[SECOND] == OTHER_PHONE, "the one who was refused keeps the number she had"


async def test_the_same_form_sent_twice_at_once_sets_the_number_once_and_refuses_neither(env: Env) -> None:
    """A double click on Save, or on Change."""
    csrf = await text_login(env)
    await add_user(env, csrf)
    for number, was in ((STAFF_PHONE, {}), (OTHER_PHONE, {"was": "0143"})):
        first, second = await asyncio.gather(give(env, csrf, STAFF, number), give(env, csrf, STAFF, number))
        assert "Saved" in said(first) and "Saved" in said(second), "the second click is not told off"
        assert await numbers(env) == {OWNER: OWNER_PHONE, STAFF: number}
        assert (await entries(env, "office_phone_set", by="office"))[-1].detail == {"email": STAFF, "ends": number[-4:], **was}
    assert len(await entries(env, "office_phone_set", by="office")) == 2, "one audit entry for each change"


@pytest.mark.parametrize("typed", ["", "555-0143", "not a number", "91655501430000", "916 555 0143 x2"])
async def test_only_a_real_mobile_number_can_be_set(env: Env, typed: str) -> None:
    csrf = await text_login(env)
    await add_user(env, csrf)
    assert "err=That is not a mobile number. Type all 10 digits." in said(await give(env, csrf, STAFF, typed))
    assert await numbers(env) == {OWNER: OWNER_PHONE}


async def test_a_number_can_only_be_set_for_someone_on_the_office_list(env: Env) -> None:
    csrf = await text_login(env)
    assert "err=That person is not on the office list." in said(await give(env, csrf, "nobody@example.com", STAFF_PHONE))
    await add_user(env, csrf)
    await env.http.post("/office/users", data={"csrf": csrf, "email": STAFF, "role": "staff", "remove": "1"})
    assert "err=That person is not on the office list." in said(await give(env, csrf, STAFF, STAFF_PHONE))
    assert await numbers(env) == {OWNER: OWNER_PHONE}


async def test_taking_a_number_away_stops_it_at_once(env: Env) -> None:
    csrf = await text_login(env)
    await add_user(env, csrf)
    await give(env, csrf, STAFF, STAFF_PHONE)
    await ask(env, STAFF_PHONE)  # she has asked for a code, and it is on her phone
    code = texted_code(env)

    assert ("Taken away. That number gets no more codes. Someone who is signed in already stays signed in: "
            "remove the person to end that.") in said(await take_away(env, csrf, STAFF))
    assert await numbers(env) == {OWNER: OWNER_PHONE}
    (entry,) = await entries(env, "office_phone_removed")
    assert (entry.actor, entry.detail) == (OWNER, {"email": STAFF, "ends": "0143"})

    env.http.cookies.clear()
    r = await enter(env, STAFF_PHONE, code)
    assert r.status_code == 401, "the code that was texted to it a minute ago is dead"
    assert (await env.http.post("/office/verify", data={"email": STAFF, "code": code})).status_code == 401, (
        "on the email way's code step too")
    texts = len(env.texter.sent)
    r = await ask(env, STAFF_PHONE)
    assert r.status_code == 200 and len(env.texter.sent) == texts, "and it gets no more texts"
    assert await env.all(sa.select(db.office_sessions).where(db.office_sessions.c.email == STAFF)) == []

    # She is still on the list, and the email way still lets her in.
    await ask(env, STAFF)
    r = await env.http.post("/office/verify", data={"email": STAFF, "code": code_from(env.mailer.sent[-1].text)})
    assert r.status_code == 303

    env.http.cookies.clear()
    csrf = await text_login(env)
    assert "There was no number to take away." in said(await take_away(env, csrf, STAFF))
    assert len(await entries(env, "office_phone_removed")) == 1


async def test_taking_a_number_away_does_not_sign_out_someone_who_is_signed_in_and_the_page_says_so(env: Env) -> None:
    """It ends codes, not sessions. An owner who takes the number of a lost phone away must
    not be left thinking that person is out: the message says what does that."""
    csrf = await text_login(env)
    await add_user(env, csrf)
    await give(env, csrf, STAFF, STAFF_PHONE)
    owners = dict(env.http.cookies)
    env.http.cookies.clear()
    await text_login(env, STAFF_PHONE)
    hers = dict(env.http.cookies)

    env.http.cookies.clear()
    env.http.cookies.update(owners)
    assert "stays signed in: remove the person to end that." in said(await take_away(env, csrf, STAFF))
    env.http.cookies.clear()
    env.http.cookies.update(hers)
    assert (await env.http.get("/office")).status_code == 200, "her open page still works"

    env.http.cookies.clear()
    env.http.cookies.update(owners)
    await env.http.post("/office/users", data={"csrf": csrf, "email": STAFF, "role": "staff", "remove": "1"})
    env.http.cookies.clear()
    env.http.cookies.update(hers)
    assert (await env.http.get("/office")).status_code == 303, "removing the person is what signs her out"


async def test_a_number_change_leaves_an_emailed_code_alone(env: Env) -> None:
    """Only texted codes die with a number. An emailed one went to her inbox."""
    csrf = await text_login(env)
    await add_user(env, csrf)
    await give(env, csrf, STAFF, STAFF_PHONE)
    await ask(env, STAFF)
    emailed = code_from(env.mailer.sent[-1].text)
    await give(env, csrf, STAFF, OTHER_PHONE)
    await take_away(env, csrf, STAFF)
    env.http.cookies.clear()
    assert (await by_email(env, STAFF, emailed)).status_code == 303


async def test_a_changed_number_stops_the_old_one_at_once_and_the_new_one_works(env: Env) -> None:
    csrf = await text_login(env)
    await add_user(env, csrf)
    await give(env, csrf, STAFF, STAFF_PHONE)
    await ask(env, STAFF_PHONE)
    old_code = texted_code(env)
    await give(env, csrf, STAFF, OTHER_PHONE)

    env.http.cookies.clear()
    assert (await enter(env, STAFF_PHONE, old_code)).status_code == 401
    assert (await enter(env, OTHER_PHONE, old_code)).status_code == 401, "a code texted to the old number is dead"
    texts = len(env.texter.sent)
    await ask(env, STAFF_PHONE)
    assert len(env.texter.sent) == texts, "the old number gets nothing"
    await ask(env, OTHER_PHONE)
    assert env.texter.sent[-1][0] == OTHER_PHONE
    assert (await enter(env, OTHER_PHONE, texted_code(env))).status_code == 303


async def test_a_removed_user_gets_no_text_and_one_who_is_added_back_does(env: Env) -> None:
    csrf = await text_login(env)
    await add_user(env, csrf)
    await give(env, csrf, STAFF, STAFF_PHONE)
    await env.http.post("/office/users", data={"csrf": csrf, "email": STAFF, "role": "staff", "remove": "1"})
    texts = len(env.texter.sent)
    assert (await ask(env, STAFF_PHONE)).status_code == 200 and len(env.texter.sent) == texts

    # The owner can still take the number of a removed user away, to give it to someone else.
    page = html.unescape((await env.http.get("/office/users")).text)
    row = row_of(page, STAFF)
    assert "removed" in row and "Number ending <b>0143</b>" in row and "Take number away" in row and 'name="phone"' not in row

    await add_user(env, csrf)  # "Add back"
    await ask(env, STAFF_PHONE)
    assert env.texter.sent[-1][0] == STAFF_PHONE, "her number was kept"


async def test_the_users_page_shows_who_has_a_number_by_its_last_four_digits_only(env: Env) -> None:
    csrf = await text_login(env)
    await add_user(env, csrf)
    await add_user(env, csrf, SECOND)
    await give(env, csrf, STAFF, STAFF_PHONE)
    r = await env.http.get("/office/users")
    page = html.unescape(r.text)
    rows = {email: row_of(page, email) for email in (OWNER, STAFF, SECOND)}

    assert "Number ending <b>0142</b>" in rows[OWNER] and "(set in the server's settings)" in rows[OWNER]
    assert "/office/users/phone" not in rows[OWNER], "the owner's number from the variable has no form here"
    assert "Number ending <b>0143</b>" in rows[STAFF]
    assert "none: signs in by email" in rows[SECOND] and "Take number away" not in rows[SECOND]
    # Every button in a row says what it is for: there were two called Save, and a red
    # "Take away" beside a red "Remove".
    buttons = {email: re.findall(r"<button[^>]*>([^<]+)</button>", row) for email, row in rows.items()}
    assert buttons[STAFF] == ["Change number", "Take number away", "Save role", "Remove person"]
    assert buttons[SECOND] == ["Save number", "Save role", "Remove person"]
    assert buttons[OWNER] == []
    assert rows[STAFF].count('name="csrf"') == 2 and rows[SECOND].count('name="csrf"') == 2, "every form has its token"
    for number in (OWNER_PHONE, STAFF_PHONE):
        for written in every_way_of_writing(number):
            assert written not in page, "a whole number is never put on the page"
    assert "Only an owner can set a number, change it or take it away." in page
    assert "cannot send texts yet" not in page


async def test_the_users_page_says_when_no_text_can_be_sent(env: Env) -> None:
    await text_login(env)
    no_texting(env)
    page = html.unescape((await env.http.get("/office/users")).text)
    assert "This server cannot send texts yet, so for now every code comes by email." in page


# == the owner's own number comes from VIDEO_OWNER_PHONES ================================


async def test_the_owners_number_is_there_at_the_first_start_without_anyone_typing_it(env: Env) -> None:
    row = await env.one(sa.select(db.office_phones))
    assert (row.phone, row.email, row.added_by) == (OWNER_PHONE, OWNER, "env")
    (entry,) = await entries(env, "office_phone_set")
    assert (entry.actor_type, entry.actor) == ("system", "env")
    assert entry.detail == {"email": OWNER, "ends": "0142", "by": "VIDEO_OWNER_PHONES"}


async def test_two_starts_in_a_row_change_nothing(env: Env) -> None:
    before = await env.one(sa.select(db.office_phones))
    await restart(env)
    await restart(env)
    await auth.ensure_owners(env.ctx)
    assert await env.all(sa.select(db.office_phones)) == [before], "the same row, with the day it was first added"
    assert len(await entries(env, "office_phone_set")) == 1, "and one audit entry, not one for each start"
    await text_login(env)


async def test_two_servers_starting_side_by_side_put_the_number_in_once(tmp_path) -> None:
    """Railway starts the new server before it stops the old one."""
    env = await make_env(tmp_path)
    csrf = await office_login(env)
    await add_user(env, csrf)
    await give(env, csrf, STAFF, OWNER_PHONE)  # the number sits on a staff user, to make it harder
    await give(env, csrf, OWNER, OTHER_PHONE)  # and the owner has another one
    cfg = replace(env.ctx.cfg, owner_phones=((OWNER, OWNER_PHONE),))
    servers = [
        build_ctx(replace(cfg), fr_client=_client_for(env.fake), storage=env.storage, mailer=env.mailer,
                  texter=env.texter, pusher=env.pusher)
        for _ in range(2)
    ]
    try:
        results = await asyncio.gather(
            *(video_app.start_up(ctx) for ctx in servers), *(auth.ensure_owners(ctx) for ctx in servers),
            return_exceptions=True,
        )
        assert [r for r in results if isinstance(r, BaseException)] == [], "neither tripped over the other"
        assert await numbers(env) == {OWNER: OWNER_PHONE}
        assert len(await entries(env, "office_phone_removed")) == 1, "taken from the staff user once"
        by_env = [e for e in await entries(env, "office_phone_set") if e.actor == "env"]
        assert [e.detail for e in by_env] == [
            {"email": OWNER, "ends": "0142", "was": "0144", "by": "VIDEO_OWNER_PHONES"}]
    finally:
        for ctx in servers:
            await ctx.engine.dispose()
        await env.http.aclose()
        await env.ctx.engine.dispose()


async def test_servers_starting_at_once_on_a_database_with_no_number_yet_put_it_in_once(tmp_path) -> None:
    """The plain case, and the harder one: nobody has the number, so nothing makes one start
    wait for another (in the test above the first write of each start is on the staff
    user's row, and that lines them up). With an ordinary insert in place of `insert_once`
    all but one of these starts are refused by the database."""
    env = await make_env(tmp_path)
    cfg = replace(env.ctx.cfg, owner_phones=((OWNER, OWNER_PHONE),))
    servers = [
        build_ctx(replace(cfg), fr_client=_client_for(env.fake), storage=env.storage, mailer=env.mailer,
                  texter=env.texter, pusher=env.pusher)
        for _ in range(6)
    ]
    try:
        for _ in range(3):  # and three times over, each time from an empty table
            await env.run(sa.delete(db.office_phones))
            await env.run(sa.delete(db.events).where(db.events.c.action == "office_phone_set"))
            results = await asyncio.gather(*(video_app.start_up(ctx) for ctx in servers), return_exceptions=True)
            assert [r for r in results if isinstance(r, BaseException)] == [], "no start was refused"
            assert await numbers(env) == {OWNER: OWNER_PHONE}
            assert len(await entries(env, "office_phone_set")) == 1, "and one audit entry, not one for each server"
    finally:
        for ctx in servers:
            await ctx.engine.dispose()
        await env.http.aclose()
        await env.ctx.engine.dispose()


async def test_a_number_that_sat_on_a_staff_user_moves_to_the_owner_at_the_next_start(tmp_path) -> None:
    env = await make_env(tmp_path)  # before the variable was set: the owner signs in by email
    try:
        csrf = await office_login(env)
        await add_user(env, csrf)
        await give(env, csrf, STAFF, OWNER_PHONE)
        await ask(env, OWNER_PHONE)
        staff_code = texted_code(env)  # a code for the staff user is on that phone right now
        assert await numbers(env) == {STAFF: OWNER_PHONE}

        await restart(env, owner_phones=((OWNER, OWNER_PHONE),))
        assert await numbers(env) == {OWNER: OWNER_PHONE}, "taken from her, put on him"
        (taken,) = await entries(env, "office_phone_removed")
        assert (taken.actor_type, taken.actor) == ("system", "env")
        assert taken.detail == {"email": STAFF, "ends": "0142", "moved_to": OWNER, "by": "VIDEO_OWNER_PHONES"}

        env.http.cookies.clear()
        assert (await enter(env, OWNER_PHONE, staff_code)).status_code == 401, "her code no longer opens anything"
        assert (await env.http.post("/office/verify", data={"email": STAFF, "code": staff_code})).status_code == 401
        await text_login(env)
        assert (await env.one(sa.select(db.office_sessions).order_by(db.office_sessions.c.created_at.desc()))).email == OWNER
        assert (await env.http.get("/office/settings")).status_code == 200, "the number now signs in the owner"
    finally:
        await env.http.aclose()
        await env.ctx.engine.dispose()


async def test_the_variable_wins_over_a_number_the_owner_had_set_on_the_page(tmp_path) -> None:
    env = await make_env(tmp_path)
    try:
        csrf = await office_login(env)
        assert "Saved" in said(await give(env, csrf, OWNER, OTHER_PHONE)), "with no variable he may set his own"
        await restart(env, owner_phones=((OWNER, OWNER_PHONE),))
        assert await numbers(env) == {OWNER: OWNER_PHONE}
        assert (await entries(env, "office_phone_set"))[-1].detail == {
            "email": OWNER, "ends": "0142", "was": "0144", "by": "VIDEO_OWNER_PHONES"}
        texts = len(env.texter.sent)
        await ask(env, OTHER_PHONE)
        assert len(env.texter.sent) == texts, "the number he had before signs nobody in"
    finally:
        await env.http.aclose()
        await env.ctx.engine.dispose()


async def test_a_number_from_the_variable_cannot_be_changed_or_taken_away_on_the_page(env: Env) -> None:
    csrf = await text_login(env)
    fixed = "err=That number is set in the server's settings and cannot be changed here."
    assert fixed in said(await give(env, csrf, OWNER, OTHER_PHONE))
    assert fixed in said(await take_away(env, csrf, OWNER))
    assert await numbers(env) == {OWNER: OWNER_PHONE}
    assert len(await entries(env, "office_phone_set")) == 1 and await entries(env, "office_phone_removed") == []


async def test_taking_the_entry_out_of_the_variable_removes_nothing_and_the_page_can_then_take_it_away(env: Env) -> None:
    await restart(env, owner_phones=())
    assert await numbers(env) == {OWNER: OWNER_PHONE}, "the number stays his"
    csrf = await text_login(env)
    page = html.unescape((await env.http.get("/office/users")).text)
    assert "(set in the server's settings)" not in page and "Take number away" in page, "and it is no longer fixed"
    assert "Taken away" in said(await take_away(env, csrf, OWNER))
    assert await numbers(env) == {}
    await restart(env, owner_phones=())
    assert await numbers(env) == {}, "and a start does not bring it back"


ONE = ("owner@zest.test",)
TWO = ("owner@zest.test", "boss@zest.test")


@pytest.mark.parametrize("written, owners, read", [
    ("", ONE, ()),
    ("916-555-0142", ONE, (("owner@zest.test", "9165550142"),)),  # one owner: his number alone will do
    ("owner@zest.test=(916) 555-0142", ONE, (("owner@zest.test", "9165550142"),)),
    (" Owner@Zest.Test = +1 916 555 0142 ,", ONE, (("owner@zest.test", "9165550142"),)),
    ("owner@zest.test=9165550142,boss@zest.test=9165550143", TWO,
     (("owner@zest.test", "9165550142"), ("boss@zest.test", "9165550143"))),
])
def test_how_the_variable_is_written(written: str, owners: tuple[str, ...], read: tuple) -> None:
    assert config.read_owner_phones(written, owners) == (read, ())


@pytest.mark.parametrize("written, owners, kept, why", [
    ("555-0142", ONE, 0, "the number in it is not a mobile number of 10 digits"),
    ("owner@zest.test=916555014", ONE, 0, "the number in it is not a mobile number of 10 digits"),
    ("owner@zest.test=call me", ONE, 0, "the number in it is not a mobile number of 10 digits"),
    ("staff@zest.test=9165550142", ONE, 0, "the email in it is not one of the owners in VIDEO_OWNER_EMAILS"),
    ("=9165550142", ONE, 0, "the email in it is not one of the owners in VIDEO_OWNER_EMAILS"),
    ("9165550142", TWO, 0, "it has no email in front of the number"),
    ("9165550142", (), 0, "it has no email in front of the number"),
    ("owner@zest.test=9165550142,owner@zest.test=9165550143", ONE, 1, "an entry before it already gives that owner a number"),
    ("owner@zest.test=9165550142,boss@zest.test=9165550142", TWO, 1,
     "an entry before it already gives that number to another owner"),
])
def test_a_bad_entry_is_skipped_with_one_plain_line_that_never_repeats_it(
    written: str, owners: tuple[str, ...], kept: int, why: str
) -> None:
    good, notes = config.read_owner_phones(written, owners)
    assert len(good) == kept
    (note,) = notes
    assert note.startswith("WARNING: VIDEO_OWNER_PHONES, entry ") and why in note and "was skipped" in note
    assert "Every other entry still applies" in note
    assert "emailed code" not in note, "it promises no email: on a server with no mail service none would come"
    for entry in written.split(","):
        for part in (entry, *entry.split("=")):
            if part.strip() and part.strip() not in owners:
                assert part.strip() not in note, "the line names the variable, never what was written in it"
    assert not re.search(r"\d{4}", note), "no digits of anybody's number"


def test_a_bad_entry_does_not_spoil_the_good_ones() -> None:
    good, notes = config.read_owner_phones(
        "boss@zest.test=555-0143, owner@zest.test=916-555-0142, friend@example.com=9165550144", TWO)
    assert good == (("owner@zest.test", "9165550142"),)
    assert [note.split(",")[1].strip() for note in notes] == ["entry 1 of 3", "entry 3 of 3"]


# What Railway puts in every container, and the settings a real start needs. All made up.
PRODUCTION = {
    "RAILWAY_ENVIRONMENT_NAME": "production", "RAILWAY_SERVICE_NAME": "zest-video",
    "VIDEO_DATABASE_URL": "postgresql://postgres:made-up-db-password@postgres.railway.internal:5432/railway",
    "VIDEO_S3_ENDPOINT": "https://storage.example.com", "VIDEO_S3_BUCKET": "zest-videos",
    "VIDEO_S3_ACCESS_KEY_ID": "made-up-key-id", "VIDEO_S3_SECRET_ACCESS_KEY": "made-up-secret-key",
    "VIDEO_EMAIL_MODE": "off", "FR_WRITES": "off",
    "VIDEO_OWNER_EMAILS": "owner@zest.test, boss@zest.test",
}
QUO = {"QUO_API_KEY": "made-up-quo-key", "QUO_FROM_NUMBER": "+19165550100"}


@pytest.fixture
def start(monkeypatch, capsys):
    """The real entry point, `main()`, with the web server and the database stood in for.
    start(settings) runs it with exactly those settings and returns (the Config it read,
    what it printed, whether it got as far as serving)."""
    import uvicorn

    def run(settings: dict[str, str]) -> tuple[config.Config, str, bool]:
        for name in list(os.environ):
            if name == "VIDEO_TEST_DATABASE_URL":
                continue  # the test suite's own switch, not a setting of the server
            if name.startswith(("VIDEO_", "RAILWAY_", "RESEND_", "QUO_", "APNS_")) or name in (
                "DATABASE_URL", "FR_WRITES", "FR_WRITE_CUSTOMER_IDS",
            ):
                monkeypatch.delenv(name)
        for name, value in settings.items():
            monkeypatch.setenv(name, value)
        served: list[Any] = []

        async def answers(engine: Any) -> None:
            return None

        monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: served.append(app))
        monkeypatch.setattr(db, "create_all", answers)
        capsys.readouterr()
        video_app.main()
        return config.load(), capsys.readouterr().err, len(served) == 1

    return run


def test_the_first_line_of_a_start_says_how_many_owner_numbers_are_set_and_never_one(start) -> None:
    cfg, err, served = start({**PRODUCTION, **QUO, "VIDEO_OWNER_PHONES": "owner@zest.test=916-555-0142,boss@zest.test=9165550143"})
    assert served and cfg.owner_phones == (("owner@zest.test", "9165550142"), ("boss@zest.test", "9165550143"))
    assert "text=on, filed under Done in Quo; owner-phones=2; push=off;" in err
    assert "VIDEO_OWNER_PHONES" not in err, "nothing to say about a variable that is all good"
    for number in ("9165550142", "9165550143"):
        for written in (*every_way_of_writing(number), "916-555-0142"):
            assert written not in err


def test_with_the_variable_not_set_the_count_is_zero_and_the_start_is_as_it_was(start) -> None:
    cfg, err, served = start({**PRODUCTION, **QUO})
    assert served and cfg.owner_phones == () and cfg.owner_phone_notes == ()
    assert "owner-phones=0;" in err and "WARNING" not in err


def test_a_bad_entry_never_stops_a_start_and_the_rest_still_apply(start) -> None:
    cfg, err, served = start({
        **PRODUCTION, **QUO,
        "VIDEO_OWNER_PHONES": "owner@zest.test=555-0142, boss@zest.test=916-555-0143, friend@example.com=9165550144",
    })
    assert served, "it got as far as serving"
    assert "NOT starting" not in err
    assert cfg.owner_phones == (("boss@zest.test", "9165550143"),) and "owner-phones=1;" in err
    lines = [line for line in err.splitlines() if "VIDEO_OWNER_PHONES" in line]
    assert len(lines) == 2 and all(line.startswith("zest-video: WARNING: VIDEO_OWNER_PHONES, entry ") for line in lines)
    assert "entry 1 of 3, was skipped: the number in it is not a mobile number of 10 digits" in lines[0]
    assert "entry 3 of 3, was skipped: the email in it is not one of the owners in VIDEO_OWNER_EMAILS" in lines[1]
    for value in ("555-0142", "5550142", "9165550143", "916-555-0143", "9165550144", "friend@example.com"):
        assert value not in err, "names only; a value is never printed"


# What a start says about the email way, by the mail service the server has. It names the
# email way as the way in only when an emailed code reaches an inbox.
MAIL = [
    ({"RESEND_API_KEY": "re_made_up_key"}, "An emailed code still works."),
    ({"VIDEO_MAIL_BACKEND": "log"}, "No mail service is connected either (mail=log): a code asked for by email is only printed in this log."),
    ({}, "No mail service is connected either, so no code can be emailed."),
]


@pytest.mark.parametrize("mail, by_email", MAIL)
def test_a_start_says_when_owner_numbers_are_set_and_no_text_can_be_sent(start, mail: dict, by_email: str) -> None:
    _, err, served = start({**PRODUCTION, **mail, "VIDEO_OWNER_PHONES": "owner@zest.test=916-555-0142"})
    assert served and "text=off; owner-phones=1;" in err
    (line,) = [line for line in err.splitlines() if "WARNING" in line]
    assert "WARNING: VIDEO_OWNER_PHONES is set, but texting is off" in line and line.endswith(by_email)
    assert ("emailed code still works" in line) == ("RESEND_API_KEY" in mail), "no promise of an email that cannot come"
    _, err, _ = start({**PRODUCTION, **mail, **QUO, "VIDEO_OWNER_PHONES": "owner@zest.test=916-555-0142"})
    assert "texting is off" not in err and "WARNING" not in err


@pytest.mark.parametrize("mail, by_email", MAIL)
def test_a_start_says_when_an_owners_number_is_the_line_the_codes_are_texted_from(start, mail: dict, by_email: str) -> None:
    """No code is texted to the office's own line (the sign-in page refuses it in plain
    words). So an owner whose number in the variable is that line gets no code by text.
    The start says so, by the names of the two variables, and goes on."""
    cfg, err, served = start({**PRODUCTION, **mail, **QUO, "VIDEO_OWNER_PHONES": "owner@zest.test=(916) 555-0100"})
    assert served and "owner-phones=1;" in err and cfg.owner_phones == (("owner@zest.test", "9165550100"),)
    (line,) = [line for line in err.splitlines() if "WARNING" in line]
    assert "a number in VIDEO_OWNER_PHONES is the same as QUO_FROM_NUMBER" in line
    assert "No code is texted to that line" in line and "That owner gets no code by text until" in line
    assert line.endswith(by_email)
    assert "5550100" not in err and "555-0100" not in err


def test_an_owners_number_is_cut_out_of_any_line_bound_for_the_log(env: Env) -> None:
    line = config.hush(f"could not write ('{OWNER_PHONE}', 'owner@zest.test') to office_phones", env.ctx.cfg)
    assert OWNER_PHONE not in line and "office_phones" in line


async def test_the_settings_page_counts_the_owner_numbers_and_shows_none(env: Env) -> None:
    await text_login(env)
    r = await env.http.get("/office/settings")
    page = html.unescape(r.text)
    row = next(line for line in page.splitlines() if "VIDEO_OWNER_PHONES" in line)
    assert "1 set" in row
    for written in every_way_of_writing(OWNER_PHONE):
        assert written not in page


# == when a text cannot be sent, the page says so and the email way still works ==========


async def test_with_texting_not_set_up_the_page_says_so_and_points_to_the_email_way(env: Env) -> None:
    no_texting(env)
    answers = {number: await ask(env, number) for number in (OWNER_PHONE, STRANGER)}
    for r in answers.values():
        page = html.unescape(r.text)
        assert r.status_code == 503
        assert "This server cannot send texts yet. Type your email here, and the code comes by email." in page
        assert "we just texted" not in page, "it never says a text went"
        assert 'name="contact"' in page, "and the box is right there to type the email in"
    assert answers[OWNER_PHONE].text == answers[STRANGER].text, "the same answer for every number"
    assert await env.all(sa.select(db.login_codes)) == [], "no code is made that nobody could get"

    r = await ask(env, OWNER)
    assert r.status_code == 200 and "we just emailed it a 6-digit code" in r.text
    r = await env.http.post("/office/verify", data={"email": OWNER, "code": code_from(env.mailer.sent[-1].text)})
    assert r.status_code == 303, "the email way is the way back in"


FAILED = ("Something went wrong sending the text, so a code may not come. If one comes, type it below. If none "
          "comes in a minute, ask for a new one.")


@pytest.mark.parametrize("broken, asked, logged", [
    ({"refuse_with": (402, "0200402")}, 1, "Quo answered 402 to"),  # out of credits
    ({"refuse_with": (500, "0200500")}, 1, "Quo answered 500 to"),
    # Quo's "no" to the request itself. The texter tries once more without its Done field,
    # and when that is refused too it says nothing at all: the field was not why. These are
    # the likely answers for a number that once replied STOP.
    ({"refuse_with": (400, "0200400")}, 2, "Quo answered 400 to"),
    ({"refuse_with": (403, "1001403")}, 2, "Quo answered 403 to"),
    ({"refuse_with": (404, "1000404")}, 2, "Quo answered 404 to"),
    ({"refuse_with": (422, "0200422")}, 2, "Quo answered 422 to"),
    ({"no_answer": httpx.ConnectError("refused")}, 1, "got no answer from Quo, so nobody knows whether it went"),
    ({"no_answer": httpx.ReadTimeout("slow")}, 1, "got no answer from Quo, so nobody knows whether it went"),
])
async def test_when_quo_refuses_or_is_down_the_page_says_so_the_log_says_so_and_the_email_way_still_works(
    env: Env, capsys, broken: dict, asked: int, logged: str
) -> None:
    quo = use_quo(env)
    for name, value in broken.items():
        setattr(quo, name, value)
    capsys.readouterr()
    r = await ask(env, OWNER_PHONE)
    page = html.unescape(r.text)
    assert r.status_code == 503 and "we just texted" not in page, "it never says a text went"
    assert f"{FAILED} Or start again and type your email: the code then comes by email." in page
    assert quo.texts == [] and len(quo.requests) == asked, "and nothing is asked of Quo beyond what the texter does"
    (entry,) = await entries(env, "code_requested")
    assert entry.detail == {"by": "text", "ends": "0142", "delivered": []}
    assert QUO_KEY not in page and "0200" not in page and "1001403" not in page, "none of Quo's own words reach the page"

    # One line in the log, every time. The owner this happens to cannot sign in to read the
    # audit trail, so the log is the only place anyone can find out why.
    err = capsys.readouterr().err
    (line,) = our_lines(err)
    assert line.startswith("zest-video: ") and logged in line and "the number ending 0142" in line
    assert line.endswith("The sign-in page said a code may not come.")
    unsent = code_from(quo.bodies()[0]["content"])
    assert unsent not in err, "the code that was to be texted is not in the log"
    for written in (*every_way_of_writing(OWNER_PHONE), f"+1{OWNER_PHONE}"):
        assert written not in err

    r = await ask(env, OWNER)
    assert r.status_code == 200 and "we just emailed it a 6-digit code" in r.text
    code = code_from(env.mailer.sent[-1].text)
    assert (await env.http.post("/office/verify", data={"email": OWNER, "code": code})).status_code == 303
    assert code not in capsys.readouterr().err


async def test_a_failed_text_has_its_line_in_the_log_with_the_done_switch_off_too(env: Env, capsys) -> None:
    """With QUO_MARK_DONE=off the texter never writes a line of its own."""
    quo = use_quo(env, quo_mark_done=False)
    quo.refuse_with = (500, "0200500")
    capsys.readouterr()
    assert (await ask(env, OWNER_PHONE)).status_code == 503
    (line,) = [line for line in capsys.readouterr().err.splitlines() if "Quo" in line]
    assert "Quo answered 500 to an office sign-in code text for the number ending 0142." in line


async def test_a_text_that_went_while_its_answer_was_lost_is_not_sent_twice_and_not_called_unsent(env: Env) -> None:
    quo = use_quo(env)
    quo.answer_lost = True
    r = await ask(env, OWNER_PHONE)
    assert r.status_code == 503 and "a code may not come" in r.text and "did not" not in r.text
    assert len(quo.texts) == 1, "one text, whatever became of the answer"


async def test_after_a_lost_answer_the_page_has_the_box_for_the_code_that_may_have_come(env: Env) -> None:
    """The text went, and Quo's answer never arrived. The page that says "a code may not
    come" used to send him back to the first step, where asking again killed the code that
    was on his phone by then."""
    quo = use_quo(env)
    quo.answer_lost = True
    r = await ask(env, OWNER_PHONE)
    page = html.unescape(r.text)
    assert r.status_code == 503 and "If one comes, type it below." in page
    assert 'name="code"' in page and f'<input type="hidden" name="phone" value="{OWNER_PHONE}">' in page
    assert "we just texted" not in page
    (text,) = quo.texts
    assert (await enter(env, OWNER_PHONE, code_from(text["content"]))).status_code == 303, "and the code that came works"


async def test_the_code_page_has_a_button_for_a_new_code_and_it_is_the_way_on_after_five_wrong_tries(env: Env) -> None:
    """ "Too many wrong tries. Ask for a new code." used to stand over a page with no way to
    ask but a small link in a sentence about email, and the number to type again."""
    r = await ask(env, OWNER_PHONE)
    again = (f'<form method="post" action="/office/login" style="margin-top:10px">\n'
             f'    <input type="hidden" name="contact" value="{OWNER_PHONE}">\n'
             f'    <button class="ghost" type="submit">Text me a new code</button>')
    assert again in r.text, "on the first code page already: no text after a minute is the other reason to press it"
    assert "A new code stops the old one." in r.text
    code = texted_code(env)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        locked = await enter(env, OWNER_PHONE, wrong)
    page = html.unescape(locked.text)
    assert locked.status_code == 423 and "Too many wrong tries. Ask for a new code." in page
    assert again in locked.text
    assert "we just texted it" not in page, "the locked page does not say a text has just gone"
    assert f"Code for {shown(OWNER_PHONE)}" in page, "it still says whose code it is"

    # Pressing it is the first step again, with the number filled in.
    r = await ask(env, OWNER_PHONE)
    assert r.status_code == 200 and len(env.texter.sent) == 2
    assert (await enter(env, OWNER_PHONE, texted_code(env))).status_code == 303

    # The same button whoever the number belongs to.
    stranger = await ask(env, STRANGER)
    assert again.replace(OWNER_PHONE, STRANGER) in stranger.text


# == on a server with no mail service, no page points to the email way ===================
#
# The live server, the day the owner asked for this: mail backend "log". An "emailed" code
# is one line in the Railway log and reaches no inbox. Every page that said "type your
# email and the code comes by email" sent him back to the dead end he started from.


@pytest.mark.parametrize("backend, words", [
    ("log", "This server cannot send texts yet, and it cannot send email yet either. A code asked for by email is "
            "only written to the server's log."),
    ("off", "This server cannot send texts yet, and it cannot send email yet either."),
])
async def test_with_no_texting_and_no_mail_service_the_page_does_not_promise_an_email(env: Env, backend: str, words: str) -> None:
    no_texting(env)
    no_mail_service(env, backend)
    r = await ask(env, OWNER_PHONE)
    page = html.unescape(r.text)
    assert r.status_code == 503 and f'<div class="banner err">{words}</div>' in page
    assert "comes by email" not in page and "comes by text" not in page, "the line under the box promises neither"
    assert "This server cannot send texts yet. It cannot send email yet either.</p>" in page


@pytest.mark.parametrize("backend, words", [
    ("log", f"{FAILED} This server cannot send email yet: a code asked for by email is only written to the server's log."),
    ("off", f"{FAILED} This server cannot send email yet."),
])
async def test_when_a_text_fails_and_there_is_no_mail_service_the_page_does_not_promise_an_email(
    env: Env, backend: str, words: str
) -> None:
    quo = use_quo(env)
    quo.refuse_with = (402, "0200402")
    no_mail_service(env, backend)
    r = await ask(env, OWNER_PHONE)
    page = html.unescape(r.text)
    assert r.status_code == 503 and f'<div class="banner err">{words}</div>' in page
    assert "comes by email" not in page and "type your email" not in page
    assert "Text me a new code" in page and "Use another number" in page


async def test_with_no_mail_service_the_sign_in_pages_say_so_and_point_nobody_to_an_email(env: Env) -> None:
    no_mail_service(env)
    first = html.unescape((await env.http.get("/office/login")).text)
    assert "Type the mobile number the owner set for you and the code comes by text. Email is not set up on this server yet." in first
    assert "comes by email" not in first

    code_page = html.unescape((await ask(env, OWNER_PHONE)).text)
    assert "we just texted it a 6-digit code" in code_page
    assert "No text after a minute? Ask for a new one. A new code stops the old one." in code_page
    assert "email" not in code_page.split("</h1>")[1], "nothing on the code page sends him to an email that cannot come"
    assert (await enter(env, OWNER_PHONE, texted_code(env))).status_code == 303, "and the text way works without it"


async def test_with_a_mail_service_the_same_pages_point_to_the_email_way(env: Env) -> None:
    first = html.unescape((await env.http.get("/office/login")).text)
    assert ("Type the mobile number the owner set for you and the code comes by text. Type your work email and it "
            "comes by email.</p>") in first
    no_texting(env)  # a mail service and no Quo key: the email way is the one that works
    first = html.unescape((await env.http.get("/office/login")).text)
    assert "This server cannot send texts yet. Type your work email and the code comes by email.</p>" in first
    use_quo(env)
    code_page = html.unescape((await ask(env, OWNER_PHONE)).text)
    assert ('No text after a minute? Ask for a new one. A new code stops the old one. Or <a href="/office/login">start '
            "again</a> and type your email: the code then comes by email.") in code_page


async def test_with_no_mail_service_the_users_and_settings_pages_do_not_say_a_code_comes_by_email(env: Env) -> None:
    csrf = await text_login(env)
    await add_user(env, csrf)
    no_mail_service(env)
    users = html.unescape((await env.http.get("/office/users")).text)
    assert "This server cannot send email yet, so only someone with a mobile number here can sign in." in users
    assert "signs in by email" not in users and '<span class="muted">none</span>' in row_of(users, STAFF)

    no_texting(env)
    env.reconfigure(owner_phones=((OWNER, OWNER_PHONE),))
    users = html.unescape((await env.http.get("/office/users")).text)
    assert ("This server cannot send texts yet, and it cannot send email yet either: for now a code is only written "
            "to the server's log. A number set here starts to work the day texting is switched on.") in users
    settings = html.unescape((await env.http.get("/office/settings")).text)
    row = next(line for line in settings.splitlines() if "VIDEO_OWNER_PHONES" in line)
    assert "1 set · but texting is off, and no email sender is set up, so no code reaches anyone" in row

    no_mail_service(env, "off")
    users = html.unescape((await env.http.get("/office/users")).text)
    assert "and it cannot send email yet either: for now no code reaches anyone." in users


# == the office's own line ===============================================================
#
# The owner's first answer to "which number?" was the office's published line, which is
# the line the codes are texted from. Nobody has seen Quo text a line from itself, and a
# code texted to that line would be read by everyone on it. So it is never tried, and the
# page says why in plain words, where it used to be one warning in a log he does not read.


def the_office_line(env: Env) -> str:
    return security.normalize_phone(QUO_LINE)


async def test_the_line_the_codes_are_texted_from_is_refused_in_plain_words_whoever_has_it(tmp_path) -> None:
    for owner_phones in ((), ((OWNER, security.normalize_phone(QUO_LINE)),)):  # nobody has it; the variable gave it to the owner
        (tmp_path / str(len(owner_phones))).mkdir()
        env = await make_env(tmp_path / str(len(owner_phones)), owner_phones=owner_phones)
        try:
            quo = use_quo(env)
            await auth.ensure_owners(env.ctx)
            r = await ask(env, "(916) 555-0100")
            page = html.unescape(r.text)
            assert r.status_code == 400
            assert ("That is the office's own line. The codes are texted from it, so one cannot be texted to it. "
                    "Type your own mobile number, or your email.") in page
            assert 'name="contact"' in page and "we just texted" not in page
            assert quo.requests == [], "Quo is not asked"
            assert await env.all(sa.select(db.login_codes)) == [], "and no code is made, real or not"

            no_mail_service(env)
            page = html.unescape((await ask(env, the_office_line(env))).text)
            assert "so one cannot be texted to it. Type your own mobile number.</div>" in page, "no email to point to"
        finally:
            await env.http.aclose()
            await env.ctx.engine.dispose()


async def test_the_office_line_cannot_be_given_to_anyone_on_the_users_page(env: Env) -> None:
    use_quo(env)
    csrf = await office_login(env)
    await add_user(env, csrf)
    r = await give(env, csrf, STAFF, "916-555-0100")
    assert ("err=That is the office's own line, the one the codes are texted from. Use the person's own mobile "
            "number.") in said(r)
    assert await numbers(env) == {OWNER: OWNER_PHONE}


async def test_an_owner_whose_number_in_the_variable_is_the_office_line_is_told_so_on_the_users_page(tmp_path) -> None:
    env = await make_env(tmp_path, owner_phones=((OWNER, security.normalize_phone(QUO_LINE)),))
    try:
        use_quo(env)
        await office_login(env)  # by email: his number gets no code
        row = row_of(html.unescape((await env.http.get("/office/users")).text), OWNER)
        assert "Number ending <b>0100</b>" in row and "(set in the server's settings)" in row
        assert "This is the office's own line, the one the codes are texted from, so no code is texted to it." in row
    finally:
        await env.http.aclose()
        await env.ctx.engine.dispose()


async def test_the_text_goes_through_quo_as_a_techs_code_does_and_is_filed_under_done(env: Env, capsys) -> None:
    quo = use_quo(env)
    r = await ask(env, OWNER_PHONE)
    assert r.status_code == 200
    (body,) = quo.bodies()
    code = code_from(body["content"])
    assert body == {
        "content": f"{code} is your Zest Lawn & Pest office code. It works once, for 15 minutes. Do not share it.",
        "from": QUO_LINE,
        "to": ["+19165550142"],
        "setInboxStatus": "done",
    }
    assert quo.inbox == {"+19165550142": "done"} and quo.others == [], "the send and nothing else"
    assert (await enter(env, OWNER_PHONE, code)).status_code == 303
    assert [line for line in capsys.readouterr().err.splitlines() if "Quo" in line] == [], "when it works the log is quiet"


async def test_a_quo_that_will_not_file_it_under_done_still_texts_the_code_once(env: Env, capsys) -> None:
    quo = use_quo(env, FakeQuo(knows_done=False))  # its refusal repeats the code and the whole number
    assert (await ask(env, OWNER_PHONE)).status_code == 200
    (text,) = quo.texts
    code = code_from(text["content"])
    assert (await enter(env, OWNER_PHONE, code)).status_code == 303
    (line,) = [line for line in capsys.readouterr().err.splitlines() if "Quo" in line]
    assert "the number ending 0142" in line and code not in line
    for written in every_way_of_writing(OWNER_PHONE):
        assert written not in line


# == the email way is as it was ==========================================================


async def test_the_email_way_is_unchanged_for_someone_who_also_has_a_number(env: Env) -> None:
    r = await ask(env, OWNER)
    assert r.status_code == 200
    assert f"If <b>{OWNER}</b> is on the office list, we just emailed it a 6-digit code." in r.text
    assert f'<input type="hidden" name="email" value="{OWNER}">' in r.text
    assert env.texter.sent == [], "an email was asked for: no text"
    (mail,) = env.mailer.sent
    code = code_from(mail.text)
    assert (mail.to, mail.tag, mail.subject) == (OWNER, "office_code", f"{code} is your Zest Lawn & Pest office code")
    assert mail.text == f"Your Zest Lawn & Pest office code is {code}\n\nIt works once, for 15 minutes.\n"

    wrong = "000000" if code != "000000" else "111111"
    bad = await env.http.post("/office/verify", data={"email": OWNER, "code": wrong})
    assert bad.status_code == 401 and "not right" in bad.text
    assert (await env.http.post("/office/verify", data={"email": OWNER, "code": code})).status_code == 303
    (asked,) = await entries(env, "code_requested")
    (signed_in,) = await entries(env, "login")
    assert (asked.actor, asked.detail, signed_in.actor, signed_in.detail) == (OWNER, None, OWNER, None)

    stranger = await ask(env, "stranger@example.com")
    assert stranger.status_code == 200 and "we just emailed it a 6-digit code" in stranger.text
    assert len(env.mailer.sent) == 1 and await env.all(sa.select(db.login_codes).where(db.login_codes.c.purpose == "decoy")) == []


async def test_the_shared_sign_in_helper_of_every_other_test_still_signs_in(env: Env) -> None:
    csrf = await office_login(env)
    assert len(csrf) == 64 and env.texter.sent == []


async def test_a_techs_sign_in_is_untouched_and_an_office_number_does_not_sign_in_to_the_app(env: Env) -> None:
    headers = await login(env)  # Pat, by the number on his FieldRoutes record
    assert (await env.http.get("/v1/today", headers=headers)).status_code == 200
    assert env.texter.sent[-1][1].endswith("is your Zest Video code. It works once, for 15 minutes. Do not share it.")
    texts, mails = len(env.texter.sent), len(env.mailer.sent)

    r = await env.http.post("/v1/auth/request-code", json={"contact": OWNER_PHONE})
    assert r.status_code == 200 and (len(env.texter.sent), len(env.mailer.sent)) == (texts, mails)
    # And a tech's number does not open the office page.
    r = await ask(env, "916-555-0101")
    assert r.status_code == 200 and len(env.texter.sent) == texts


# == nothing private in a log or in the audit trail ======================================


async def test_no_whole_number_and_no_code_reaches_the_log_or_the_audit_trail(tmp_path, capsys) -> None:
    """Everything this change can do, in one go, and then every line and every entry is read."""
    env = await make_env(tmp_path)
    try:
        quo = use_quo(env)
        csrf = await office_login(env)
        emailed = code_from(env.mailer.sent[-1].text)
        await add_user(env, csrf)
        await add_user(env, csrf, SECOND)
        await give(env, csrf, STAFF, STAFF_PHONE)
        await give(env, csrf, STAFF, OTHER_PHONE)  # changed
        await give(env, csrf, SECOND, OTHER_PHONE)  # refused: taken
        await give(env, csrf, SECOND, "555-0143")  # refused: not a number
        await give(env, csrf, SECOND, OWNER_PHONE)
        await take_away(env, csrf, STAFF)
        await give(env, csrf, STAFF, STAFF_PHONE)
        await restart(env, owner_phones=((OWNER, OWNER_PHONE),))  # moves the number from SECOND to the owner
        use_quo(env, quo)  # the new server texts through the same Quo
        env.http.cookies.clear()

        await ask(env, STRANGER)
        await enter(env, STRANGER, "000000")
        await ask(env, "not a number")
        await ask(env, STAFF_PHONE)
        await enter(env, STAFF_PHONE, "000000")
        quo.refuse_with = (500, "0200500")
        await ask(env, OWNER_PHONE)  # a text that fails
        quo.refuse_with = None
        quo.knows_done = False  # Quo's refusal now repeats the code and the number
        await ask(env, OWNER_PHONE)
        quo.knows_done = True
        await ask(env, OWNER_PHONE)
        codes = [code_from(text["content"]) for text in quo.texts]
        assert len(codes) == 3
        assert (await enter(env, OWNER_PHONE, codes[-1])).status_code == 303

        err = capsys.readouterr().err
        trail = json.dumps([[e.actor_type, e.actor, e.action, e.detail] for e in await env.all(sa.select(db.events))])
        flags = json.dumps([[f.kind, f.detail, f.note] for f in await env.flags(open_only=False)])
        assert "office_phone_set" in trail and "office_phone_removed" in trail and "code_requested" in trail
        assert "number ending 0142" in err, "the texter's own lines are there, with four digits"
        for number in (OWNER_PHONE, STAFF_PHONE, OTHER_PHONE, STRANGER):
            for written in (*every_way_of_writing(number), f"+1{number}"):
                assert written not in err, "no whole number in the log"
                assert written not in trail and written not in flags, "and none in the audit trail"
        for code in codes:
            assert code not in err, "no texted code in the log"
            assert code not in trail
        assert emailed not in trail
        assert QUO_KEY not in err
    finally:
        await env.http.aclose()
        await env.ctx.engine.dispose()


async def test_a_database_error_does_not_print_the_number_that_was_typed(env: Env) -> None:
    """A statement that fails while it runs (the database restarting under a request) is
    printed by the web server with the values it was run with. Those values are what was
    typed: here, a whole phone number."""
    async with env.ctx.engine.begin() as conn:
        await conn.run_sync(db.office_phones.drop)  # stands in for a database that fails mid-request
    with pytest.raises(sa.exc.DBAPIError) as failed:
        await ask(env, STRANGER)
    with pytest.raises(sa.exc.DBAPIError) as failed_too:
        await enter(env, OWNER_PHONE, "123456")
    for error in (failed.value, failed_too.value):
        text = "".join(__import__("traceback").format_exception(error))
        assert "office_phones" in text, "the statement is still there to read"
        for number in (STRANGER, OWNER_PHONE):
            for written in every_way_of_writing(number):
                assert written not in text
    assert db.make_engine("sqlite+aiosqlite:///:memory:").sync_engine.hide_parameters
    assert db.make_engine("postgresql+asyncpg://u:p@db.example.com/x").sync_engine.hide_parameters


# == a database from before this change ==================================================


async def has_table(env: Env, name: str) -> bool:
    async with env.ctx.engine.connect() as conn:
        return await conn.run_sync(lambda sync: sa.inspect(sync).has_table(name))


async def test_a_server_started_on_a_database_from_before_this_change_starts_and_works(tmp_path) -> None:
    """The live Postgres was made before `office_phones` existed. A start adds the missing
    table and touches nothing else: there are no migrations, so nothing else could be."""
    env = await make_env(tmp_path)
    try:
        # The database as the live one is today: every table but the new one, an owner and a
        # staff user, both signed in by email, and a tech's phone.
        csrf = await office_login(env)
        await add_user(env, csrf)
        tech = await login(env)
        async with env.ctx.engine.begin() as conn:
            await conn.run_sync(db.office_phones.drop)
        assert not await has_table(env, "office_phones")
        users_before = await env.all(sa.select(db.office_users).order_by(db.office_users.c.email))
        columns_before = await columns_of(env)

        await restart(env, owner_phones=((OWNER, OWNER_PHONE),))

        assert await has_table(env, "office_phones"), "the start made the one missing table"
        assert await numbers(env) == {OWNER: OWNER_PHONE}, "and put the owner's number in it"
        after = await columns_of(env)
        assert {t: c for t, c in after.items() if t != "office_phones"} == columns_before, "no other table was touched"
        assert await env.all(sa.select(db.office_users).order_by(db.office_users.c.email)) == users_before

        assert (await env.http.get("/office")).status_code == 200, "the session from before still works"
        assert (await env.http.get("/v1/today", headers=tech)).status_code == 200, "and so does the tech's phone"
        assert (await env.http.get("/office/users")).status_code == 200
        assert "Saved" in said(await give(env, csrf, STAFF, STAFF_PHONE))

        env.http.cookies.clear()
        await text_login(env)  # the owner, by text
        env.http.cookies.clear()
        await text_login(env, STAFF_PHONE)
        env.http.cookies.clear()
        await office_login(env)  # and by email, as before

        await restart(env)  # the start after that one finds the table and leaves it alone
        assert await numbers(env) == {OWNER: OWNER_PHONE, STAFF: STAFF_PHONE}
    finally:
        await env.http.aclose()
        await env.ctx.engine.dispose()


async def columns_of(env: Env) -> dict[str, list[str]]:
    def read(sync: Any) -> dict[str, list[str]]:
        inspector = sa.inspect(sync)
        return {t: [c["name"] for c in inspector.get_columns(t)] for t in inspector.get_table_names() if t in db.metadata.tables}

    async with env.ctx.engine.connect() as conn:
        return await conn.run_sync(read)


def test_the_new_table_is_a_table_of_its_own_and_no_table_from_before_gained_a_column() -> None:
    """`create_all` makes a missing table and never alters one. A column added to a table the
    live database already has would simply not be there. This list is the tables as they
    were before this change; a new column on any of them fails here."""
    before = {
        "office_users": ["email", "role", "active", "added_at", "added_by"],
        "office_sessions": ["token_hash", "email", "created_at", "expires_at"],
        "login_codes": ["id", "purpose", "subject", "code_hash", "created_at", "expires_at", "tries", "used_at", "issued_by"],
    }
    for table, columns in before.items():
        assert [c.name for c in db.metadata.tables[table].columns] == columns
    phones = db.office_phones
    assert [c.name for c in phones.columns] == ["phone", "email", "added_at", "added_by"]
    assert [c.name for c in phones.primary_key] == ["phone"] and phones.c.email.unique


# == odd input ===========================================================================


async def test_the_new_forms_survive_null_characters_and_huge_values(env: Env) -> None:
    csrf = await text_login(env)
    await add_user(env, csrf)
    nul = "a\x00b"
    posts = [
        ("/office/users/phone", {"email": f"staff{nul}@zest.test", "phone": f"916{nul}5550143"}),
        ("/office/users/phone", {"email": nul, "phone": "9" * 5000}),
        ("/office/users/phone", {"email": STAFF, "phone": nul, "remove": nul}),
        ("/office/users/phone", {"phone": STAFF_PHONE}),
        ("/office/login", {"contact": nul}),
        ("/office/login", {"contact": "916\x00555\x000142"}),
        ("/office/login", {"contact": "9" * 5000}),
        ("/office/verify", {"phone": nul, "code": nul}),
        ("/office/verify", {"phone": "9" * 5000, "code": "9" * 5000}),
    ]
    for path, data in posts:
        r = await env.http.post(path, data={"csrf": csrf, **data})
        assert r.status_code < 500, f"{path} answered {r.status_code}"
