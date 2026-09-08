"""Placer/Yolo portal adapter: permit-type classification against the real live
vocabulary, facility grouping/zip-allowlist filtering, event/territory/chain
lane assignment, the Yolo PDF email attach, and the portal's wire format
(pagination, 25-row cap, circuit breaker on a block)."""

from __future__ import annotations

import asyncio
from datetime import date

import httpx
import pytest

import leads_fixtures as fx
from fr_mcp.leads import myhd
from fr_mcp.leads.pipeline import LANE_CHAIN, LANE_EVENT, LANE_TERRITORY

TODAY = date(2026, 9, 8)


def _placer_row(
    *,
    name="LOCAL MARKET",
    permit_type="Market - With Food Prep < 5000 Sq Ft",
    permit_name=None,
    zip5="95661",
    city="Roseville",
    comments="",
    outcome="",
    inspection_date="2026-09-01T00:00:00.000Z",
    program="Retail Food",
    inspection_id="AAAAAAAA-0000-0000-0000-000000000001",
) -> dict:
    return {
        "nick": "cpch",
        "inspectionID": inspection_id,
        "inspectionDate": inspection_date,
        "score": 0,
        "inspectionType": "Retail Food Facility",
        "purpose": "Routine",
        "establishmentName": name,
        "addressLine1": "100 Main St",
        "addressLine2": None,
        "city": city,
        "state": "CA",
        "zip": f"{zip5}-1234",
        "permitID": "BBBBBBBB-0000-0000-0000-000000000001",
        "permitName": permit_name or f"{name} - PR0099001",
        "comments": comments,
        "InspectionOutcome": outcome,
        "permitType": permit_type,
        "programName": program,
    }


def _yolo_row(
    *,
    name="RIVER MARKET",
    permit_type="Retail Food Markets less than 2,000 square feet, RC1",
    zip5="95691",
    city="West Sacramento",
    comments="",
    inspection_date="2026-09-01T00:00:00.000Z",
    program="Food",
    inspection_id="CCCCCCCC-0000-0000-0000-000000000001",
) -> dict:
    return {
        "nick": "ycc",
        "inspectionID": inspection_id,
        "inspectionDate": inspection_date,
        "score": 0,
        "inspectionType": "Rec Health Routine Inspection",
        "comments": comments,
        "establishmentName": name,
        "addressLine1": "200 River Rd",
        "addressLine2": "",
        "city": city,
        "state": "CA",
        "zip": f"{zip5}-1234",
        "permitID": "DDDDDDDD-0000-0000-0000-000000000001",
        "progIdent": None,
        "INSP_PURPOSEID": "Routine",
        "permitType": permit_type,
        "programName": program,
    }


def _run(rows, config, *, fetcher=None, circuit=None):
    return asyncio.run(myhd.build_candidates(rows, config, today=TODAY, fetcher=fetcher, circuit=circuit))


# --- permit classification (real observed vocabulary, 2026-09-08) ---------


def test_placer_large_market_scores_a():
    base, excluded = myhd._placer_icp_base("Market - With Food Prep Equal To Or > 5000 Sq Ft", "COSTCO WHOLESALE")
    assert (base, excluded) == (30, False)


def test_placer_mid_market_gets_keyword_bump():
    base, excluded = myhd._placer_icp_base("Market - With Food Prep < 5000 Sq Ft", "ARCO AM/PM MARKET")
    assert (base, excluded) == (24, False)


def test_placer_mid_market_without_keyword_stays_low():
    base, excluded = myhd._placer_icp_base("Market - No Food Prep >500 - 5000 Sq Ft", "AISLE 1 #2588")
    assert (base, excluded) == (12, False)


def test_placer_tiny_market_is_excluded():
    base, excluded = myhd._placer_icp_base("Market - No Food Prep < 500 Sq Ft", "24 HOUR FITNESS")
    assert excluded is True


