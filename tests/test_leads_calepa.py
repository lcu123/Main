"""CalEPA site-portal adapter: the two-export join, the NAICS prefix syntax that
is the only way to filter this source, and the two ways it misleads -- a regulator's
switchboard sitting in the phone column, and a wrong bounding box that answers 200
with nothing.

Fixtures are shaped from a live 2026-09-09 pull, including the real CUPA District
row (Sacramento County EMD's (916) 875-8550, which appears on every Sacramento site
and is emphatically not the business's line).
"""

from __future__ import annotations

import io
import zipfile
from urllib.parse import parse_qsl, urlparse

import httpx
import pytest

from fr_mcp.leads import calepa

SITE_CSV = (
    "SiteID,SiteName,Address,City,ZIP,Latitude,Longitude,EnviroscreenScore,\n"
    "1001,BLUE DIAMOND GROWERS,1802 C ST,SACRAMENTO,95811,38.584903,-121.494400,>90 - 100,\n"
    "1002,MARY ANN'S BAKING CO,4010 SEAPORT BLVD,WEST SACRAMENTO,95691,38.476352,-121.550000,80-90,\n"
    "1003,NO CONTACT COLD STORAGE,1 CHILL WAY,SACRAMENTO,95815,38.610000,-121.450000,70-80,\n"
)

AFFIL_CSV = (
    "SiteID,FACILITY_NAME,AFFIL_TYPE_DESC,ENTITY_NAME,ENTITY_TITLE,ADDRESS,CITY,STATE,COUNTRY,ZIP_CODE,PHONE,\n"
    "1001,BLUE DIAMOND GROWERS,CUPA District,Sacramento County EMD,,,,CA,,95670,(916) 875-8550,\n"
    "1001,BLUE DIAMOND GROWERS,Legal Owner,Blue Diamond Growers,,,,CA,,95811,(916) 442-0771,\n"
    "1001,BLUE DIAMOND GROWERS,Operator,Blue Diamond Growers,,,,CA,,95811,(916) 446-8500,\n"
    "1002,MARY ANN'S BAKING CO,CUPA District,Sacramento County EMD,,,,CA,,95670,(916) 875-8550,\n"
    "1002,MARY ANN'S BAKING CO,Legal Owner,Mary Ann's Baking Co,,,,CA,,95691,(916) 681-7444,\n"
    "1003,NO CONTACT COLD STORAGE,CUPA District,Sacramento County EMD,,,,CA,,95670,(916) 875-8550,\n"
    "1003,NO CONTACT COLD STORAGE,Document Preparer,Some Consultant Inc,,,,CA,,95814,(916) 555-0000,\n"
)

BBOX = (-121.95, 38.25, -121.00, 39.15)


def _zip_of(name: str, body: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, body)
    return buf.getvalue()


def _handler(*, site=SITE_CSV, affil=AFFIL_CSV, record=None):
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(parse_qsl(urlparse(str(request.url)).query))
        if record is not None:
            record.append(request)
        if params.get("exportTypeId") == calepa.EXPORT_SITE:
            return httpx.Response(200, content=_zip_of("Site.csv", site))
        return httpx.Response(200, content=_zip_of("Affils.csv", affil))

    return handler


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_the_phone_comes_from_the_operator_never_from_the_regulator():
    """Every Sacramento site carries the county environmental health department as
    a "CUPA District" affiliation. Counting those is what makes this source look
    like it has 100% phone coverage -- and puts a regulator's switchboard in front
    of a rep."""
    async with _client(_handler()) as http:
        sites = {s.name: s for s in await calepa.fetch_sites(http, bbox=BBOX, prefixes=["311"])}

    blue = sites["BLUE DIAMOND GROWERS"]
    assert blue.phone == "9164468500"  # the Operator, preferred over the Legal Owner
    assert blue.phone_role == "Operator"
    assert sites["MARY ANN'S BAKING CO"].phone == "9166817444"  # only a Legal Owner exists
    # A site whose only phones are a regulator and a consultant gets no phone at all,
    # rather than a wrong one.
    assert sites["NO CONTACT COLD STORAGE"].phone == ""
    assert sites["NO CONTACT COLD STORAGE"].phone_role == ""


