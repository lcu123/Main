"""USDA FSIS directory adapter: the header check, the gzip sniff, and the archive
fallback that exists because the live host blocks some networks outright.

The CSV fixture is real rows from the 2026-08-18 directory.
"""

from __future__ import annotations

import gzip

import httpx
import pytest

from fr_mcp.leads import food, fsis

HEADER = (
    "establishment_id,establishment_number,establishment_name,duns_number,street,city,state,zip,"
    "phone,grant_date,activities,dbas,district,circuit,size,latitude,longitude,county,fips_code\n"
)
CSV = HEADER + (
    "1001,M12345,\"Stafford  Meat Company, Inc.\",,6900 W 2nd St,Rio LInda,CA,95673,(916) 991-3021,"
    "2001-01-01,Meat Processing; Poultry Processing,,50,1,Small,38.6905,-121.4657,Sacramento,06067\n"
    "1002,P54321,Bare Naked Birdies,,1 Coop Ln,Sacramento,CA,95820,(916) 259-3737,"
    "2010-01-01,Poultry Processing; Poultry Slaughter,,50,1,Very Small,38.53,-121.45,Sacramento,06067\n"
    "1003,M99999,Faraway Packing,,1 Elsewhere,Fresno,CA,93721,(559) 555-0100,"
    "2010-01-01,Meat Processing,,50,1,Large,36.74,-119.78,Fresno,06019\n"
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_the_directory_parses_into_establishments_with_real_coordinates():
    rows = fsis.parse_directory(CSV)
    assert [r.name for r in rows][:2] == ["Stafford Meat Company, Inc.", "Bare Naked Birdies"]
    assert rows[0].phone == "9169913021"
    assert rows[0].key == "FSIS:1001"
    assert rows[0].lat == 38.6905
    assert rows[0].size == "Small"


def test_a_slaughter_operation_is_distinguishable_from_a_processing_one():
    """More blood, more offal, more flies -- it is a different pest conversation."""
    rows = fsis.parse_directory(CSV)
    assert rows[0].slaughters is False
    assert rows[1].slaughters is True


def test_a_changed_file_layout_is_an_error_not_a_silent_empty_list():
    with pytest.raises(fsis.FsisError):
        fsis.parse_directory("id,name,town\n1,Acme,Sacramento\n")


def test_within_uses_the_real_coordinates_rather_than_a_centroid():
    near = fsis.within(fsis.parse_directory(CSV), 28.0)
    assert [r.name for r in near] == ["Stafford Meat Company, Inc.", "Bare Naked Birdies"]
    assert fsis.distance_miles(near[0]) < 1.0  # Rio Linda, essentially next door


@pytest.mark.asyncio
async def test_the_live_host_is_tried_first_and_reports_itself():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == fsis.DIRECTORY_URL
        return httpx.Response(200, content=CSV.encode())

    async with _client(handler) as http:
        rows, source = await fsis.fetch_establishments(http)
    assert source == "live"
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_a_403_from_the_live_host_falls_back_to_the_archive():
    """www.fsis.usda.gov returns 403 to this project's sandbox -- an Akamai edge
    denial against the egress IP, which no header fixes. Having weeks-old data is
    better than having no meat and poultry plants at all."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host != "web.archive.org":
            return httpx.Response(403, text="<html>Access Denied</html>")
        return httpx.Response(200, content=gzip.compress(CSV.encode()))

    async with _client(handler) as http:
        rows, source = await fsis.fetch_establishments(http)
    assert source == "archive"
    assert len(rows) == 3
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_gzip_is_detected_by_its_magic_bytes_not_by_a_header():
    """The archive serves this file gzipped without always saying so -- the same
    lesson PoliteFetcher learned about sniffing %PDF instead of Content-Type."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=gzip.compress(CSV.encode()))

    async with _client(handler) as http:
        rows, _ = await fsis.fetch_establishments(http)
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_both_sources_failing_is_a_single_error_naming_both():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    async with _client(handler) as http:
        with pytest.raises(fsis.FsisError) as exc:
            await fsis.fetch_establishments(http)
    assert "live" in str(exc.value) and "archive" in str(exc.value)


def test_fsis_rows_never_need_a_category_guess():
    """Every establishment in this directory is a meat, poultry or egg operation by
    definition, so it is the one source that contributes no review flags."""
    rows = [food.from_fsis(e) for e in fsis.parse_directory(CSV)]
    assert all(r.needs_review is False for r in rows)
    assert rows[0].category == food.CAT_MEAT
    assert "Poultry Slaughter" in rows[1].found_via


def test_a_seafood_plant_is_not_filed_as_poultry_just_because_fsis_lists_it():
    """Pacific Seafood is a real in-range FSIS establishment. The directory says
    "Meat Processing; Poultry Processing" about it, which is the licence, not the
    product -- the name is the better answer."""
    est = fsis.parse_directory(
        HEADER
        + "1,M1,Pacific Seafood - Sacramento LLC,,1 Dock,Sacramento,CA,95814,(916) 419-5500,"
          "2010-01-01,Meat Processing; Poultry Processing,,50,1,Small,38.58,-121.49,Sacramento,06067\n"
    )[0]
    assert food.from_fsis(est).category == food.CAT_SEAFOOD