def test_placer_bar_and_mobile_food_are_excluded():
    assert myhd._placer_icp_base("Bar Or Lounge - No Food Prep", "CHAMPS")[1] is True
    assert myhd._placer_icp_base("Mobile Food Facility - Full Prep", "Freshly Squeezed Inc")[1] is True


def test_placer_restaurant_seat_tiers():
    assert myhd._placer_icp_base("Restaurant: 100 Or More Seats", "BIG DINER")[0] == 14
    assert myhd._placer_icp_base("Restaurant: 50 - 99 Seats", "COUNTRY GABLES CAFE")[0] == 12
    assert myhd._placer_icp_base("Restaurant: 0 - 49 Seats", "MENCHIES FROZEN YOGURT")[0] == 8


def test_yolo_bakery_and_catering():
    assert myhd._yolo_icp_base("Bakery less than 2,000 square feet, RC1", "ARIANA FOOD MARKET")[0] == 22
    assert myhd._yolo_icp_base("CATERING - YEAR PERMIT", "SOME CATERER") == (24, False)


def test_yolo_market_tiers():
    assert myhd._yolo_icp_base("Retail Food Markets 5,000+ square feet, RC1", "BIG MARKET") == (30, False)
    assert myhd._yolo_icp_base("Retail Food Markets 2,000-4,999 square feet", "MID MARKET") == (24, False)
    assert myhd._yolo_icp_base("Retail Food Markets less than 2,000 square feet, RC1", "ARIANA FOOD MARKET") == (24, False)
    assert myhd._yolo_icp_base("Retail Food Markets less than 2,000 square feet, RC1", "JOE'S CORNER STORE")[0] == 12


def test_yolo_restaurant_seat_tiers():
    assert myhd._yolo_icp_base("Restaurant 150+ seats, RC 3", "BIG PLACE")[0] == 14
    assert myhd._yolo_icp_base("Restaurant 50-149 seats, RC 3", "WOK OF FLAME")[0] == 12
    assert myhd._yolo_icp_base("Restaurant 26-49 seats, RC 2", "WINGSTOP #649")[0] == 8


def test_yolo_school_satellite_and_pool_excluded():
    assert myhd._yolo_icp_base("School / Institutional Food Service Facility- Satellite Food Distribution", "SOME ELEM SCHOOL")[1] is True
    assert myhd._yolo_icp_base("EDIBLE FOOD RECOVERY AUDIT FEE", "SOME ELEM SCHOOL")[1] is True
    assert myhd._yolo_icp_base("PUBLIC SWIMMING POOL OR SPA - YEAR PERMIT", "SOME APTS")[1] is True


# --- grouping / zip allowlist / program filter -----------------------------


def test_group_rows_drops_out_of_scope_zip_and_non_food_program():
    rows = [
        _placer_row(zip5="96143", city="Kings Beach"),  # Tahoe basin, out of scope
        _placer_row(program="Pool"),  # not Retail Food
        _placer_row(zip5="95661"),  # in scope
    ]
    groups = myhd.group_rows(rows, myhd.PLACER)
    assert len(groups) == 1


def test_group_rows_collapses_multiple_permits_under_one_facility():
    rows = [
        _yolo_row(name="ARIANA FOOD MARKET", permit_type="Bakery less than 2,000 square feet, RC1"),
        _yolo_row(name="ARIANA FOOD MARKET", permit_type="Retail Food Markets less than 2,000 square feet, RC1"),
    ]
    groups = myhd.group_rows(rows, myhd.YOLO)
    assert len(groups) == 1
    assert len(next(iter(groups.values()))) == 2


# --- candidate building: lanes ---------------------------------------------


def test_placer_chain_with_no_signal_is_parked():
    rows = [_placer_row(name="COSTCO WHOLESALE #1371", permit_type="Market - With Food Prep Equal To Or > 5000 Sq Ft")]
    candidates = _run(rows, myhd.PLACER)
    assert len(candidates) == 1
    assert candidates[0].lane == LANE_CHAIN
    assert candidates[0].pushable is False


