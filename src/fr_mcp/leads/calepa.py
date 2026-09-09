"""CalEPA Regulated Site Portal: the food facilities CDFA's grower-facing registry
never sees -- distribution, cold storage, and the manufacturers who are regulated
for chemicals rather than licensed for farm products.

The portal (https://siteportal.calepa.ca.gov/nsite/) publishes no documented API.
The endpoints below were read out of its own front-end bundle and verified live
2026-09-09; they are open (no key, no auth, no session). Treat that as a private
API: it can change shape with no notice or changelog, and a schema change here is
expected maintenance, not an incident.

Shape of the thing:

- Everything hangs off `/nsite/api`, and the search criteria go in the **query
  string** even for the exports. A criteria value is repeated for arrays, so a
  bounding box is four `boundingBox=` params in min-lon, min-lat, max-lon, max-lat
  order, EPSG:4326.
- The UI's own "advanced search" fields are folded into the free-text `term` as
  `key:"value"` pairs. That is how NAICS filtering works and it is a **prefix**
  match: `naics_code:"311"` returns all of food manufacturing, `naics_code:"3118"`
  just bakeries. This is the whole reason the source is usable -- there is no NAICS
  entry in `/filter/sitefilters`, so anyone looking there concludes it cannot be
  done.
- Two CSV exports carry what a call list needs, and **neither one alone is enough**:
  `Site` has name, address and real lat/lng but no phone; `Affiliations` has the
  phone but no coordinates. They join on `SiteID`, and each export is one request,
  so a NAICS prefix costs two calls total. Both arrive as a **zip**, whatever the
  Content-Type says.

The trap that matters most: **the phone in the Affiliations export is not always
the business's.** Its `AFFIL_TYPE_DESC` column mixes the facility's own people with
the regulator's -- `CUPA District` is the county environmental health department
(every Sacramento row carries (916) 875-8550, the county's switchboard), and
`Regional Board Caseworker` and `Local Agency Caseworker` are state staff. Counting
those makes the source look like it has ~100% phone coverage when the real,
business-line figure is nearer 28%. `BUSINESS_AFFIL_TYPES` is the allow-list;
widening it without checking who the affiliation actually is will put a regulator's
number in front of a rep.

Failure modes, one of them silent:

- A missing bounding box is HTTP 500 -- loud, fine.
- A bounding box in the wrong projection (Web Mercator metres instead of degrees)
  returns **HTTP 200 with zero rows**. No error, no warning, just "nothing here".
  `search_sites` therefore treats an empty result for a box it was told should be
  populated as an error, rather than as an empty county.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass
from typing import Iterable, Sequence

import httpx

from .arcgis import USER_AGENT

API_ROOT = "https://siteportal.calepa.ca.gov/nsite/api"
CLUSTER_URL = f"{API_ROOT}/site/sitecluster"
EXPORT_URL = f"{API_ROOT}/export/exportcsv"

# From /export/exportTypes, verified live 2026-09-09.
EXPORT_SITE = "bd6c32d0-4d1a-4a07-8373-6a8af7fc2693"
EXPORT_AFFILIATIONS = "1b5ac3b8-9fd5-4e87-b789-6d2669a633df"

# NAICS prefixes worth pulling, and what each is in plain terms. Prefix matching
# means the short ones subsume the long ones -- 311 is all food manufacturing --
# so this list is deliberately a handful of broad strokes rather than a taxonomy.
NAICS_PREFIXES: dict[str, str] = {
    "311": "food manufacturing",
    "312": "beverage and tobacco manufacturing",
    "4244": "grocery and related product wholesale",
    "4245": "farm product raw material wholesale",
    "49312": "refrigerated warehousing and storage",
}

# Prefixes that look right and are not, measured live 2026-09-09 in the 28-mile box:
#   4931  warehousing generally -- 56 sites, mostly Amazon fulfilment, Best Buy
#         delivery pads, storage hangars and a flooring distributor. Narrowing to
#         49312 (refrigerated) drops it to 7, every one of them cold chain
#         (Shamrock Foods, US Cold Storage, Lineage).
#   4451  grocery stores -- 177 sites, overwhelmingly 7-Eleven. Retail belongs to
#         the Leads tab, not here.
#   722   restaurants -- 42 sites. Same reason.

# Affiliation roles that are the *business*. Everything else in that column is a
# regulator, a consultant or a signatory -- see the module docstring.
BUSINESS_AFFIL_TYPES = frozenset(
    {
        "Operator",
        "Legal Owner",
        "Legal Operator",
        "Owner/Operator",
        "Owner and Operator",
        "Facility Owner",
        "Facility Contact",
        "Property Owner",
        "Public Contact",
        "Company Official",
        "Technical Contact",
    }
)

# Ordered best-first: the operator runs the site day to day and is who a rep wants,
# the owner is next, and a generic public/technical contact is a last resort.
_AFFIL_PRIORITY = (
    "Operator",
    "Owner/Operator",
    "Owner and Operator",
    "Legal Operator",
    "Legal Owner",
    "Facility Owner",
    "Facility Contact",
    "Property Owner",
    "Company Official",
    "Public Contact",
    "Technical Contact",
)

_WS_RE = re.compile(r"\s+")


class CalEpaError(RuntimeError):
    """The portal did not return usable data this run."""


@dataclass(frozen=True)
class Site:
    site_id: str
    name: str
    address: str
    city: str
    zip5: str
    lat: float | None
    lng: float | None
    naics_prefix: str  # the prefix this row was found under
    phone: str = ""  # 10 digits; "" when no business-role affiliation had one
    phone_role: str = ""  # which affiliation the phone came from
    contact_name: str = ""

    @property
    def key(self) -> str:
        return f"CALEPA:{self.site_id}"


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


def criteria(bbox: Sequence[float], term: str = "") -> list[tuple[str, str]]:
    """Query params shared by the cluster search and both exports. The bounding box
    is four repeated `boundingBox` params in min-lon, min-lat, max-lon, max-lat
    order and **in degrees** -- see the module docstring on what a projection
    mistake looks like (it looks like success)."""
    if len(bbox) != 4:
        raise CalEpaError("bbox must be (min_lon, min_lat, max_lon, max_lat) in EPSG:4326 degrees.")
    params = [("boundingBox", f"{v}") for v in bbox]
    params += [("type", "search"), ("term", term), ("discardLocationlessFeatures", "true")]
    return params


def naics_term(prefix: str) -> str:
    """The advanced-search syntax the front end builds. Prefix match, not exact."""
    return f'naics_code:"{prefix}"'


def _read_zip_csv(payload: bytes, expected_header: str) -> list[dict[str, str]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        head = payload[:120].decode("utf-8", "replace")
        raise CalEpaError(f"CalEPA export was not a zip: {head!r}") from exc
    names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
    if not names:
        raise CalEpaError(f"CalEPA export zip held no CSV: {archive.namelist()}")
    text = archive.read(names[0]).decode("utf-8-sig", "replace")
    reader = csv.DictReader(io.StringIO(text))
    # Match the parsed column name exactly. A substring check passes on a *renamed*
    # column ("PHONE" is in "PHONE_NUMBER"), which is precisely the rename this is
    # here to catch.
    if expected_header not in [(f or "").strip() for f in (reader.fieldnames or [])]:
        raise CalEpaError(
            f"CalEPA export {names[0]} is missing its {expected_header!r} column -- "
            f"the export schema changed (columns: {reader.fieldnames})."
        )
    return list(reader)


async def _export(
    client: httpx.AsyncClient, *, export_type: str, bbox: Sequence[float], term: str, header: str
) -> list[dict[str, str]]:
    params = criteria(bbox, term) + [
        ("exportTypeId", export_type),
        ("includeFilterCount", "false"),
    ]
    try:
        resp = await client.get(EXPORT_URL, params=params, headers={"User-Agent": USER_AGENT})
    except httpx.HTTPError as exc:
        raise CalEpaError(f"CalEPA export request failed: {exc}") from exc
    if resp.status_code != 200:
        raise CalEpaError(
            f"CalEPA export returned HTTP {resp.status_code} "
            "(500 here usually means the bounding box was missing or malformed)."
        )
    return _read_zip_csv(resp.content, header)


def _best_phone(affiliations: Iterable[dict[str, str]]) -> tuple[str, str, str]:
    """(phone, role, contact name) for one site, preferring the role closest to
    whoever answers at the facility. Roles outside `BUSINESS_AFFIL_TYPES` are not
    considered at all -- see the docstring on the CUPA District trap."""
    best: tuple[int, str, str, str] | None = None
    for row in affiliations:
        role = _clean(row.get("AFFIL_TYPE_DESC"))
        if role not in BUSINESS_AFFIL_TYPES:
            continue
        phone = normalize_phone(row.get("PHONE"))
        if not phone:
            continue
        rank = _AFFIL_PRIORITY.index(role) if role in _AFFIL_PRIORITY else len(_AFFIL_PRIORITY)
        if best is None or rank < best[0]:
            best = (rank, phone, role, _clean(row.get("ENTITY_NAME")))
    return (best[1], best[2], best[3]) if best else ("", "", "")


async def fetch_sites(
    client: httpx.AsyncClient,
    *,
    bbox: Sequence[float],
    prefixes: Iterable[str] = tuple(NAICS_PREFIXES),
    require_rows: bool = True,
) -> list[Site]:
    """Two requests per NAICS prefix: the Site export for identity and coordinates,
    the Affiliations export for the phone, joined on `SiteID`.

    `require_rows` guards the silent failure: a wrong-projection bbox answers 200
    with an empty CSV, which is indistinguishable from "no food plants here" unless
    somebody insists there should be some. Pass False only when an empty result is
    genuinely expected (a tiny test box)."""
    found: dict[str, Site] = {}
    for prefix in prefixes:
        term = naics_term(prefix)
        sites = await _export(
            client, export_type=EXPORT_SITE, bbox=bbox, term=term, header="SiteName"
        )
        if not sites:
            continue
        affils = await _export(
            client, export_type=EXPORT_AFFILIATIONS, bbox=bbox, term=term, header="PHONE"
        )
        by_site: dict[str, list[dict[str, str]]] = {}
        for row in affils:
            by_site.setdefault(_clean(row.get("SiteID")), []).append(row)

        for row in sites:
            site_id = _clean(row.get("SiteID"))
            name = _clean(row.get("SiteName"))
            if not site_id or not name or site_id in found:
                continue
            phone, role, contact = _best_phone(by_site.get(site_id, []))
            found[site_id] = Site(
                site_id=site_id,
                name=name,
                address=_clean(row.get("Address")),
                city=_clean(row.get("City")).title(),
                zip5=_clean(row.get("ZIP"))[:5],
                lat=_float(row.get("Latitude")),
                lng=_float(row.get("Longitude")),
                naics_prefix=prefix,
                phone=phone,
                phone_role=role,
                contact_name=contact,
            )

    if require_rows and not found:
        raise CalEpaError(
            "CalEPA returned zero sites for every NAICS prefix. A bounding box in the "
            "wrong projection answers HTTP 200 with an empty result, so this is more "
            "likely a bad bbox than an empty region."
        )
    return list(found.values())
