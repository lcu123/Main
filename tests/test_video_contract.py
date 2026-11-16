"""The app's practice server and the real server must answer the same way.

The iPhone app's UI tests never talk to this server: they talk to
`ios/Scripts/fake_server.py`, a small stand-in that re-implements the rules in
`docs/video/API.md`. The app was built against that stand-in. If the two drift apart,
every UI test still passes and the first real phone finds out.

This test plays one scripted day against both, call for call, and compares what comes
back: the status, the error word, and the shape of the answer. Everything the stand-in
sends (which is what the app was built to read) must be in the real answer with the
same type. The real server may send more.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from video_helpers import Env, code_from, make_env, video_body

from zest_video import videos

ROOT = Path(__file__).resolve().parents[1]
FAKE = ROOT / "ios" / "Scripts" / "fake_server.py"
DEVICE = {"deviceName": "Test iPhone", "deviceModel": "iPhone15,2", "osVersion": "18.1", "appBuild": 1}


@pytest.fixture
async def env(tmp_path) -> Env:
    e = await make_env(tmp_path)
    yield e
    await e.http.aclose()
    await e.ctx.engine.dispose()


@pytest.fixture
def fake_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, str(FAKE), "--port", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                if httpx.get(f"{url}/__test/ping", timeout=1, trust_env=False).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.05)
        else:
            raise RuntimeError("the practice server did not start")
        yield url
    finally:
        proc.terminate()
        proc.wait(timeout=5)


class Real:
    """The real server, in-process, with the made-up day from video_helpers."""

    name = "real"
    contact = "916-555-0101"
    stop_a, stop_b = 501, 503

    def __init__(self, env: Env):
        self.env = env

    async def call(self, method: str, path: str, json: Any = None, token: str | None = None) -> tuple[int, Any]:
        headers = {"X-Zest-Build": "1"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = await self.env.http.request(method, path, json=json, headers=headers)
        return r.status_code, (r.json() if r.content else None)

    async def code(self) -> str:
        return code_from(self.env.texter.sent[-1][1])

    async def upload(self, answer: dict, data: bytes) -> None:
        assert answer["upload"]["method"] == "PUT" and answer["upload"]["headers"] == {"Content-Type": "video/mp4"}
        self.env.storage.objects[(await self.env.video(answer["id"])).object_key] = data

    async def locked_customer(self, token: str, today: dict) -> int:
        """A stop whose video already went to its customer."""
        body = video_body(502, stopDate=today["date"])
        status, answer = await self.call("POST", "/v1/videos", body, token)
        assert status == 200, answer
        await self.upload(answer, b"v" * body["sizeBytes"])
        await self.call("POST", f"/v1/videos/{body['id']}/complete", {"sizeBytes": body["sizeBytes"]}, token)
        await self.env.window_closed(body["id"])
        await videos.send_due(self.env.ctx)
        assert (await self.env.video(body["id"])).state == "sent"
        return 502


class Fake:
    """The app's practice server, as its own process, exactly as the UI tests run it."""

    name = "practice"
    contact = "9165550101"
    stop_a, stop_b = 202, 203

    def __init__(self, url: str):
        self.http = httpx.AsyncClient(base_url=url, trust_env=False, timeout=10)

    async def call(self, method: str, path: str, json: Any = None, token: str | None = None) -> tuple[int, Any]:
        headers = {"X-Zest-Build": "1"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = await self.http.request(method, path, json=json, headers=headers)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None

    async def code(self) -> str:
        return "123456"

    async def upload(self, answer: dict, data: bytes) -> None:
        up = answer["upload"]
        r = await self.http.request(up["method"], up["url"], content=data, headers=up["headers"])
        assert r.status_code == 200, r.text

    async def locked_customer(self, token: str, today: dict) -> int:
        return 205  # its fixture: this stop's video was sent 40 minutes ago


async def play(side: Any) -> dict[str, tuple[int, Any]]:
    """One day, the way the app lives it."""
    out: dict[str, tuple[int, Any]] = {}
    call = side.call

    out["request-code"] = await call("POST", "/v1/auth/request-code", {"contact": side.contact})
    out["request-code, nothing typed"] = await call("POST", "/v1/auth/request-code", {"contact": ""})
    out["verify, wrong code"] = await call("POST", "/v1/auth/verify", {"contact": side.contact, "code": "000000", **DEVICE})
    out["verify"] = await call("POST", "/v1/auth/verify", {"contact": side.contact, "code": await side.code(), **DEVICE})
    token = out["verify"][1]["token"]
    out["today, signed out"] = await call("GET", "/v1/today")
    out["today, a made-up token"] = await call("GET", "/v1/today", token="not-a-real-token")
    out["today"] = await call("GET", "/v1/today", token=token)
    today = out["today"][1]
    a = side.stop_a

    def body_for(customer: int | None) -> dict:
        stop = next((s for s in today["stops"] if s["customerId"] == customer), None)
        return video_body(customer, stopDate=today["date"], appointmentIds=stop["appointmentIds"] if stop else [])

    first = body_for(a)
    data = b"v" * first["sizeBytes"]
    out["register"] = await call("POST", "/v1/videos", first, token)
    out["register, the same again"] = await call("POST", "/v1/videos", first, token)
    out["complete, before the file is there"] = await call("POST", f"/v1/videos/{first['id']}/complete", {"sizeBytes": first["sizeBytes"]}, token)
    await side.upload(out["register"][1], data)
    out["complete"] = await call("POST", f"/v1/videos/{first['id']}/complete", {"sizeBytes": first["sizeBytes"]}, token)
    out["complete, the same again"] = await call("POST", f"/v1/videos/{first['id']}/complete", {"sizeBytes": first["sizeBytes"]}, token)
    out["register, once it is held"] = await call("POST", "/v1/videos", first, token)
    out["today, with a held video"] = await call("GET", "/v1/today", token=token)

    second = body_for(a)
    out["register, recording again"] = await call("POST", "/v1/videos", second, token)
    await side.upload(out["register, recording again"][1], data)
    out["complete, recording again"] = await call("POST", f"/v1/videos/{second['id']}/complete", {"sizeBytes": second["sizeBytes"]}, token)
    out["today, after recording again"] = await call("GET", "/v1/today", token=token)
    out["complete, the replaced one"] = await call("POST", f"/v1/videos/{first['id']}/complete", {"sizeBytes": first["sizeBytes"]}, token)

    out["delete"] = await call("DELETE", f"/v1/videos/{second['id']}", token=token)
    out["delete, the same again"] = await call("DELETE", f"/v1/videos/{second['id']}", token=token)
    out["today, after delete"] = await call("GET", "/v1/today", token=token)
    unknown = str(uuid.uuid4())
    out["delete, a video nobody knows"] = await call("DELETE", f"/v1/videos/{unknown}", token=token)
    out["complete, a video nobody knows"] = await call("POST", f"/v1/videos/{unknown}/complete", {"sizeBytes": 5}, token)

    loose = body_for(None)
    out["register, not on my list"] = await call("POST", "/v1/videos", loose, token)
    await side.upload(out["register, not on my list"][1], data)
    out["complete, not on my list"] = await call("POST", f"/v1/videos/{loose['id']}/complete", {"sizeBytes": loose["sizeBytes"]}, token)

    events = {"events": [
        {"type": "record_start", "at": first["recordedAt"], "videoId": first["id"], "customerId": a, "detail": {}},
        {"type": "retake", "at": first["recordedAt"], "videoId": second["id"], "customerId": a, "detail": {"n": 1}},
    ]}
    out["events"] = await call("POST", "/v1/events", events, token)
    out["push token"] = await call("POST", "/v1/devices/push", {"apnsToken": "ab" * 32, "sandbox": True}, token)

    locked = await side.locked_customer(token, today)
    out["today, with a sent video"] = await call("GET", "/v1/today", token=token)
    out["register, at a stop whose video already went"] = await call("POST", "/v1/videos", body_for(locked), token)

    out["logout"] = await call("POST", "/v1/auth/logout", token=token)
    out["today, after logout"] = await call("GET", "/v1/today", token=token)
    out["_ids"] = (0, {"first": first["id"], "second": second["id"], "loose": loose["id"], "a": a, "locked": locked})
    return out


def kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def lacking(practice: Any, real: Any, where: str) -> list[str]:
    """Everything the practice server sends that the real answer lacks or types differently."""
    if practice is None or real is None:
        return []  # null on either side: a stop with no video yet, say. Nothing to compare.
    if isinstance(practice, dict):
        if not isinstance(real, dict):
            return [f"{where}: the app is sent an object, the real server sends {kind(real)}"]
        problems = []
        for key, value in practice.items():
            if key not in real:
                problems.append(f"{where}.{key}: the app was built to read this; the real server does not send it")
            else:
                problems += lacking(value, real[key], f"{where}.{key}")
        return problems
    if isinstance(practice, list):
        if not isinstance(real, list):
            return [f"{where}: the app is sent a list, the real server sends {kind(real)}"]
        # Every row on both sides, so a field that is null in the first row still gets compared.
        return sorted({p for a in practice for b in real for p in lacking(a, b, f"{where}[]")})
    if kind(practice) != kind(real) and "null" not in (kind(practice), kind(real)):
        return [f"{where}: {kind(practice)} from the practice server, {kind(real)} from the real one"]
    return []


def stop(today: dict, customer: int) -> dict:
    return next(s for s in today["stops"] if s["customerId"] == customer)


def check_the_day(side_name: str, out: dict[str, tuple[int, Any]]) -> list[str]:
    """What must be true on either server, in the app's own terms."""
    ids = out["_ids"][1]
    first, second, a, locked = ids["first"], ids["second"], ids["a"], ids["locked"]
    wrong: list[str] = []

    def expect(ok: bool, what: str) -> None:
        if not ok:
            wrong.append(f"[{side_name}] {what}")

    expect(out["today"][1]["stops"] != [] and stop(out["today"][1], a)["video"] is None, "the stop starts with no video")
    expect(out["register"][1]["state"] == "awaiting_upload" and out["register"][1]["upload"]["method"] == "PUT", "register hands back an upload")
    expect(out["register, the same again"][1]["state"] == "awaiting_upload", "a repeat register is the same video, still to upload")
    expect(out["complete"][1]["state"] == "held" and bool(out["complete"][1].get("emailAt")), "complete answers held with the time the email goes")
    expect(out["complete, the same again"][1]["state"] == "held", "a repeat complete changes nothing")
    held = out["register, once it is held"][1]
    expect(held["state"] == "held" and held["upload"] is None, "a repeat register once held says: do not upload again")
    with_held = out["today, with a held video"][1]
    expect(stop(with_held, a)["video"] == {"id": first, "state": "held", "byYou": True}, "the stop shows the tech's own held video")
    expect([v["id"] for v in with_held["videos"] if v["customerId"] == a] == [first], "and My Videos lists it once")
    again = out["today, after recording again"][1]
    expect(stop(again, a)["video"]["id"] == second, "after recording again the stop shows the new video")
    expect(first not in [v["id"] for v in again["videos"]], "and the old one is gone from My Videos")
    expect(out["complete, the replaced one"][1]["state"] == "replaced", "a late complete for the replaced video says replaced")
    expect(out["delete"][1] == {"id": second, "state": "deleted"} and out["delete, the same again"][1] == {"id": second, "state": "deleted"},
           "delete answers deleted, and the same on a repeat")
    expect(stop(out["today, after delete"][1], a)["video"] is None, "after delete the stop is open again")
    expect(out["complete, not on my list"][1]["state"] == "unmatched", "a video not on the list waits for the office")
    sent = out["today, with a sent video"][1]
    expect(stop(sent, locked)["video"]["state"] in ("sent", "watched") and stop(sent, locked)["video"]["byYou"] is True,
           "a sent video shows on its stop as the tech's own")
    refused = out["register, at a stop whose video already went"][1]
    expect(refused.get("existing", {}).get("byYou") is True and refused["existing"]["state"] in ("sent", "watched"),
           "the refusal names the tech's own sent video")
    return wrong


async def test_the_apps_practice_server_and_the_real_server_answer_alike(env: Env, fake_url: str) -> None:
    fake = Fake(fake_url)
    try:
        practice = await play(fake)
    finally:
        await fake.http.aclose()
    real = await play(Real(env))

    problems: list[str] = []
    for step, (practice_status, practice_body) in practice.items():
        if step == "_ids":
            continue
        real_status, real_body = real[step]
        if practice_status != real_status:
            problems.append(f"{step}: practice server answers {practice_status}, real server {real_status} ({str(real_body)[:120]})")
            continue
        if practice_status >= 400:
            practice_error, real_error = (practice_body or {}).get("error"), (real_body or {}).get("error")
            if practice_error != real_error:
                problems.append(f"{step}: practice server says {practice_error!r}, real server says {real_error!r}")
            if not (real_body or {}).get("message"):
                problems.append(f"{step}: the real server sends no message for the tech to read")
        problems += lacking(practice_body, real_body, step)

    problems += check_the_day("practice", practice) + check_the_day("real", real)
    assert problems == [], "\n" + "\n".join(problems)