def test_placer_independent_mid_market_with_no_signal_is_territory():
    rows = [_placer_row(name="AISLE 1 MARKET #2588", permit_type="Market - No Food Prep >500 - 5000 Sq Ft")]
    candidates = _run(rows, myhd.PLACER)
    assert len(candidates) == 1
    assert candidates[0].lane == LANE_TERRITORY
    assert candidates[0].pushable is True
    assert candidates[0].county == "Placer"
    assert candidates[0].customer_link == "PCHD:PR0099001"


def test_placer_low_icp_restaurant_with_no_signal_is_dropped():
    rows = [_placer_row(name="MENCHIES FROZEN YOGURT", permit_type="Restaurant: 0 - 49 Seats")]
    candidates = _run(rows, myhd.PLACER)
    assert candidates == []


def test_placer_rodent_comment_is_event_lane():
    rows = [
        _placer_row(
            name="AZAYAKA",
            permit_type="Restaurant: 50 - 99 Seats",
            comments="Closure due to rodent droppings observed throughout the kitchen.",
            outcome="Red Placard",
        )
    ]
    candidates = _run(rows, myhd.PLACER)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.lane == LANE_EVENT
    assert c.classification.label == "rodent"
    assert c.score.pest_signal > 0
    assert c.signal is not None
    assert c.signal.report_url == myhd.report_url(myhd.PLACER, rows[0]["inspectionID"])


def test_placer_no_header_no_email_ever_attached():
    # Placer's report PDF has no owner/phone/email at all (plan 2.6) -- the adapter
    # never fetches it, so header/email must stay unset even with a fetcher passed.
    rows = [_placer_row(name="AISLE 1 MARKET #2588", permit_type="Market - No Food Prep >500 - 5000 Sq Ft")]

    class _ExplodingFetcher:
        async def fetch_text(self, *a, **kw):
            raise AssertionError("Placer should never fetch a report PDF")

    candidates = _run(rows, myhd.PLACER, fetcher=_ExplodingFetcher())
    assert candidates[0].header is None
    assert candidates[0].email is None


def test_yolo_out_of_scope_city_is_dropped():
    rows = [_yolo_row(name="WINGSTOP #649", city="Davis", zip5="95616", permit_type="Restaurant 150+ seats, RC 3")]
    candidates = _run(rows, myhd.YOLO)
    assert candidates == []


def test_yolo_cockroach_comment_is_event_lane():
    rows = [
        _yolo_row(
            name="CRAZY D'S HOT CHICKEN",
            permit_type="Restaurant 26-49 seats, RC 2",
            comments="Several live cockroaches observed under the prep counter.",
        )
    ]
    candidates = _run(rows, myhd.YOLO)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.lane == LANE_EVENT
    assert c.classification.label == "cockroach"
    assert c.county == "Yolo"


class _StubFetcher:
    def __init__(self, text: str | None):
        self.text = text
        self.calls = 0

    async def fetch_text(self, report_url: str, pkey: str) -> str | None:
        self.calls += 1
        return self.text


def test_yolo_fetches_pdf_and_attaches_email_and_key():
    rows = [_yolo_row(name="AM/PM MINI MARKET #5731", permit_type="Retail Food Markets less than 2,000 square feet, RC1")]
    fetcher = _StubFetcher(fx.YOLO_AMPM)
    candidates = _run(rows, myhd.YOLO, fetcher=fetcher)
    assert len(candidates) == 1
    c = candidates[0]
    assert fetcher.calls == 1
    assert c.email == "reedaveampm@gmail.com"
    assert c.email_source == "yolo_pdf"
    assert c.customer_link == "YOLO:FA0002270"
    assert c.header is not None
    assert c.header.is_entity is False  # "AM/PM MINI MARKET #5731- FOOD" has no LLC/INC/CORP marker


