"""A sign-in code text files its own conversation under Done in the office's Quo inbox.

The owner's ask (Nov 16, 2026, the day texting went live): every code text opened a
conversation in the office's shared Quo inbox, and sign-ins must not pile up there. The
server asks for it on the send itself, with the one field Quo's reference has for it
(`"setInboxStatus": "done"`), so there is no second request, no conversation to look up
and no way to touch any conversation but the one the text belongs to.

What these tests hold it to, in the order of what it would cost if it broke:

- a tech still signs in, whatever Quo makes of the new field;
- a tech never gets two codes because of it;
- nothing but the send is ever asked of Quo;
- the request is the documented one, word for word;
- when Quo will not take the field, one plain line says so, with no key, no code and no
  whole phone number in it;
- when a text that carried the field fails and is not tried without it, one such line
  says that too and names the switch that sends the text as it went before: the caller
  passes no failed text on, so without the line a Quo that choked on the field would
  stop every code text and nothing would say why.

Quo here is `video_helpers.FakeQuo`, built from Quo's published reference. Nothing in this
file, and nothing in the server's tests, talks to the real Quo: that the conversation
really leaves the inbox on the office's account is for one real sign-in to show.
"""

from __future__ import annotations

import html
import json
from dataclasses import replace

import httpx
import pytest
import sqlalchemy as sa

from video_helpers import QUO_KEY, QUO_LINE, QUO_SEND, Env, FakeQuo, code_from, make_env, office_login, use_quo

from zest_video import db
from zest_video.notify import DeliveryError, Mailer, Texter

PAT = "+19165550101"  # the mobile number on the tech's own FieldRoutes record (made up)
PLAIN = {"from": QUO_LINE, "to": [PAT]}


@pytest.fixture
async def env(tmp_path) -> Env:
    e = await make_env(tmp_path)
    yield e
    await e.http.aclose()
    await e.ctx.engine.dispose()


async def ask_for_a_code(env: Env) -> httpx.Response:
    return await env.http.post("/v1/auth/request-code", json={"contact": "(916) 555-0101"})


async def sign_in_with(env: Env, code: str) -> httpx.Response:
    return await env.http.post("/v1/auth/verify", json={"contact": "9165550101", "code": code, "appBuild": 1})


async def went_by(env: Env) -> list[str]:
    """How the newest code reached the tech, as the audit trail has it."""
    row = await env.one(
        sa.select(db.events).where(db.events.c.action == "code_requested").order_by(db.events.c.id.desc())
    )
    return list(row.detail["delivered"])


