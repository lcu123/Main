"""Google Places enrichment: phone normalisation, the address check that keeps a
plausible-but-wrong match out of the sheet, the per-run call ceiling, and the
circuit breaker on the responses that mean "every later call fails too"."""

from __future__ import annotations

import json

import httpx
import pytest

from fr_mcp.leads import places


def _response(
    *,
    name="AZAYAKA JAPANESE FUSION",
    phone="(916) 555-1234",
    address="6726 Stanford Ranch Rd Ste 7, Roseville, CA 95678, USA",
    website="https://azayaka.example",
    status="OPERATIONAL",
) -> dict:
    place: dict = {"displayName": {"text": name}, "formattedAddress": address}
    if phone is not None:
        place["nationalPhoneNumber"] = phone
    if website is not None:
        place["websiteUri"] = website
    if status is not None:
        place["businessStatus"] = status
    return {"places": [place]}


def _client(handler, **kw) -> tuple[places.PlacesClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return places.PlacesClient(http, lambda: "test-token", **kw), http


async def _lookup(handler, *, street="6726 Stanford Ranch Rd Ste 7", zip5="95678", **kw):
    client, http = _client(handler, **kw)
    async with http:
        hit = await client.lookup(name="AZAYAKA JAPANESE FUSION", street=street, city="Roseville", zip5=zip5)
    return hit, client


# --- phone normalisation ---------------------------------------------------


def test_phone_normalises_to_bare_ten_digits():
    assert places.normalize_phone("(916) 416-1664") == "9164161664"
    assert places.normalize_phone("+1 916-416-1664") == "9164161664"
    assert places.normalize_phone("916.416.1664") == "9164161664"


def test_phone_that_is_not_ten_digits_is_dropped():
    # FieldRoutes' customer/search phone filter is an exact 10-digit match, so a
    # short or international number is worse than nothing downstream.
    assert places.normalize_phone("555-1234") is None
    assert places.normalize_phone("+44 20 7123 4567") is None
    assert places.normalize_phone(None) is None
    assert places.normalize_phone("") is None


# --- the address check -----------------------------------------------------


def test_matching_zip_is_accepted():
    assert places.is_same_place(street="1 Main St", zip5="95678", matched_address="1 Main St, Roseville, CA 95678, USA")


def test_matching_house_number_is_accepted_when_the_zip_differs():
    # Google often returns a zip+4 or a neighbouring zip for the same building.
    assert places.is_same_place(street="6726 Stanford Ranch Rd Ste 7", zip5="95678", matched_address="6726 Stanford Ranch Rd, Rocklin, CA 95677")


def test_a_different_building_is_rejected():
    assert not places.is_same_place(street="6726 Stanford Ranch Rd", zip5="95678", matched_address="1200 Douglas Blvd, Roseville, CA 95661")


def test_a_house_number_only_matches_whole():
    # "672" must not match "6726 ..." -- that's how a neighbouring business slips in.
    assert not places.is_same_place(street="672 Main St", zip5="95999", matched_address="6726 Main St, Roseville, CA 95678")


# --- lookup ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_confirmed_match_returns_the_phone_website_and_status():
    hit, client = await _lookup(lambda r: httpx.Response(200, json=_response()))
    assert hit.phone == "9165551234"
    assert hit.website == "https://azayaka.example"
    assert hit.business_status == "OPERATIONAL"
    assert hit.permanently_closed is False
    assert client.matched == 1


@pytest.mark.asyncio
async def test_a_result_at_a_different_address_is_rejected_not_returned():
    handler = lambda r: httpx.Response(200, json=_response(address="1200 Douglas Blvd, Roseville, CA 95661"))
    hit, client = await _lookup(handler, street="6726 Stanford Ranch Rd", zip5="95678")
    assert hit is None
    assert client.rejected == 1
    assert client.matched == 0


@pytest.mark.asyncio
async def test_a_permanently_closed_business_is_still_returned_and_flagged():
    handler = lambda r: httpx.Response(200, json=_response(status="CLOSED_PERMANENTLY"))
    hit, _ = await _lookup(handler)
    assert hit.permanently_closed is True  # the row gets marked, not silently dropped


@pytest.mark.asyncio
async def test_no_results_returns_none():
    hit, client = await _lookup(lambda r: httpx.Response(200, json={"places": []}))
    assert hit is None
    assert client.calls == 1


@pytest.mark.asyncio
async def test_the_request_carries_the_bearer_token_and_field_mask():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen["auth"] = request.headers.get("authorization")
        seen["mask"] = request.headers.get("x-goog-fieldmask")
        seen["query"] = _json.loads(request.content)["textQuery"]
        return httpx.Response(200, json=_response())

    await _lookup(handler)
    assert seen["auth"] == "Bearer test-token"
    assert seen["mask"] == places.FIELD_MASK
    assert "AZAYAKA" in seen["query"] and "95678" in seen["query"]


# --- cost control ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_call_ceiling_stops_further_lookups():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_response())

    client, http = _client(handler, call_ceiling=2)
    async with http:
        for _ in range(5):
            await client.lookup(name="X", street="1 Main St", city="Roseville", zip5="95678")
    assert calls["n"] == 2
    assert client.budget_left == 0


@pytest.mark.parametrize("status", [401, 403, 429])
@pytest.mark.asyncio
async def test_an_auth_or_quota_response_blocks_the_rest_of_the_run(status):
    # Places API disabled, billing off, or quota gone: every later call fails the
    # same way, so keep paying for exactly zero of them.
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, text="denied")

    client, http = _client(handler)
    async with http:
        assert await client.lookup(name="X", street="1 Main St", city="Roseville", zip5="95678") is None
        assert client.blocked is True
        assert await client.lookup(name="Y", street="2 Main St", city="Roseville", zip5="95678") is None
    assert calls["n"] == 1
    assert str(status) in client.block_reason


