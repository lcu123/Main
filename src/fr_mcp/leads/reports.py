"""Fetch and parse a Sacramento County inspection report PDF.

The PDF is the only place the owner's name and phone number live (the ArcGIS
feed doesn't carry them), and it's the source for the pest narrative that
`classify.py` reads. Every page repeats a fixed-label header (plan section
2.5): "Owners Name<X>Est Name<Y>", "City<X>Address<Y> Zip<Z> Phone<P>",
"FA<id> Permit ID<PR...> Purpose<...>".

Politeness: callers are responsible for spacing calls (`MIN_INTERVAL_SECONDS`
documents the rule; `PoliteFetcher` enforces it) and for treating a 403/block
as "stop fetching for this run, not fatal" -- see fr_push.py's use of this
module.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .arcgis import USER_AGENT
from .classify import Classification, classify_report

MIN_INTERVAL_SECONDS = 2.0

_OWNER_RE = re.compile(r"Owners Name(.*?)Est Name", re.S)
_EST_NAME_RE = re.compile(r"Est Name(.*?)(?:City|\n)", re.S)
_CITY_RE = re.compile(r"City(.*?)Address", re.S)
_ADDRESS_RE = re.compile(r"Address(.*?)Zip", re.S)
_ZIP_RE = re.compile(r"Zip(\d{5}(?:-\d{4})?)")
_PHONE_RE = re.compile(r"Phone\(?(\d{3})\)?[\s.\-]?(\d{3})[\s.\-]?(\d{4})")
_FA_PERMIT_RE = re.compile(r"\bFA(FA\d+)\s*Permit ID(PR\d+)")
_ENTITY_RE = re.compile(r"\b(LLC|L\.L\.C\.|INC\.?|INCORPORATED|CORP\.?|CORPORATION|LP|L\.P\.)\b", re.I)

# Yolo County's report template is a different layout entirely (single page, no
# per-violation blocks the way Sacramento's is): "Permit HolderX Email addressY
# Phone(Z) Facility IDFA... PR IDPR...". Verified live 2026-09-08 (AM/PM MINI
# MARKET #5731, West Sacramento): the facility ID here is NOT doubled the way
# Sacramento's "FAFA..." is -- it's a single "FA" label plus an "FA0002270" value.
_YOLO_PERMIT_HOLDER_RE = re.compile(r"Permit Holder(.*?)Email address", re.S)
_YOLO_EMAIL_RE = re.compile(r"Email address(\S+@\S+?)\s+Phone", re.S)
_YOLO_PIC_EMAIL_RE = re.compile(r"PIC Email(\S+@\S+?)\s+Accepted By", re.S)
_YOLO_PHONE_RE = re.compile(r"Phone\(?(\d{3})\)?[\s.\-]?(\d{3})[\s.\-]?(\d{4})")
_YOLO_FACILITY_ID_RE = re.compile(r"Facility ID(FA\d+)\s*PR ID(PR\d+)")


@dataclass(frozen=True)
class ReportHeader:
    owner: str
    is_entity: bool
    facility_id: str
    permit_id: str
    phone: str | None  # 10 digits, no punctuation; None if the report doesn't have one


@dataclass(frozen=True)
class ParsedReport:
    header: ReportHeader | None
    classification: Classification
    text: str


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def is_entity_name(owner: str) -> bool:
    """Shared with myhd.py, which has its own YoloHeader (no is_entity field of
    its own) and needs the same LLC/INC/CORP detection to normalise into a
    ReportHeader."""
    return bool(_ENTITY_RE.search(owner))


def parse_header(text: str) -> ReportHeader | None:
    """The report repeats this header on every page, but so does an "Inspector
    ... Phone(...)" line further down each page (confirmed live, SEAPOT report:
    "Insp Phone(916) 862-3400" is the inspector's own number, not the facility's).
    Owner/phone only ever appear in the block *before* the facility's own
    "FA<id> Permit ID<PR...>" marker, so every regex here searches that slice."""
    fa_m = _FA_PERMIT_RE.search(text)
    if not fa_m:
        return None
    header = text[: fa_m.start()]
    owner_m = _OWNER_RE.search(header)
    owner = _clean(owner_m.group(1)) if owner_m else ""
    phone = None
    phone_m = _PHONE_RE.search(header)
    if phone_m:
        phone = "".join(phone_m.groups())
    return ReportHeader(
        owner=owner,
        is_entity=bool(_ENTITY_RE.search(owner)),
        facility_id=fa_m.group(1),
        permit_id=fa_m.group(2),
        phone=phone,
    )


@dataclass(frozen=True)
class YoloHeader:
    """Yolo's report has no per-violation VERMIN block the way Sacramento's does --
    the pest signal for a Yolo row comes from the portal search row's own `comments`
    field (classify.classify_text applied directly, no heading-based extraction
    needed). This header exists only to pull the facility ID, owner and contact
    email/phone the search row doesn't carry."""

    facility_id: str
    permit_id: str
    owner: str
    email: str | None  # Permit Holder's email; falls back to the person-in-charge's
    phone: str | None


