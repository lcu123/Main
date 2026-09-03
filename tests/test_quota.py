"""Quota and rate-limit depth: day rollover, env-configured limits, refusal
not consuming a call, and the rate limiter's correctness under real
concurrency (not just sequential calls).

The 95%-threshold and reads/writes-independent behavior are already covered
where they were introduced (test_wire_format.py); this file covers what
wasn't: what happens across a day boundary, whether a refused call is
free, and whether asyncio.gather concurrency can race past the sliding
window's lock.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import date

import httpx
import pytest

os.environ.setdefault("FR_SUBDOMAIN", "zest")
os.environ.setdefault("FR_AUTH_KEY", "key")
os.environ.setdefault("FR_AUTH_TOKEN", "token")

import fr_mcp.client as client_module  # noqa: E402
from fr_mcp.client import FieldRoutesClient, RateLimiter, UsageCounter  # noqa: E402

from conftest import FakeFR  # noqa: E402


class _FrozenDate:
    """Stand-in for datetime.date with a controllable .today(), for testing
    UsageCounter's day rollover without waiting for a real midnight."""

    current = date(2026, 1, 1)

    @classmethod
    def today(cls) -> date:
        return cls.current


def test_usage_counter_resets_reads_and_writes_at_day_rollover(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "date", _FrozenDate)
    _FrozenDate.current = date(2026, 1, 1)
    usage = UsageCounter(read_limit=100, write_limit=100)
    usage.record(True)
    usage.record(True)
    usage.record(False)
    assert usage.snapshot() == {"date": "2026-01-01", "reads": 2, "readLimit": 100, "writes": 1, "writeLimit": 100}

    _FrozenDate.current = date(2026, 1, 2)
    snap = usage.snapshot()
    assert snap["date"] == "2026-01-02"
    assert snap["reads"] == 0 and snap["writes"] == 0

    # And a threshold hit on day 1 doesn't carry over to day 2.
    usage2 = UsageCounter(read_limit=2, write_limit=100)
    _FrozenDate.current = date(2026, 1, 1)
    usage2.record(True)
    usage2.record(True)
    with pytest.raises(client_module.FieldRoutesError, match="Daily read quota"):
        usage2.check(True)
    _FrozenDate.current = date(2026, 1, 2)
    usage2.check(True)  # does not raise: fresh day, fresh budget


def test_reads_and_writes_are_independently_thresholded() -> None:
    usage = UsageCounter(read_limit=100, write_limit=4)
    for _ in range(3):
        usage.record(False)
    with pytest.raises(client_module.FieldRoutesError, match="Daily write quota"):
        usage.check(False)  # 3/4 writes hits int(4*0.95)=3
    usage.check(True)  # reads are untouched by the write threshold
    usage.record(True)
    assert usage.snapshot()["reads"] == 1


def test_client_reads_daily_limits_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FR_DAILY_READ_LIMIT", "50")
    monkeypatch.setenv("FR_DAILY_WRITE_LIMIT", "20")
    fake = FakeFR()
    client = FieldRoutesClient(
        subdomain="zest", auth_key="key", auth_token="token", transport=httpx.MockTransport(fake.handler)
    )
    assert client.usage.read_limit == 50
    assert client.usage.write_limit == 20


def test_client_daily_limits_default_to_3000_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FR_DAILY_READ_LIMIT", raising=False)
    monkeypatch.delenv("FR_DAILY_WRITE_LIMIT", raising=False)
    fake = FakeFR()
    client = FieldRoutesClient(
        subdomain="zest", auth_key="key", auth_token="token", transport=httpx.MockTransport(fake.handler)
    )
    assert client.usage.read_limit == 3000
    assert client.usage.write_limit == 3000


def test_client_daily_limit_env_ignores_garbage_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FR_DAILY_READ_LIMIT", "not-a-number")
    fake = FakeFR()
    client = FieldRoutesClient(
        subdomain="zest", auth_key="key", auth_token="token", transport=httpx.MockTransport(fake.handler)
    )
    assert client.usage.read_limit == 3000  # falls back to the default rather than crashing


async def test_refused_call_never_reaches_the_server_or_counts_against_quota() -> None:
    fake = FakeFR()
    client = FieldRoutesClient(
        subdomain="zest",
        auth_key="key",
        auth_token="token",
        transport=httpx.MockTransport(fake.handler),
        rate_limiter=RateLimiter(limit=100000),
        usage=UsageCounter(read_limit=2, write_limit=100),
    )
    async with client:
        await client.call("office", "search")
        assert client.usage.snapshot()["reads"] == 1
        with pytest.raises(client_module.FieldRoutesError, match="Daily read quota"):
            await client.call("office", "search")
        # The refusal itself must not be counted -- otherwise a client that
        # keeps calling after being refused would ratchet the count forever.
        assert client.usage.snapshot()["reads"] == 1
    assert len(fake.requests) == 1


async def test_rate_limiter_holds_under_concurrent_gather() -> None:
    # A sliding window guarded by a naive check-then-append (not atomic under
    # concurrency) would let more than `limit` calls through when many
    # coroutines call acquire() at once. Prove the asyncio.Lock actually
    # serializes them: 6 concurrent acquires against limit=2/window=0.3s
    # must take at least 2 window-lengths, not complete instantly.
    limiter = RateLimiter(limit=2, window=0.3)
    start = time.monotonic()
    await asyncio.gather(*[limiter.acquire() for _ in range(6)])
    elapsed = time.monotonic() - start
    # 6 calls at 2/window means the last pair waits for 2 full windows.
    assert elapsed >= 0.55, f"6 calls at limit=2 should take >=2 windows, took {elapsed:.2f}s"


async def test_rate_limiter_never_exceeds_limit_in_any_window() -> None:
    limiter = RateLimiter(limit=3, window=0.3)
    timestamps: list[float] = []

    async def _tracked_acquire() -> None:
        await limiter.acquire()
        timestamps.append(time.monotonic())

    await asyncio.gather(*[_tracked_acquire() for _ in range(9)])
    timestamps.sort()
    # No 3 consecutive acquisitions may land within a single window.
    for i in range(len(timestamps) - 3):
        assert timestamps[i + 3] - timestamps[i] >= 0.29, "more than `limit` calls landed inside one window"