@pytest.mark.asyncio
async def test_a_transport_error_is_survivable_and_not_a_block():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client, http = _client(handler)
    async with http:
        assert await client.lookup(name="X", street="1 Main St", city="Roseville", zip5="95678") is None
    assert client.blocked is False  # one flaky call shouldn't stop the others


def test_max_calls_reads_the_env_with_a_safe_default(monkeypatch):
    monkeypatch.delenv("LEADS_PLACES_MAX_CALLS", raising=False)
    assert places.max_calls() == places.DEFAULT_MAX_CALLS
    monkeypatch.setenv("LEADS_PLACES_MAX_CALLS", "7")
    assert places.max_calls() == 7
    monkeypatch.setenv("LEADS_PLACES_MAX_CALLS", "not-a-number")
    assert places.max_calls() == places.DEFAULT_MAX_CALLS


def test_the_token_provider_reads_the_inline_credential_railway_actually_sets(monkeypatch):
    # Railway's variables UI has no secret-file mechanism, so the live deployment
    # sets GOOGLE_SERVICE_ACCOUNT_JSON inline and never the _PATH form. Reading
    # only _PATH here raised PlacesError on every scheduled run.
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON_PATH", raising=False)
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", '{"type": "service_account"}')
    seen = {}

    class _Creds:
        valid = True
        token = "tok"

        @classmethod
        def from_service_account_info(cls, info, scopes):
            seen["info"], seen["scopes"] = info, scopes
            return cls()

    monkeypatch.setattr(
        "google.oauth2.service_account.Credentials", _Creds, raising=False
    )
    assert places.service_account_token_provider()() == "tok"
    assert seen["info"] == {"type": "service_account"}
    assert seen["scopes"] == list(places.SCOPES)


def test_a_malformed_inline_credential_is_a_places_error_not_a_json_error(monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON_PATH", raising=False)
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "{not json")
    with pytest.raises(places.PlacesError):
        places.service_account_token_provider()