def parse_yolo_header(text: str) -> YoloHeader | None:
    fa_m = _YOLO_FACILITY_ID_RE.search(text)
    if not fa_m:
        return None
    owner_m = _YOLO_PERMIT_HOLDER_RE.search(text)
    email_m = _YOLO_EMAIL_RE.search(text) or _YOLO_PIC_EMAIL_RE.search(text)
    phone_m = _YOLO_PHONE_RE.search(text)
    return YoloHeader(
        facility_id=fa_m.group(1),
        permit_id=fa_m.group(2),
        owner=_clean(owner_m.group(1)) if owner_m else "",
        email=email_m.group(1).strip() if email_m else None,
        phone="".join(phone_m.groups()) if phone_m else None,
    )


def parse_report(text: str) -> ParsedReport:
    return ParsedReport(header=parse_header(text), classification=classify_report(text), text=text)


def extract_text(pdf_bytes: bytes) -> str:
    import io

    import pypdf  # imported lazily: only report-fetching paths need it

    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


class PoliteFetcher:
    """Fetches inspection report PDFs at most one every `MIN_INTERVAL_SECONDS`,
    caches every fetch to disk by pKey forever (the report never changes once
    published), and stops issuing new requests for the rest of the run the
    moment the portal answers with anything but 200 -- a 403 or a captcha
    redirect means "back off", not "retry harder"."""

    def __init__(self, cache_dir: Path, client: httpx.AsyncClient | None = None):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._client = client
        self._owns_client = client is None
        self._last_fetch = 0.0
        self.blocked = False
        self.fetched = 0
        self.cache_hits = 0

    async def __aenter__(self) -> "PoliteFetcher":
        if self._owns_client:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    def _cache_path(self, pkey: str) -> Path:
        return self.cache_dir / f"{pkey}.pdf"

    async def fetch_text(self, report_url: str, pkey: str) -> str | None:
        """Text of the report, or None if it's unavailable (cached-miss and the
        portal is blocked, or the fetch failed) -- callers treat that as
        `vermin_unclassified`, never as a reason to stop the run."""
        cached = self._cache_path(pkey)
        if cached.exists():
            self.cache_hits += 1
            return extract_text(cached.read_bytes())
        if self.blocked or self._client is None:
            return None
        wait = MIN_INTERVAL_SECONDS - (time.monotonic() - self._last_fetch)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_fetch = time.monotonic()
        try:
            resp = await self._client.get(report_url, headers={"User-Agent": USER_AGENT})
        except httpx.TransportError:
            return None
        # Sniff the body rather than trust Content-Type: verified live 2026-09-08, the
        # portal serves some reports as a valid PDF with no Content-Type header at all,
        # and a header-only check read those as a captcha page and blocked the whole run.
        if resp.status_code != 200 or not resp.content.startswith(b"%PDF"):
            self.blocked = True
            return None
        cached.write_bytes(resp.content)
        self.fetched += 1
        return extract_text(resp.content)