@pytest.mark.asyncio
async def test_site_identity_and_coordinates_come_from_the_site_export():
    async with _client(_handler()) as http:
        sites = await calepa.fetch_sites(http, bbox=BBOX, prefixes=["311"])
    blue = next(s for s in sites if s.site_id == "1001")
    assert (blue.address, blue.city, blue.zip5) == ("1802 C ST", "Sacramento", "95811")
    assert blue.lat == 38.584903 and blue.lng == -121.494400
    assert blue.key == "CALEPA:1001"
    assert blue.naics_prefix == "311"


@pytest.mark.asyncio
async def test_the_bounding_box_is_four_repeated_degree_params_in_lon_lat_order():
    seen: list[httpx.Request] = []
    async with _client(_handler(record=seen)) as http:
        await calepa.fetch_sites(http, bbox=BBOX, prefixes=["311"])
    q = parse_qsl(urlparse(str(seen[0].url)).query)
    assert [v for k, v in q if k == "boundingBox"] == ["-121.95", "38.25", "-121.0", "39.15"]


@pytest.mark.asyncio
async def test_naics_is_a_prefix_match_folded_into_the_free_text_term():
    """There is no NAICS entry in /filter/sitefilters, so anyone looking there
    concludes this source cannot be filtered. It can -- through `term`."""
    seen: list[httpx.Request] = []
    async with _client(_handler(record=seen)) as http:
        await calepa.fetch_sites(http, bbox=BBOX, prefixes=["3118"])
    q = dict(parse_qsl(urlparse(str(seen[0].url)).query))
    assert q["term"] == 'naics_code:"3118"'


@pytest.mark.asyncio
async def test_a_prefix_that_returns_nothing_costs_only_one_request():
    """The Affiliations export is only worth fetching if the Site export found
    something -- otherwise every empty prefix doubles its own cost for nothing."""
    seen: list[httpx.Request] = []
    header_only = "SiteID,SiteName,Address,City,ZIP,Latitude,Longitude,EnviroscreenScore,\n"
    async with _client(_handler(site=header_only, record=seen)) as http:
        sites = await calepa.fetch_sites(http, bbox=BBOX, prefixes=["311"], require_rows=False)
    assert sites == []
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_zero_rows_everywhere_is_an_error_because_a_bad_bbox_looks_like_this():
    """A bounding box in Web Mercator metres instead of degrees returns HTTP 200
    with an empty result -- no error at all. Silence has to be loud here."""
    header_only = "SiteID,SiteName,Address,City,ZIP,Latitude,Longitude,EnviroscreenScore,\n"
    async with _client(_handler(site=header_only)) as http:
        with pytest.raises(calepa.CalEpaError) as exc:
            await calepa.fetch_sites(http, bbox=BBOX, prefixes=["311", "3121"])
    assert "wrong projection" in str(exc.value)


@pytest.mark.asyncio
async def test_a_site_found_under_two_prefixes_is_kept_once():
    async with _client(_handler()) as http:
        sites = await calepa.fetch_sites(http, bbox=BBOX, prefixes=["311", "3121"])
    assert len(sites) == len({s.site_id for s in sites}) == 3


@pytest.mark.asyncio
async def test_a_non_zip_body_is_an_error_not_a_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>Service Unavailable</html>")

    async with _client(handler) as http:
        with pytest.raises(calepa.CalEpaError) as exc:
            await calepa.fetch_sites(http, bbox=BBOX, prefixes=["311"])
    assert "not a zip" in str(exc.value)


@pytest.mark.asyncio
async def test_a_renamed_export_column_is_an_error_rather_than_silent_blanks():
    """This is an undocumented private API; a schema change is expected. What is
    not acceptable is every row quietly coming back with no phone."""
    renamed = AFFIL_CSV.replace("PHONE", "PHONE_NUMBER")
    async with _client(_handler(affil=renamed)) as http:
        with pytest.raises(calepa.CalEpaError) as exc:
            await calepa.fetch_sites(http, bbox=BBOX, prefixes=["311"])
    assert "export schema changed" in str(exc.value)


def test_a_bbox_of_the_wrong_length_is_refused_before_any_request():
    with pytest.raises(calepa.CalEpaError):
        calepa.criteria((1.0, 2.0))