def said_about_quo(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return [line for line in capsys.readouterr().err.splitlines() if "Quo" in line]


# What the log says about a text that asked for Done, failed and was not tried without it.
# `{}` is what Quo did. Pinned word for word: it is the only place a failed code text shows.
FAILED = (
    "zest-video: {} a sign-in code text for the number ending 0101{}, and it was not sent again. The request "
    "asked Quo to file the conversation under Done (setInboxStatus). If no code text has gone through since "
    "the server began asking that, set QUO_MARK_DONE=off on zest-video: the text then goes exactly as it went "
    "before. A code sent by email is not affected."
)


def nothing_private_in(line: str, *more: str) -> None:
    for private in (QUO_KEY, "9165550101", "5550101", QUO_LINE, "is your Zest Video code", *more):
        assert private not in line, "no key, no code, no whole number, and none of Quo's own words"


# == it is done, and the tech signs in ==================================================


async def test_a_code_text_is_filed_under_done_and_the_tech_signs_in(env: Env, capsys) -> None:
    quo = use_quo(env)
    r = await ask_for_a_code(env)
    assert r.status_code == 200 and r.json() == {"ok": True}

    assert [t["to"] for t in quo.texts] == [PAT], "one text, to the number on his own record"
    assert quo.inbox == {PAT: "done"}, "and its conversation is out of the office's inbox"
    assert len(quo.requests) == 1, "in the one request that sent the text: no second call"
    assert await went_by(env) == ["text", "email"]

    r = await sign_in_with(env, code_from(quo.texts[0]["content"]))
    assert r.status_code == 200 and r.json()["tech"]["firstName"] == "Pat"
    assert said_about_quo(capsys) == [], "when it works the log says nothing"


async def test_the_request_is_the_one_quos_reference_describes(env: Env) -> None:
    quo = use_quo(env)
    await ask_for_a_code(env)
    (request,) = quo.requests
    assert request.method == "POST" and str(request.url) == "https://api.quo.com/v1/messages" == QUO_SEND
    assert request.headers["authorization"] == QUO_KEY, "the bare key: Quo takes no Bearer"
    assert request.headers["content-type"] == "application/json"
    body = json.loads(request.content)
    code = code_from(body["content"])
    assert body == {
        "content": f"{code} is your Zest Video code. It works once, for 15 minutes. Do not share it.",
        "from": QUO_LINE,
        "to": [PAT],
        "setInboxStatus": "done",  # the one value Quo's reference allows
    }


async def test_every_code_text_asks_again_because_a_plain_text_reopens_the_conversation(env: Env) -> None:
    """Quo's reference: a text sent without the field shows up "as an open conversation".
    So it is on every code text, not only on the first one to a tech."""
    quo = use_quo(env)
    for _ in range(3):
        assert (await ask_for_a_code(env)).status_code == 200
    assert [b.get("setInboxStatus") for b in quo.bodies()] == ["done", "done", "done"]
    assert len(quo.texts) == 3 and quo.inbox == {PAT: "done"}

    # What one plain text in between would have done, by the same stand-in.
    plain = Texter(replace(env.ctx.cfg, quo_mark_done=False), transport=httpx.MockTransport(quo.handler))
    await plain.send("9165550101", "a text with no mark")
    assert quo.inbox == {PAT: "open"}


async def test_nothing_but_the_send_is_ever_asked_of_quo(env: Env) -> None:
    """No list of conversations, no look-up, no mark on a conversation by its ID: the only
    conversation the server can touch is the one its own text belongs to."""
    quo = use_quo(env)
    await ask_for_a_code(env)
    await ask_for_a_code(env)
    await env.http.post("/v1/auth/request-code", json={"contact": "sam@example.com"})  # a tech with no number
    await env.http.post("/v1/auth/request-code", json={"contact": "9165559999"})  # nobody's number
    assert quo.others == [], "not one request that was not a send"
    assert [str(r.url) for r in quo.requests] == [QUO_SEND, QUO_SEND], "and a text only to the tech who has a number"
    assert set(quo.inbox) == {PAT}


@pytest.mark.parametrize("answers", ["no_conversation", "empty", "not_json"])
async def test_the_server_needs_nothing_from_quos_answer(env: Env, capsys, answers: str) -> None:
    """The mark rides on the send, so there is no ID to read out of the answer. A 202
    without the conversation's ID, with no body or with a page of words is a text sent."""
    quo = use_quo(env)
    quo.answers = answers
    assert (await ask_for_a_code(env)).status_code == 200
    assert len(quo.requests) == 1 and len(quo.texts) == 1 and quo.inbox == {PAT: "done"}
    assert await went_by(env) == ["text", "email"]
    assert (await sign_in_with(env, code_from(quo.texts[0]["content"]))).status_code == 200
    assert said_about_quo(capsys) == []


# == signing in never gets worse for it ==================================================


@pytest.mark.parametrize("status", [400, 403, 404, 422])
async def test_a_quo_that_will_not_take_the_mark_still_texts_the_code_once(env: Env, capsys, status: int) -> None:
    """Quo's own no to the request means nothing was sent, and the mark is the one thing in
    it that was not there on the day texting went live. So the text goes once more as it
    went that day. The tech gets one code and signs in with it. 400 is Quo's answer to a
    request it will not take as written (422 the other way to say it); 403 and 404 are the
    two other refusals its reference has for marking a conversation done."""
    quo = use_quo(env, FakeQuo(knows_done=False))
    quo.refuse_done_with = status
    env.ctx.mailer = Mailer()  # no email at all: the text is the only way in
    r = await ask_for_a_code(env)
    assert r.status_code == 200 and r.json() == {"ok": True}, "the phone is told what it is always told"

    first, second = quo.bodies()
    assert first["setInboxStatus"] == "done"
    assert second == {**PLAIN, "content": first["content"]}, "the same text, exactly as it was sent before this existed"
    assert len(quo.texts) == 1, "ONE text reached his phone: the refused request sent nothing"
    assert quo.inbox == {PAT: "open"}, "its conversation is in the inbox, as before"
    assert await went_by(env) == ["text"]

    code = code_from(quo.texts[0]["content"])
    assert (await sign_in_with(env, code)).status_code == 200

    (line,) = said_about_quo(capsys)
    assert line.startswith("zest-video: Quo would not file a sign-in code text under Done")
    assert f"it answered {status} to setInboxStatus" in line and "ending 0101" in line
    assert "Signing in is not affected" in line
    nothing_private_in(line, code)


async def test_the_phone_never_waits_longer_for_its_text_than_it_did_before(env: Env) -> None:
    """The phone is waiting for its answer while the code is texted. One send waits up to
    15 seconds for Quo, as it always did. When Quo refuses the mark, the second try gets
    only what the refusal left of those 15 seconds: the two together are one wait."""
    quo = use_quo(env)
    await ask_for_a_code(env)
    assert quo.waits == [15.0], "one send, with the patience it had before"

    slow = use_quo(env, FakeQuo(knows_done=False))
    slow.refusal_takes = 0.3
    await ask_for_a_code(env)
    first, second = slow.waits
    assert first == 15.0 and 1.0 <= second <= 14.7, "0.3 seconds went on the refusal"
    assert len(slow.texts) == 1


async def test_one_line_for_each_text_quo_left_open_and_none_for_the_rest(env: Env, capsys) -> None:
    quo = use_quo(env, FakeQuo(knows_done=False))
    for _ in range(2):
        assert (await ask_for_a_code(env)).status_code == 200
    assert len(quo.texts) == 2 and len(quo.requests) == 4
    assert len(said_about_quo(capsys)) == 2

    quo.knows_done = True  # Quo takes it again: the very next text is filed, and nothing is said
    assert (await ask_for_a_code(env)).status_code == 200
    assert quo.inbox == {PAT: "done"} and len(quo.requests) == 5
    assert said_about_quo(capsys) == []


@pytest.mark.parametrize("status, code", [
    (401, "0200401"), (402, "0200402"), (429, "0200429"), (500, "0200500"), (502, ""), (503, ""),
])
async def test_a_send_quo_refuses_is_not_tried_again_and_the_log_says_so(
    env: Env, capsys, status: int, code: str
) -> None:
    """Out of credits, a bad key, the daily limit, a server error. What happened before: no
    text, the code still goes by email, the tech still signs in. What is new: one line in
    the log. `auth._deliver_code` passes no failed text on, so if the mark were what Quo
    answered a 500 to, every code text would stop and nothing would say why."""
    quo = use_quo(env)
    quo.refuse_with = (status, code)
    assert (await ask_for_a_code(env)).status_code == 200
    assert len(quo.requests) == 1, "asked once: no second try, with the mark or without"
    assert quo.texts == [] and quo.inbox == {} and quo.others == [], "nothing sent, nothing marked"
    assert await went_by(env) == ["email"]
    emailed = code_from(env.mailer.sent[-1].text)
    assert (await sign_in_with(env, emailed)).status_code == 200

    (line,) = said_about_quo(capsys)
    assert line == FAILED.format(f"Quo answered {status} to", ""), "the status, the switch, and nothing else"
    assert "QUO_MARK_DONE=off" in line
    nothing_private_in(line, emailed, "Refused", code or "Refused")

    env.ctx.mailer = Mailer()  # a tech with no email: the phone is told what it was told before
    r = await ask_for_a_code(env)
    assert r.status_code == 503 and r.json()["error"] == "server_error"
    assert len(quo.requests) == 2 and quo.texts == []
    assert said_about_quo(capsys) == [line], "and that failed text has its line too"


@pytest.mark.parametrize("status, code", [(400, "0201400"), (403, "0200403"), (404, "0200404")])
async def test_a_text_quo_refuses_for_its_own_reasons_is_not_called_sent(
    env: Env, capsys, status: int, code: str
) -> None:
    """A no that is not about the mark (400: the tech's number opted out of texts; 403: the
    day's cap; 404). The second try without the mark is refused the same way, nothing is
    sent, and it is a failed text as it always was. The log names no switch: the request as
    it was before the mark existed was refused too. With no email either the phone is told
    so, as before."""
    quo = use_quo(env)
    quo.refuse_with = (status, code)
    assert (await ask_for_a_code(env)).status_code == 200
    assert [b.get("setInboxStatus") for b in quo.bodies()] == ["done", None]
    assert quo.texts == [] and quo.inbox == {}
    assert await went_by(env) == ["email"]
    assert said_about_quo(capsys) == []

    env.ctx.mailer = Mailer()
    r = await ask_for_a_code(env)
    assert r.status_code == 503 and r.json()["error"] == "server_error"
    assert quo.texts == [] and said_about_quo(capsys) == []


async def test_a_second_try_that_gets_no_answer_is_the_last_try(env: Env, capsys) -> None:
    """Quo refuses the mark, and the same text without it gets no answer. That one may have
    gone, so there is no third try. The log names no switch here either: what failed is
    the request as it was before the mark existed."""
    quo = use_quo(env, FakeQuo(knows_done=False))
    asked: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        if len(asked) == 2:
            raise httpx.ReadTimeout("no answer", request=request)
        return quo.handler(request)

    env.ctx.texter = Texter(env.ctx.cfg, transport=httpx.MockTransport(handler))
    assert (await ask_for_a_code(env)).status_code == 200
    assert ["setInboxStatus" in json.loads(r.content) for r in asked] == [True, False], "two tries, never a third"
    assert quo.texts == [] and await went_by(env) == ["email"]
    assert said_about_quo(capsys) == []


@pytest.mark.parametrize("error", [
    httpx.ReadTimeout("no answer in 15 seconds"), httpx.ConnectTimeout("no connection"), httpx.ConnectError("no route"),
])
async def test_a_quo_that_does_not_answer_is_asked_once_and_the_log_says_so(env: Env, capsys, error: Exception) -> None:
    quo = use_quo(env)
    quo.no_answer = error
    assert (await ask_for_a_code(env)).status_code == 200
    assert len(quo.requests) == 1 and quo.texts == [] and quo.inbox == {}
    assert await went_by(env) == ["email"]
    emailed = code_from(env.mailer.sent[-1].text)
    assert (await sign_in_with(env, emailed)).status_code == 200

    (line,) = said_about_quo(capsys)
    assert line == FAILED.format("Quo did not answer", f" ({type(error).__name__})"), "the kind of silence, by its name"
    assert "QUO_MARK_DONE=off" in line
    nothing_private_in(line, emailed, str(error))


async def test_a_text_that_went_while_its_answer_was_lost_is_never_sent_twice(env: Env, capsys) -> None:
    """The one case where trying again would put two codes on a tech's phone: Quo took the
    text and its answer never arrived. The server cannot tell that from a text that did not
    go, so it does not try again, with the mark or without. Its line in the log says what
    it knows, that Quo did not answer and nothing was sent again, and not that no text went."""
    quo = use_quo(env)
    quo.answer_lost = True
    assert (await ask_for_a_code(env)).status_code == 200
    assert len(quo.requests) == 1 and len(quo.texts) == 1, "one text on his phone, not two"
    assert quo.inbox == {PAT: "done"}
    assert await went_by(env) == ["email"], "counted as not texted, since nobody can know"
    code = code_from(quo.texts[0]["content"])
    assert (await sign_in_with(env, code)).status_code == 200, "and the code he got works"

    (line,) = said_about_quo(capsys)
    assert line == FAILED.format("Quo did not answer", " (ReadTimeout)")
    nothing_private_in(line, code)


async def test_the_texter_itself_raises_what_it_always_raised(env: Env) -> None:
    """`auth._deliver_code` only knows one thing about a text: it went, or `DeliveryError`."""
    quo = use_quo(env)
    cfg = env.ctx.cfg
    assert await env.ctx.texter.send("9165550101", "123456 is a code") is None
    assert len(quo.texts) == 1

    for change in ({"refuse_with": (402, "0200402")}, {"refuse_with": (400, "0201400")},
                   {"no_answer": httpx.ReadTimeout("slow")}, {"answer_lost": True}):
        broken = FakeQuo()
        for name, value in change.items():
            setattr(broken, name, value)
        with pytest.raises(DeliveryError):
            await Texter(cfg, transport=httpx.MockTransport(broken.handler)).send("9165550101", "123456 is a code")

    wrong_key = FakeQuo(key="another-made-up-key")
    with pytest.raises(DeliveryError):
        await Texter(cfg, transport=httpx.MockTransport(wrong_key.handler)).send("9165550101", "123456 is a code")
    assert len(wrong_key.requests) == 1 and wrong_key.texts == []


# == the switch ==========================================================================


async def test_switched_off_the_text_goes_exactly_as_it_did_before(env: Env, capsys) -> None:
    quo = use_quo(env, quo_mark_done=False)
    assert (await ask_for_a_code(env)).status_code == 200
    (body,) = quo.bodies()
    assert body == {**PLAIN, "content": body["content"]}, "content, from and to: nothing else"
    assert quo.inbox == {PAT: "open"}, "the conversation stays in the inbox"
    assert (await sign_in_with(env, code_from(quo.texts[0]["content"]))).status_code == 200
    assert said_about_quo(capsys) == []


@pytest.mark.parametrize("broken", [{"refuse_with": (500, "0200500")}, {"no_answer": httpx.ReadTimeout("slow")}])
async def test_switched_off_a_failed_text_is_as_quiet_as_it_was_before(env: Env, capsys, broken: dict) -> None:
    """The line exists to name the switch. With the switch already off the request is the
    one from before the mark existed, and a failed text is what it always was."""
    quo = use_quo(env, quo_mark_done=False)
    for name, value in broken.items():
        setattr(quo, name, value)
    assert (await ask_for_a_code(env)).status_code == 200
    assert [b.get("setInboxStatus") for b in quo.bodies()] == [None]
    assert quo.texts == [] and await went_by(env) == ["email"]
    assert said_about_quo(capsys) == []


async def test_the_office_page_says_where_a_code_texts_conversation_goes(env: Env) -> None:
    async def row() -> str:
        page = html.unescape((await env.http.get("/office/settings")).text)
        return next(line for line in page.splitlines() if "Login codes by text" in line)

    await office_login(env)
    assert "off (email only)" in await row()
    use_quo(env)
    assert "on · each code's conversation is filed under Done in Quo" in await row()
    use_quo(env, quo_mark_done=False)
    said = await row()
    assert "stays open in the Quo inbox" in said and "QUO_MARK_DONE" in said
    assert QUO_KEY not in (await env.http.get("/office/settings")).text
