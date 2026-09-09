"""CDFA Market Enforcement adapter: the WebForms token round-trip, the CSV shape
its POST actually returns, and the two ways this source lies quietly -- a rejected
postback that still arrives as text, and a mailing address that is a PO Box.

The CSV fixtures are real rows from a live 2026-09-09 pull, including the phone
formatting inconsistency (parenthesised on most rows, bare digits on others) that
the normaliser exists for.
"""

from __future__ import annotations

import httpx
import pytest

from fr_mcp.leads import cdfa

FORM_HTML = """
<html><body><form method="post" action="./licenseelist.aspx">
<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="STATE-ABC" />
<input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" value="1C021C32" />
<input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="EV-XYZ" />
<input type="checkbox" name="cbProcessor" /><select name="ddlCommodityType">
<option value="0">All Commodities</option></select>
<input type="submit" name="btnGenerateList" value="Generate List" />
</form></body></html>
"""

CSV_BODY = (
    "LicenseNum,Name,MailingAddress,MailingCity,MailingState,MailingZIP,Phone,ExpirationDate\n"
    "1014,Blue Diamond Growers,1802 C Street,SACRAMENTO,CA,95811,(916) 442-0771,2027-06-30\n"
    "2211,Farmers Rice Cooperative,P.O. Box 15223,SACRAMENTO,CA,95851,9165654324,2027-06-30\n"
    "3300,Far Away Packing,12 Elsewhere Ave,FRESNO,CA,93721,(559) 555-0100,2027-06-30\n"
    "4400, ,,,,,,\n"
)

POSTBACK_500 = (
    "<!DOCTYPE html><html><head><title>Invalid postback or callback argument.</title>"
    "</head><body>Event validation is enabled</body></html>"
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_the_post_echoes_the_tokens_from_the_same_get():
    """A __VIEWSTATE pair is only valid for the render it came from, so the GET and
    the POST have to be one operation. If this ever regresses to a cached token the
    live site answers HTTP 500, not a friendly error."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=FORM_HTML)
        seen["body"] = request.content.decode()
        seen["referer"] = request.headers.get("Referer")
        return httpx.Response(200, text=CSV_BODY)

    async with _client(handler) as http:
        rows = await cdfa.fetch_licensees(http)

    assert "__VIEWSTATE=STATE-ABC" in seen["body"]
    assert "__EVENTVALIDATION=EV-XYZ" in seen["body"]
    # An empty commodity value is rejected by event validation -- it must be the
    # rendered "All Commodities" option, by its real value.
    assert "ddlCommodityType=0" in seen["body"]
    assert "btnGenerateList=Generate+List" in seen["body"]
    assert seen["referer"] == cdfa.LIST_URL
    assert [r.name for r in rows] == [
        "Blue Diamond Growers",
        "Farmers Rice Cooperative",
        "Far Away Packing",
    ]


@pytest.mark.asyncio
async def test_every_category_checkbox_is_sent_by_default():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=FORM_HTML)
        seen["body"] = request.content.decode()
        return httpx.Response(200, text=CSV_BODY)

    async with _client(handler) as http:
        await cdfa.fetch_licensees(http)
    for box in cdfa.CATEGORIES:
        assert f"{box}=on" in seen["body"]


@pytest.mark.asyncio
async def test_a_rejected_postback_is_an_error_not_an_empty_list():
    """WebForms answers a bad token with an HTML error page. Parsing that as a CSV
    yields zero rows and a run that looks like "CDFA had nothing today"."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=FORM_HTML)
        return httpx.Response(200, text=POSTBACK_500)

    async with _client(handler) as http:
        with pytest.raises(cdfa.CdfaError) as exc:
            await cdfa.fetch_licensees(http)
    assert "other than the licence CSV" in str(exc.value)


@pytest.mark.asyncio
async def test_a_form_without_tokens_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><body>Access denied</body></html>")

    async with _client(handler) as http:
        with pytest.raises(cdfa.CdfaError):
            await cdfa.fetch_licensees(http)


def test_phone_normalisation_covers_both_formats_the_registry_uses():
    rows = cdfa.parse_licensees(CSV_BODY)
    assert rows[0].phone == "9164420771"  # "(916) 442-0771"
    assert rows[1].phone == "9165654324"  # already bare digits
    assert cdfa.normalize_phone("1 (916) 442-0771") == "9164420771"
    assert cdfa.normalize_phone("916-442") == ""


def test_a_po_box_row_is_marked_rather_than_dropped():
    """The phone is the point of this source, and a PO Box row still has one. But
    its distance is the distance to a post office, so the sheet has to be able to
    say the address is not a site."""
    rows = cdfa.parse_licensees(CSV_BODY)
    assert rows[0].address_is_mailbox is False
    assert rows[1].address_is_mailbox is True


def test_blank_rows_are_skipped():
    assert len(cdfa.parse_licensees(CSV_BODY)) == 3


def test_the_key_is_the_licence_number_so_a_renewal_is_not_a_new_lead():
    rows = cdfa.parse_licensees(CSV_BODY)
    assert rows[0].key == "CDFA:1014"


def test_within_keeps_local_rows_and_drops_the_rest():
    rows = cdfa.parse_licensees(CSV_BODY)
    near = cdfa.within(rows, 28.0)
    assert [r.name for r in near] == ["Blue Diamond Growers", "Farmers Rice Cooperative"]
    # Fresno is outside the 45-mile gazetteer clip on both zip and city, so it
    # resolves to None -- which must drop the row, not keep it as "distance unknown".
    assert cdfa.distance_miles(rows[2]) is None


def test_a_po_box_zip_falls_back_to_the_city_rather_than_vanishing():
    """95851 is a PO-Box-only zip with no Census ZCTA. Locating on the zip alone
    would drop it, and with it most of the 1,409 PO Box rows in the registry."""
    rows = cdfa.parse_licensees(CSV_BODY)
    miles, source = cdfa.locate(rows[1])
    assert source == "city"
    assert miles is not None and miles < 28
    # A street-addressed row still prefers the finer answer.
    assert cdfa.locate(rows[0])[1] == "zip"
