"""Header parsing against real report text, plus PoliteFetcher's caching,
spacing, and block-on-non-200 behaviour against a mock transport."""

from __future__ import annotations

import time

import httpx
import pytest

import leads_fixtures as fx
from fr_mcp.leads.reports import PoliteFetcher, parse_header, parse_report, parse_yolo_header


def test_natomas_header_owner_entity_and_phone():
    h = parse_header(fx.NATOMAS)
    assert h.facility_id == "FA0044262"
    assert h.permit_id == "PR0091055"
    assert h.owner == "SUPER SHOT RAJPURA INC"
    assert h.is_entity is True
    assert h.phone == "9164161664"


def test_seapot_header_has_no_phone():
    # Verified live: SEAPOT's header phone field is blank. A naive regex that
    # searches the whole document instead picks up the inspector's own
    # "Insp Phone(916) 862-3400" line further down the same report.
    h = parse_header(fx.SEAPOT)
    assert h.facility_id == "FA0046536"
    assert h.phone is None


def test_buds_buffet_owner_is_a_person_not_an_entity():
    h = parse_header(fx.BUDS_NO_VERMIN)
    assert h.owner == "HAROON KHAN"
    assert h.is_entity is False
    assert h.phone == "5103763395"


def test_parse_header_returns_none_without_a_facility_id():
    assert parse_header("no header here at all") is None


def test_parse_report_combines_header_and_classification():
    r = parse_report(fx.NATOMAS)
    assert r.header.facility_id == "FA0044262"
    assert r.classification.label == "rodent"
    assert r.text == fx.NATOMAS


# --- Yolo header (different template from Sacramento's) ----------------


def test_yolo_header_parses_facility_id_email_and_owner():
    h = parse_yolo_header(fx.YOLO_AMPM)
    assert h.facility_id == "FA0002270"
    assert h.permit_id == "PR0022221"
    assert h.owner == "AM/PM MINI MARKET #5731- FOOD"
    assert h.email == "reedaveampm@gmail.com"
    assert h.phone is None  # blank in the real report


def test_yolo_header_returns_none_without_a_facility_id():
    assert parse_yolo_header("no header here at all") is None


# --- PoliteFetcher -----------------------------------------------------


def _pdf_bytes() -> bytes:
    """A real, minimal, valid single-page PDF -- `fetch_text` runs `extract_text`
    on whatever it downloads, so a fake byte string would raise inside pypdf
    rather than exercising the caching/spacing/blocking behaviour these tests
    are actually about."""
    import io

    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_fetch_caches_to_disk_and_skips_the_network_on_a_hit(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=_pdf_bytes(), headers={"content-type": "application/pdf"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = PoliteFetcher(tmp_path, client)
        cached_path = tmp_path / "PKEY-1.pdf"
        assert not cached_path.exists()
        text1 = await fetcher.fetch_text("https://example.test/report", "PKEY-1")
        assert cached_path.exists()
        assert calls["n"] == 1
        text2 = await fetcher.fetch_text("https://example.test/report", "PKEY-1")
        assert text2 == text1
        assert calls["n"] == 1  # second call was a cache hit, no new request
        assert fetcher.fetched == 1
        assert fetcher.cache_hits == 1


@pytest.mark.asyncio
async def test_a_non_200_response_blocks_the_rest_of_the_run(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="blocked")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = PoliteFetcher(tmp_path, client)
        result = await fetcher.fetch_text("https://example.test/report", "PKEY-A")
        assert result is None
        assert fetcher.blocked is True
        # A second, different pKey must not even try the network once blocked.
        result2 = await fetcher.fetch_text("https://example.test/report", "PKEY-B")
        assert result2 is None
        assert not (tmp_path / "PKEY-B.pdf").exists()


@pytest.mark.asyncio
async def test_fetches_are_spaced_at_least_the_minimum_interval(tmp_path, monkeypatch):
    import fr_mcp.leads.reports as reports_mod

    monkeypatch.setattr(reports_mod, "MIN_INTERVAL_SECONDS", 0.05)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_pdf_bytes(), headers={"content-type": "application/pdf"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetcher = PoliteFetcher(tmp_path, client)
        start = time.monotonic()
        await fetcher.fetch_text("https://example.test/a", "PKEY-A")
        await fetcher.fetch_text("https://example.test/b", "PKEY-B")
        elapsed = time.monotonic() - start
        assert elapsed >= 0.04  # allow a little scheduling slack under 0.05s