def test_yolo_skips_pdf_fetch_when_circuit_is_blocked():
    rows = [_yolo_row(name="AM/PM MINI MARKET #5731")]
    fetcher = _StubFetcher(fx.YOLO_AMPM)
    circuit = myhd.PortalCircuit()
    circuit.trip("test block")
    candidates = _run(rows, myhd.YOLO, fetcher=fetcher, circuit=circuit)
    assert fetcher.calls == 0
    assert candidates[0].email is None
    # Falls back to the raw permitID GUID as the interim key (plan section 4).
    assert candidates[0].customer_link == f"YOLO:{rows[0]['permitID']}"


def test_yolo_low_icp_with_no_signal_is_dropped():
    rows = [_yolo_row(name="WINGSTOP #649", permit_type="Restaurant 26-49 seats, RC 2")]
    candidates = _run(rows, myhd.YOLO, fetcher=None)
    assert candidates == []


def test_stale_facility_is_dropped():
    rows = [
        _placer_row(
            name="AISLE 1 MARKET #2588",
            permit_type="Market - No Food Prep >500 - 5000 Sq Ft",
            inspection_date="2024-01-01T00:00:00.000Z",
        )
    ]
    candidates = _run(rows, myhd.PLACER)
    assert candidates == []


# --- wire format: pagination, 25-row cap, circuit breaker ------------------


def _paged_handler(pages: list[list[dict]]):
    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        body = _json.loads(request.content)
        start = body["data"]["start"]
        page_index = start // myhd.PAGE_SIZE
        page = pages[page_index] if page_index < len(pages) else []
        return httpx.Response(200, json=page)

    return handler


@pytest.mark.asyncio
async def test_search_county_pages_until_a_short_page(monkeypatch):
    monkeypatch.setattr(myhd, "MIN_INTERVAL_SECONDS", 0.0)
    full_page = [_placer_row(inspection_id=f"row-{i}") for i in range(myhd.PAGE_SIZE)]
    short_page = [_placer_row(inspection_id="row-last")]
    handler = _paged_handler([full_page, short_page])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        circuit = myhd.PortalCircuit()
        rows = await myhd.search_county(client, myhd.PLACER, circuit, date_from=date(2026, 8, 1), date_to=date(2026, 9, 8))
    assert len(rows) == myhd.PAGE_SIZE + 1
    assert circuit.blocked is False


@pytest.mark.asyncio
async def test_search_county_trips_the_circuit_on_a_403_and_keeps_prior_rows():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=[_placer_row(inspection_id=f"row-{i}") for i in range(myhd.PAGE_SIZE)])
        return httpx.Response(403, text="blocked")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        circuit = myhd.PortalCircuit()
        rows = await myhd.search_county(client, myhd.PLACER, circuit, date_from=date(2026, 8, 1), date_to=date(2026, 9, 8))
    assert len(rows) == myhd.PAGE_SIZE
    assert circuit.blocked is True


@pytest.mark.asyncio
async def test_search_county_is_a_noop_once_the_circuit_is_already_blocked():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=[])

    circuit = myhd.PortalCircuit()
    circuit.trip("already blocked")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        rows = await myhd.search_county(client, myhd.YOLO, circuit, date_from=date(2026, 8, 1), date_to=date(2026, 9, 8))
    assert rows == []
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_search_page_sends_the_documented_wire_format():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen["body"] = _json.loads(request.content)
        seen["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await myhd.search_page(client, myhd.YOLO, date_from=date(2026, 8, 1), date_to=date(2026, 9, 8), start=0)
    body = seen["body"]
    assert body["task"] == "searchInspections"
    assert body["data"]["path"] == "yolocountyeh"
    assert body["data"]["filters"] == {"date": "2026-08-01 to 2026-09-08"}
    assert body["data"]["count"] == myhd.PAGE_SIZE
    assert seen["ua"] == myhd.ag.USER_AGENT


def test_report_url_matches_the_sacramento_pattern_with_the_county_path():
    url = myhd.report_url(myhd.YOLO, "GUID-1")
    assert url == "https://inspections.myhealthdepartment.com/yolocountyeh/print/?task=getPrintable&path=yolocountyeh&pKey=GUID-1"
