"""Office-user semantics: the small, pure helpers that decide what an office
person actually sees, tested directly and exhaustively rather than only
incidentally through whichever curated tool happens to call them.

These are worth pinning down precisely because a subtly wrong version can be
silently wrong in a way that reads as correct: `_pick` dropping a legitimate
`0` (a frozen subscription, "no preferred tech") the same way it drops a
missing field is the sharpest example -- the office would see "not set"
where the real answer is "explicitly off."
"""

from __future__ import annotations

import os
from datetime import date

import pytest

os.environ.setdefault("FR_SUBDOMAIN", "zest")
os.environ.setdefault("FR_AUTH_KEY", "key")
os.environ.setdefault("FR_AUTH_TOKEN", "token")

from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from fr_mcp import server  # noqa: E402


# ---------------------------------------------------------------------------
# _pick: falsy-but-meaningful values must survive
# ---------------------------------------------------------------------------


def test_pick_keeps_zero_and_false_but_drops_none_empty_string_and_empty_list() -> None:
    row = {
        "active": 0,  # a frozen subscription -- must not read as "missing"
        "preferredTechID": 0,  # "no preference" -- must not read as "missing"
        "commercialAccount": False,
        "balance": 0.0,
        "notes": None,
        "email": "",
        "subscriptionIDs": [],
        "customerID": 1,
    }
    keys = list(row.keys())
    out = server._pick(row, keys)
    assert out == {"active": 0, "preferredTechID": 0, "commercialAccount": False, "balance": 0.0, "customerID": 1}


def test_pick_only_includes_requested_keys_present_on_the_row() -> None:
    row = {"a": 1, "b": 2}
    assert server._pick(row, ["a", "c"]) == {"a": 1}


# ---------------------------------------------------------------------------
# _frequency_text: every documented code, plus the fallback shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "freq,expected",
    [
        (-1, "one-time"),
        (0, "as needed"),
        (-3, "custom schedule"),
        (30, "monthly"),
        (60, "every 2 months"),
        (90, "every 3 months"),
        (365, "every 365 days"),  # not divisible by 30 -- stays in days, per the real API's own convention
        (45, "every 45 days"),
        (7, "every 7 days"),
        (None, ""),
        ("30", "monthly"),  # comes back as a string on the wire; _int() handles it
    ],
)
def test_frequency_text_table(freq, expected: str) -> None:
    assert server._frequency_text(freq) == expected


# ---------------------------------------------------------------------------
# _name / _address: the display fields every curated read builds on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"fname": "Jane", "lname": "Smith"}, "Jane Smith"),
        ({"fname": "Jane", "lname": "Smith", "companyName": "Acme"}, "Jane Smith (Acme)"),
        ({"fname": "", "lname": "", "companyName": "Acme Corp"}, "Acme Corp"),
        ({"fname": "", "lname": ""}, ""),
        ({}, ""),
        ({"fname": "Jane", "lname": "", "companyName": ""}, "Jane"),
    ],
)
def test_name_combinations(row: dict, expected: str) -> None:
    assert server._name(row) == expected


@pytest.mark.parametrize(
    "row,expected",
    [
        ({"address": "123 Main St", "city": "Folsom", "state": "CA", "zip": "95630"}, "123 Main St, Folsom, CA, 95630"),
        ({"address": "123 Main St", "city": "Folsom"}, "123 Main St, Folsom"),
        ({"city": "Folsom", "state": "CA"}, "Folsom, CA"),
        ({}, ""),
        ({"address": None, "city": "", "state": "CA", "zip": "95630"}, "CA, 95630"),
    ],
)
def test_address_combinations(row: dict, expected: str) -> None:
    assert server._address(row) == expected


def test_customer_summary_int_casts_customer_id() -> None:
    row = {"customerID": "42", "fname": "Jane", "lname": "Smith", "phone1": "916", "lat": "1.0", "lng": "-1.0"}
    out = server._customer_summary(row)
    assert out == {"customerID": 42, "name": "Jane Smith", "address": "", "phone": "916", "lat": "1.0", "lng": "-1.0"}


# ---------------------------------------------------------------------------
# _emp / _emp_list: id -> name resolution, including the comma-string case
# ---------------------------------------------------------------------------


def test_emp_resolves_known_id_falls_back_to_str_for_unknown_and_none_for_zero_or_missing() -> None:
    names = {11: "Carlos Quijas"}
    assert server._emp(names, 11) == "Carlos Quijas"
    assert server._emp(names, "11") == "Carlos Quijas"  # wire values are strings
    assert server._emp(names, 999) == "999"  # unknown id: show the id itself, not silently drop it
    assert server._emp(names, 0) is None  # "no one assigned"
    assert server._emp(names, None) is None


def test_emp_list_splits_comma_string_and_filters_zero() -> None:
    names = {11: "Carlos Quijas", 12: "Iggy Artemenko"}
    assert server._emp_list(names, "11,12") == ["Carlos Quijas", "Iggy Artemenko"]
    assert server._emp_list(names, "11,0,12") == ["Carlos Quijas", "Iggy Artemenko"]
    assert server._emp_list(names, [11, 12]) == ["Carlos Quijas", "Iggy Artemenko"]
    assert server._emp_list(names, "") == []
    assert server._emp_list(names, None) == []


# ---------------------------------------------------------------------------
# Date windows: frozen "today" so these are deterministic, not date-of-test-run
# ---------------------------------------------------------------------------


def test_window_defaults_to_today_plus_six_days(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_today", lambda: date(2026, 3, 15))
    start, end = server._window(None, 7)
    assert (start, end) == ("2026-03-15", "2026-03-21")


def test_window_respects_explicit_start_and_days(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_today", lambda: date(2026, 3, 15))
    start, end = server._window("2026-01-01", 3)
    assert (start, end) == ("2026-01-01", "2026-01-03")


def test_window_clamps_non_positive_days_to_a_single_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_today", lambda: date(2026, 3, 15))
    assert server._window(None, 0) == ("2026-03-15", "2026-03-15")
    assert server._window(None, -5) == ("2026-03-15", "2026-03-15")


def test_parse_date_accepts_datetime_strings_by_truncating_to_date() -> None:
    assert server._parse_date("2026-03-15 08:00:00") == date(2026, 3, 15)


def test_parse_date_falls_back_to_today_when_value_is_falsy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_today", lambda: date(2026, 3, 15))
    assert server._parse_date(None) == date(2026, 3, 15)
    assert server._parse_date("") == date(2026, 3, 15)


def test_parse_date_rejects_garbage_with_a_clean_tool_error() -> None:
    with pytest.raises(ToolError, match="Bad date"):
        server._parse_date("not-a-date")


def test_between_builds_the_operator_object() -> None:
    assert server._between("2026-01-01", "2026-01-07") == {
        "operator": "BETWEEN",
        "value": ["2026-01-01", "2026-01-07"],
    }
