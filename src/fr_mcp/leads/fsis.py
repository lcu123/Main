"""USDA FSIS Meat, Poultry and Egg Product Inspection Directory.

The cleanest data in this whole effort: one static CSV, no auth, no pagination, no
PDF parsing, and every row carries a phone *and* real coordinates -- so the 28-mile
cut is exact here rather than approximated from a zip centroid. It is also small.
Measured live 2026-09-09: 7,237 establishments nationally, 741 in California,
**28 inside the radius, all 28 with a phone**.

Small does not mean unimportant. These are federally inspected meat and poultry
plants, which is about as close to a guaranteed pest-control buyer as this pipeline
gets, and the merged CalEPA+CDFA set contained exactly one row categorised as meat
processing. This source is most of that category.

**The host blocks some networks.** `www.fsis.usda.gov` returns HTTP 403 to every
request from this project's sandbox -- an Akamai edge denial against the egress IP,
not a 404 and not something a User-Agent fixes. Whether Railway is blocked too is
unknown until a real run there, so `fetch_establishments` tries the live file first
and falls back to the Internet Archive's copy of the same URL, reporting which one
answered. The archived copy is byte-identical but can be weeks stale, which for a
directory of licensed plants is an acceptable trade against having no source at
all. If the live fetch starts working, the fallback simply stops being reached.

One wire-level detail: the archive serves the CSV gzip-encoded in a way that does
not always survive as a decoded body, so `_decode` sniffs the gzip magic bytes
rather than trusting Content-Encoding -- the same lesson `reports.PoliteFetcher`
learned about sniffing `%PDF` instead of Content-Type.
"""

from __future__ import annotations

import csv
import gzip
import io
import re
from dataclasses import dataclass
from typing import Iterable

import httpx

from . import regions
from .arcgis import USER_AGENT

DIRECTORY_URL = (
    "https://www.fsis.usda.gov/sites/default/files/media_file/documents/"
    "MPI_Directory_by_Establishment_Number.csv"
)
ARCHIVE_URL = f"https://web.archive.org/web/2id_/{DIRECTORY_URL}"

_GZIP_MAGIC = b"\x1f\x8b"
_WS_RE = re.compile(r"\s+")


class FsisError(RuntimeError):
    """Neither the live directory nor the archived copy produced a usable CSV."""


@dataclass(frozen=True)
class Establishment:
    establishment_id: str
    number: str
    name: str
    street: str
    city: str
    state: str
    zip5: str
    phone: str  # 10 digits
    activities: str  # "Meat Processing; Poultry Slaughter", etc.
    size: str  # FSIS size class: Very Small / Small / Large
    lat: float | None
    lng: float | None
    dbas: str = ""

    @property
    def key(self) -> str:
        return f"FSIS:{self.establishment_id or self.number}"

    @property
    def slaughters(self) -> bool:
        """A slaughter operation is a different pest conversation from a plant that
        only processes -- more blood, more offal, more flies."""
        return "slaughter" in (self.activities or "").lower()


def _clean(value: str | None) -> str:
    return _WS_RE.sub(" ", (value or "").replace("\xa0", " ")).strip()


def normalize_phone(raw: str | None) -> str:
    digits = re.sub(r"\D+", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def _float(raw: str | None) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _decode(payload: bytes) -> str:
    """Gzip is detected by its magic bytes, not by a header. The archive's copy of
    this file arrives gzipped whether or not it says so."""
    if payload.startswith(_GZIP_MAGIC):
        payload = gzip.decompress(payload)
    return payload.decode("utf-8-sig", "replace")


def parse_directory(text: str) -> list[Establishment]:
    reader = csv.DictReader(io.StringIO(text))
    fields = [(f or "").strip() for f in (reader.fieldnames or [])]
    for required in ("establishment_name", "phone", "latitude", "longitude"):
        if required not in fields:
            raise FsisError(
                f"FSIS directory is missing its {required!r} column -- the file layout "
                f"changed (columns: {fields[:8]}...)."
            )
    out: list[Establishment] = []
    for row in reader:
        name = _clean(row.get("establishment_name"))
        if not name:
            continue
        out.append(
            Establishment(
                establishment_id=_clean(row.get("establishment_id")),
                number=_clean(row.get("establishment_number")),
                name=name,
                street=_clean(row.get("street")),
                city=_clean(row.get("city")).title(),
                state=_clean(row.get("state")).upper(),
                zip5=_clean(row.get("zip"))[:5],
                phone=normalize_phone(row.get("phone")),
                activities=_clean(row.get("activities")),
                size=_clean(row.get("size")),
                lat=_float(row.get("latitude")),
                lng=_float(row.get("longitude")),
                dbas=_clean(row.get("dbas")),
            )
        )
    return out


async def fetch_establishments(client: httpx.AsyncClient) -> tuple[list[Establishment], str]:
    """(establishments, which source answered). Live file first, Internet Archive
    second -- see the module docstring on the 403. The caller should record the
    source, because "archive" means the data may be weeks old."""
    errors: list[str] = []
    for label, url in (("live", DIRECTORY_URL), ("archive", ARCHIVE_URL)):
        try:
            resp = await client.get(
                url, headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=120.0
            )
        except httpx.HTTPError as exc:
            errors.append(f"{label}: {exc}")
            continue
        if resp.status_code != 200:
            errors.append(f"{label}: HTTP {resp.status_code}")
            continue
        try:
            rows = parse_directory(_decode(resp.content))
        except (FsisError, OSError, UnicodeError) as exc:
            errors.append(f"{label}: {exc}")
            continue
        if rows:
            return rows, label
        errors.append(f"{label}: parsed zero establishments")
    raise FsisError("FSIS directory unavailable -- " + "; ".join(errors))


def distance_miles(est: Establishment) -> float | None:
    """Real coordinates when FSIS has them, the zip or city centroid when it does
    not -- the same order every other source in this pipeline follows."""
    if est.lat is not None and est.lng is not None:
        return regions.haversine_miles(est.lat, est.lng)
    centroid, _ = regions.locate(est.zip5, est.city)
    return regions.haversine_miles(*centroid) if centroid else None


def within(establishments: Iterable[Establishment], max_miles: float) -> list[Establishment]:
    keep = []
    for est in establishments:
        d = distance_miles(est)
        if d is not None and d <= max_miles:
            keep.append(est)
    return keep