def test_no_credential_at_all_names_both_variables(monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON_PATH", raising=False)
    with pytest.raises(places.PlacesError) as exc:
        places.service_account_token_provider()
    assert "GOOGLE_SERVICE_ACCOUNT_JSON" in str(exc.value)
    assert "GOOGLE_SERVICE_ACCOUNT_JSON_PATH" in str(exc.value)


# --- discovery sweep -----------------------------------------------------

RECT = places.Rect(south=38.25, west=-121.95, north=39.15, east=-121.00)


def _place(i: int, *, primary_type="manufacturer", status="OPERATIONAL") -> dict:
    return {
        "id": f"P{i}",
        "displayName": {"text": f"Plant {i}"},
        "formattedAddress": f"{100 + i} Mill Rd, Sacramento, CA 95814, USA",
        "primaryType": primary_type,
        "nationalPhoneNumber": "(916) 555-0100",
        "websiteUri": "https://example.test",
        "businessStatus": status,
        "location": {"latitude": 38.58, "longitude": -121.49},
    }


def _pager(pages: list[list[dict]]):
    """A handler serving `pages` in order, handing out a nextPageToken until the
    last one -- the shape the real API uses."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        index = int(body.get("pageToken", "0"))
        page = pages[index] if index < len(pages) else []
        out: dict = {"places": page}
        if index + 1 < len(pages):
            out["nextPageToken"] = str(index + 1)
        return httpx.Response(200, json=out)

    return handler, calls


@pytest.mark.asyncio
async def test_a_sweep_pages_until_google_stops_offering_a_token():
    handler, calls = _pager([[_place(i) for i in range(20)], [_place(i) for i in range(20, 33)]])
    client, http = _client(handler)
    async with http:
        rows, saturated = await client.search_text("food processing plant", RECT)
    assert len(rows) == 33
    assert saturated is False  # 33 < the 60-result ceiling
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_sixty_results_counts_as_saturated_because_that_is_the_hard_ceiling():
    """Measured live: three pages of 20 and no token after, whether or not the
    area is exhausted. A query that reaches it has more to give."""
    handler, _ = _pager([[_place(i + p * 20) for i in range(20)] for p in range(3)])
    client, http = _client(handler)
    async with http:
        rows, saturated = await client.search_text("bakery", RECT)
    assert len(rows) == places.SWEEP_CAP == 60
    assert saturated is True


@pytest.mark.asyncio
async def test_never_asks_for_a_fourth_page_even_if_a_token_is_offered():
    handler, calls = _pager([[_place(i + p * 20) for i in range(20)] for p in range(6)])
    client, http = _client(handler)
    async with http:
        await client.search_text("bakery", RECT)
    assert len(calls) == places.SWEEP_MAX_PAGES == 3


def test_a_rectangle_splits_into_four_quadrants_that_tile_it():
    quads = RECT.quadrants()
    assert len(quads) == 4
    assert min(q.south for q in quads) == RECT.south
    assert max(q.north for q in quads) == RECT.north
    assert min(q.west for q in quads) == RECT.west
    assert max(q.east for q in quads) == RECT.east


@pytest.mark.asyncio
async def test_a_saturated_query_is_re_run_over_the_quadrants():
    saturating = [[_place(i + p * 20) for i in range(20)] for p in range(3)]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        index = int(body.get("pageToken", "0"))
        out: dict = {"places": saturating[index]}
        if index + 1 < 3:
            out["nextPageToken"] = str(index + 1)
        return httpx.Response(200, json=out)

    client, http = _client(handler)
    client.call_ceiling = 500
    async with http:
        found = await client.sweep(["bakery"], RECT, max_depth=1)
    # 3 pages for the whole rectangle, then 3 for each of four quadrants.
    assert client.calls == 3 + 4 * 3
    assert len(found) == 60  # same fixture everywhere, deduplicated by place id


@pytest.mark.asyncio
async def test_the_sweep_stops_dead_when_the_budget_runs_out():
    """It is the only billed step in the pipeline; a runaway recursion here is
    the one that costs real money."""
    handler, _ = _pager([[_place(i) for i in range(20)]])
    client, http = _client(handler)
    client.call_ceiling = 5
    async with http:
        await client.sweep(list(places.SWEEP_QUERIES), RECT)
    assert client.calls <= 5


@pytest.mark.asyncio
async def test_a_typed_pass_asks_google_to_filter_rather_than_merely_rank():
    handler, calls = _pager([[_place(1)]])
    client, http = _client(handler)
    async with http:
        await client.sweep([], RECT, typed_queries=[("manufacturer", "food")])
    assert calls[0]["includedType"] == "manufacturer"
    assert calls[0]["strictTypeFiltering"] is True


@pytest.mark.asyncio
async def test_the_rectangle_is_sent_as_low_south_west_and_high_north_east():
    handler, calls = _pager([[_place(1)]])
    client, http = _client(handler)
    async with http:
        await client.search_text("bakery", RECT)
    rect = calls[0]["locationRestriction"]["rectangle"]
    assert rect["low"] == {"latitude": 38.25, "longitude": -121.95}
    assert rect["high"] == {"latitude": 39.15, "longitude": -121.0}


def test_the_address_is_split_without_paying_for_a_components_field():
    addr = "1802 C St, Sacramento, CA 95811, USA"
    assert places.street_from_address(addr) == "1802 C St"
    assert places.city_from_address(addr) == "Sacramento"
    assert places.zip_from_address(addr) == "95811"
    assert places.zip_from_address("no address here") == ""
    # A zip+4 and a street number that looks like a zip must not confuse it.
    assert places.zip_from_address("95814 Main St, Elk Grove, CA 95624-1234, USA") == "95624"
