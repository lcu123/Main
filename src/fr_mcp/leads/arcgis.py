"""Sacramento County's ArcGIS food-inspection feed: pull, normalise, ICP-tier.

CC0-licensed, no auth. Feature service:
https://services1.arcgis.com/5NARefyPVtAeuJPU/arcgis/rest/services/Food_Inspections/FeatureServer
Layer 0 = Facilities (one row per Facility_ID, the latest inspection); layer 1 =
Inspection & Violation History (one row per inspection per permit). See
docs/lead-scraper-plan.md sections 2.1-2.3 for the field notes this encodes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import httpx

FEATURE_SERVICE = (
    "https://services1.arcgis.com/5NARefyPVtAeuJPU/arcgis/rest/services/Food_Inspections/FeatureServer"
)
FACILITIES_LAYER = 0
HISTORY_LAYER = 1

# A real browser UA -- the portal (and, historically, some ArcGIS-fronted county
# sites) can be unfriendly to obvious bot traffic. The feature service itself
# has answered plain requests fine in testing; sending this costs nothing.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

PAGE_SIZE = 2000

# The 11 fields both layers share (docs/lead-scraper-plan.md 2.1). If a future
# re-publish of the feed drops or renames one of these, fail loudly before any
# FieldRoutes write rather than silently scoring on missing data.
EXPECTED_FIELDS = {
    "OBJECTID", "Facility_ID", "Facility_Name", "Facility_Address", "Description",
    "Inspection_Service", "Inspection_Type", "Inspection_Result", "Inspection_Date",
    "Inspection_Report", "Violation_Description",
}
MIN_LAYER0_ROWS = 5000  # verified live at 6,211; a feed outage/empty response is nowhere close

VERMIN_CATEGORY = "VERMIN AND ANIMAL CONTAMINATION"
CLOSURE_RESULTS = {"CLOSED", "SUSPENSION OF PERMIT TO OPERATE"}
CRITICAL_RESULTS = {"CRITICAL VIOLATIONS", "CONDITIONAL PASS"}
ATTEMPTED_TYPE = "ATTEMPTED - UNABLE TO INSPECT"

PKEY_RE = re.compile(r"pKey=([0-9A-Fa-f\-]{36})")


class FeedError(RuntimeError):
    """The feed didn't look like the schema this pipeline was built against."""


# --- ICP mapping (docs/lead-scraper-plan.md 2.2, 3.2) ----------------------


# Deliberately no \b word boundaries around most of these -- real business names
# compound them ("SUPERMARKET", "BAKESHOP", "TAQUERIA...MARISCOS"), and plan 2.2's own
# keyword-hit counts ("supermarket" 49, separate from "market" 268) were substring
# counts, not word-bounded ones. "meat" and "deli" stay short enough that a stray
# substring hit is an acceptable false positive for a scoring bonus, not a filter.
_SMALL_MARKET_KEYWORD_RE = re.compile(
    r"(market|mercado|carnicer|seafood|fish|bakery|bake|panader|halal|grocery|deli)", re.I
)
_FOOD_PREP_KEYWORD_RE = re.compile(
    r"(bakery|bake|panader|catering|commissary|kitchen|meat|seafood|fish|carnicer)", re.I
)
_MEAT_SEAFOOD_RE = re.compile(r"(meat|carnicer|seafood|fish|pescader)", re.I)
_BAKERY_RE = re.compile(r"(bakery|bake|panader)", re.I)
_SCHOOL_HOSPITAL_RE = re.compile(r"\b(school|elementary|academy|district|hospital|medical)\b", re.I)


@dataclass(frozen=True)
class PermitClass:
    base_icp: int
    excluded: bool = False
    keyword_bump_to: int | None = None  # icp_fit base when `keyword_re` matches the name
    keyword_re: "re.Pattern[str] | None" = None


# Description values are the county's own strings, verified live 2026-09-07 -- including
# the unclosed paren on the pre-packaged market type. The raw feed has a trailing space
# on "FOOD PREP ESTAB " specifically; every lookup here goes through the stripped form
# (normalize_layer0/attach_signals store permits stripped, and permit_class() strips its
# input too), so the key below is the stripped spelling, not the raw one.
PERMIT_CLASSES: dict[str, PermitClass] = {
    "RETAIL MARKET (15000+SQ.FT)": PermitClass(30),
    "RETAIL MARKET (6000-14999 SQ.FT.)": PermitClass(30),
    "COMMISSARY": PermitClass(28),
    "SATELLITE FOOD DISTRIBUTION FACILITY": PermitClass(25),
    "BAKERY--NO PREPARATION": PermitClass(22),
    "RETAIL MARKET (LESS THAN 6000 SQ FT)": PermitClass(12, keyword_bump_to=24, keyword_re=_SMALL_MARKET_KEYWORD_RE),
    "FOOD PREP ESTAB": PermitClass(8, keyword_bump_to=18, keyword_re=_FOOD_PREP_KEYWORD_RE),
    "RESTAURANT WITH BAR": PermitClass(14),
    "RESTAURANT": PermitClass(12),
    "LICENSED HEALTH CARE FACILITY": PermitClass(12),
    # Excluded: not owner-accessible, or no meaningful rodent/pest exposure.
    "MOBILE FOOD FACILITY CAT D": PermitClass(0, excluded=True),
    "SCHOOL AND/OR NONPROFIT SENIOR MEAL PROGRAM": PermitClass(0, excluded=True),
    "SCHOOL SATELLITE FACILITY - EACH FACILITY": PermitClass(0, excluded=True),
    "RETAIL MARKET (25SQFT<300SQFT PRE PKG NON-PHF": PermitClass(0, excluded=True),
    "BAR": PermitClass(0, excluded=True),
    "CERTIFIED FARMERS' MARKET": PermitClass(0, excluded=True),
    "VETERAN'S ORGANIZATION FOOD FACILITY": PermitClass(0, excluded=True),
    "RESTRICTED FOOD SERVICE ESTABLISHMENT": PermitClass(0, excluded=True),
    "PRODUCE STAND": PermitClass(0, excluded=True),
    "FARM STAND": PermitClass(0, excluded=True),
}

# National/big-box chains: procurement is corporate, not owner-accessible (plan 3.1).
# Regional ethnic operators (99 Ranch, La Superior, Seafood City, Viva, ...) are
# deliberately NOT on this list -- the owner treats them as eligible independents.
CHAIN_NAME_RE = re.compile(
    r"\b("
    r"costco|safeway|walmart|target|raley'?s|bel air|nob hill|save mart|foodmaxx|winco"
    r"|trader joe'?s|whole foods|sprouts|grocery outlet|smart\s*&\s*final|smart foodservice"
    r"|sam'?s club|foods\s*co|food\s*4\s*less|us foods|bevmo|nugget|7[\s-]*eleven"
    r"|chevron|arco|shell|dollar tree|dollar general|walgreens|cvs|rite aid"
    r"|starbucks|taco bell|subway|mcdonald'?s|dutch bros|jack in the box|carl'?s jr"
    r"|round table|panda express|mountain mike'?s|chipotle|jamba|domino'?s|burger king"
    r"|little caesars|wendy'?s|el pollo loco|popeyes|panera|wingstop|kfc|pizza hut"
    r"|chick-fil-a|denny'?s|ihop|applebee'?s|olive garden|black angus|sonic|peet'?s"
    r"|home depot|office depot|24 hour fitness"
    r")\b",
    re.I,
)
_STORE_SUFFIX_RE = re.compile(r"\s*[#\-]\s*[\dA-Za-z]+$")
_TRAILING_STORE_CODE_RE = re.compile(r"\s+\d{3,}[A-Za-z]?$")


def permit_class(description: str) -> PermitClass:
    return PERMIT_CLASSES.get(description.strip(), PermitClass(0, excluded=True))


def effective_icp_base(permit_cls: PermitClass, facility_name: str) -> int:
    """The base_icp a permit class contributes, after the name-keyword bump
    ("small market 12, or 24 with a market keyword in the name")."""
    if permit_cls.keyword_bump_to is not None and permit_cls.keyword_re and permit_cls.keyword_re.search(facility_name):
        return permit_cls.keyword_bump_to
    return permit_cls.base_icp


def has_meat_seafood_keyword(facility_name: str) -> bool:
    return bool(_MEAT_SEAFOOD_RE.search(facility_name))


def has_bakery_keyword(facility_name: str) -> bool:
    return bool(_BAKERY_RE.search(facility_name))


def has_market_keyword(facility_name: str) -> bool:
    """Used by myhd.py too, for Placer/Yolo's own mid-size-market keyword bump --
    their permit-type vocabulary doesn't carry a `PermitClass.keyword_bump_to` of
    its own, so they call this directly rather than going through `effective_icp_base`."""
    return bool(_SMALL_MARKET_KEYWORD_RE.search(facility_name))


def is_school_or_hospital(facility_name: str) -> bool:
    """Satellite/distribution permits at a school or hospital aren't owner-accessible
    even though the permit type itself (SATELLITE FOOD DISTRIBUTION FACILITY) is
    eligible -- plan 3.4's A-tier carve-out."""
    return bool(_SCHOOL_HOSPITAL_RE.search(facility_name))


def base_name(facility_name: str) -> str:
    """Strip a trailing store number ("#181", "24276F", "- 34629") so chain and
    multi-location detection groups a chain's locations under one name."""
    name = facility_name.strip()
    prev = None
    while prev != name:
        prev = name
        name = _STORE_SUFFIX_RE.sub("", name).strip()
        name = _TRAILING_STORE_CODE_RE.sub("", name).strip()
    return name.upper()


def is_chain_name(facility_name: str) -> bool:
    return bool(CHAIN_NAME_RE.search(facility_name))


# --- address parsing (plan 2.1: "no state, zip+4, occasional 'CA, USA' noise") ---

_ADDR_RE = re.compile(r"^(?P<street>.+?),\s*(?P<city>[A-Za-z .'\-]+?)\s+(?P<zip>\d{5})(?:-\d{4})?\s*$")
_NOISE_RE = re.compile(r",?\s*CA,?\s*USA\s*", re.I)


@dataclass(frozen=True)
class ParsedAddress:
    street: str
    city: str
    zip5: str


def parse_address(raw: str) -> ParsedAddress | None:
    cleaned = _NOISE_RE.sub(", ", raw or "").strip()
    m = _ADDR_RE.match(cleaned)
    if not m:
        return None
    return ParsedAddress(street=m.group("street").strip(), city=m.group("city").strip(), zip5=m.group("zip"))


# --- normalised records -----------------------------------------------------


@dataclass(frozen=True)
class Signal:
    pkey: str
    date: date
    result: str
    inspection_type: str
    violation_description: str | None
    report_url: str

    @property
    def is_vermin(self) -> bool:
        return bool(self.violation_description) and VERMIN_CATEGORY in self.violation_description

    @property
    def is_closure(self) -> bool:
        return self.result in CLOSURE_RESULTS

    @property
    def is_critical(self) -> bool:
        return self.result in CRITICAL_RESULTS

    @property
    def is_attempted(self) -> bool:
        return self.inspection_type == ATTEMPTED_TYPE


@dataclass
class Facility:
    facility_id: str
    name: str
    street: str
    city: str
    zip5: str
    lat: float | None
    lng: float | None
    permits: set[str] = field(default_factory=set)
    latest_inspection: date | None = None
    latest_result: str | None = None
    # Layer 0's own Inspection_Report: the facility's most recent report, whatever its
    # result. Its header carries the owner name and phone even on a plain PASS, which
    # makes it the phone source for territory-lane facilities that have no signal.
    latest_report_url: str = ""
    latest_pkey: str = ""
    signals: list[Signal] = field(default_factory=list)
    vermin_count_24mo: int = 0

    @property
    def best_permit_class(self) -> PermitClass:
        classes = [permit_class(p) for p in self.permits] or [PermitClass(0, excluded=True)]
        eligible = [c for c in classes if not c.excluded]
        return max(eligible, key=lambda c: c.base_icp) if eligible else classes[0]

    @property
    def excluded_type(self) -> bool:
        return self.best_permit_class.excluded

    @property
    def extra_permit_count(self) -> int:
        return max(len(self.permits) - 1, 0)


def _epoch_ms_to_date(value: object) -> date | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).date()
    except (TypeError, ValueError, OSError):
        return None


def _pkey_from_url(url: str | None) -> str:
    m = PKEY_RE.search(url or "")
    return m.group(1) if m else ""


def normalize_layer0(rows: list[dict]) -> dict[str, Facility]:
    """Layer 0 has one row per Facility_ID and carries the latest permit; multiple
    permits under one facility only show up across several rows in layer 1, so
    `permits` starts with just this row's Description and is extended by
    `attach_history`."""
    out: dict[str, Facility] = {}
    for row in rows:
        attrs = row.get("attributes", row)
        fid = attrs.get("Facility_ID")
        if not fid:
            continue
        parsed = parse_address(attrs.get("Facility_Address") or "")
        geom = row.get("geometry") or {}
        out[fid] = Facility(
            facility_id=fid,
            name=" ".join(str(attrs.get("Facility_Name") or "").split()),
            street=parsed.street if parsed else "",
            city=parsed.city if parsed else "",
            zip5=parsed.zip5 if parsed else "",
            lat=geom.get("x") and geom.get("y") and float(geom["y"]),
            lng=geom.get("x") and geom.get("y") and float(geom["x"]),
            permits={(attrs.get("Description") or "").strip()} if attrs.get("Description") else set(),
            latest_inspection=_epoch_ms_to_date(attrs.get("Inspection_Date")),
            latest_result=attrs.get("Inspection_Result"),
            latest_report_url=attrs.get("Inspection_Report") or "",
            latest_pkey=_pkey_from_url(attrs.get("Inspection_Report")),
        )
    return out


def attach_signals(facilities: dict[str, Facility], history_rows: list[dict]) -> None:
    """Fold layer-1 rows into their facility: extend the permit set, and keep every
    signal (vermin / closure / suspension / critical / conditional) as a candidate
    -- scoring later picks the strongest one. Rows sharing a pKey (a facility with
    several permits inspected together) are kept once per pKey per facility."""
    seen: dict[str, set[str]] = {}
    for row in history_rows:
        attrs = row.get("attributes", row)
        fid = attrs.get("Facility_ID")
        fac = facilities.get(fid)
        if fac is None:
            continue
        desc = (attrs.get("Description") or "").strip()
        if desc:
            fac.permits.add(desc)
        result = attrs.get("Inspection_Result") or ""
        violation = attrs.get("Violation_Description")
        is_signal = (violation and VERMIN_CATEGORY in violation) or result in CLOSURE_RESULTS | CRITICAL_RESULTS
        if not is_signal:
            continue
        pkey = _pkey_from_url(attrs.get("Inspection_Report"))
        if pkey and pkey in seen.setdefault(fid, set()):
            continue
        if pkey:
            seen[fid].add(pkey)
        d = _epoch_ms_to_date(attrs.get("Inspection_Date"))
        if d is None:
            continue
        signal = Signal(
            pkey=pkey,
            date=d,
            result=result,
            inspection_type=attrs.get("Inspection_Type") or "",
            violation_description=violation,
            report_url=attrs.get("Inspection_Report") or "",
        )
        if not signal.is_attempted:
            fac.signals.append(signal)


def apply_vermin_counts(facilities: dict[str, Facility], counts: dict[str, int]) -> None:
    for fid, n in counts.items():
        if fid in facilities:
            facilities[fid].vermin_count_24mo = n


def best_signal(facility: Facility) -> Signal | None:
    """Most recent signal, preferring vermin/closure over a bare critical/conditional
    when dates tie (a vermin category is stronger evidence than "conditional pass")."""
    if not facility.signals:
        return None

    def rank(s: Signal) -> tuple:
        strength = 2 if (s.is_vermin or s.is_closure) else 1
        return (s.date, strength)

    return max(facility.signals, key=rank)


# --- hard filters (plan 3.1) -------------------------------------------------

MAX_MILES = 35.0
MAX_STALE_DAYS = 540


def hard_filter_reason(facility: Facility, *, today: date, distance_miles: float | None) -> str | None:
    """None if the facility survives the hard filters; otherwise the reason it was
    dropped entirely (never scored, never pushed -- distinct from `parked chain`,
    which is scored/logged but withheld from the push)."""
    if facility.excluded_type:
        return "excluded permit type"
    if facility.latest_inspection and (today - facility.latest_inspection).days > MAX_STALE_DAYS:
        return "stale (no inspection in over 540 days)"
    if distance_miles is not None and distance_miles > MAX_MILES:
        return "more than 35 miles from the office"
    if not facility.zip5:
        return "no parseable address"
    return None


# --- fetching (network) ------------------------------------------------------


async def _query(client: httpx.AsyncClient, layer: int, **params: object) -> dict:
    p = {"f": "json", **params}
    resp = await client.get(f"{FEATURE_SERVICE}/{layer}/query", params=p, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise FeedError(f"ArcGIS layer {layer} error: {data['error']}")
    return data


def _assert_schema(features: list[dict]) -> None:
    if not features:
        return
    fields = set(features[0].get("attributes", {}).keys())
    missing = EXPECTED_FIELDS - fields
    if missing:
        raise FeedError(f"ArcGIS feed is missing expected field(s) {sorted(missing)} -- schema may have changed")


async def fetch_paginated(
    client: httpx.AsyncClient,
    layer: int,
    *,
    where: str,
    out_fields: str,
    return_geometry: bool = False,
    assert_full_schema: bool = False,
    **extra: object,
) -> list[dict]:
    """`assert_full_schema` is for callers requesting every field (`out_fields="*"`
    or the explicit full list) -- a narrow query like `fetch_vermin_counts` (just
    Facility_ID) would otherwise fail the check on every field it *didn't* ask for,
    which isn't a schema change, just a smaller request."""
    features: list[dict] = []
    offset = 0
    while True:
        data = await _query(
            client,
            layer,
            where=where,
            outFields=out_fields,
            returnGeometry="true" if return_geometry else "false",
            outSR=4326,
            orderByFields="OBJECTID",
            resultRecordCount=PAGE_SIZE,
            resultOffset=offset,
            **extra,
        )
        page = data.get("features", [])
        if assert_full_schema:
            _assert_schema(page)
        features.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return features


async def fetch_layer0_full(client: httpx.AsyncClient) -> list[dict]:
    features = await fetch_paginated(
        client, FACILITIES_LAYER, where="1=1", out_fields="*", return_geometry=True, assert_full_schema=True
    )
    if len(features) < MIN_LAYER0_ROWS:
        raise FeedError(f"ArcGIS layer 0 returned only {len(features)} rows (expected >= {MIN_LAYER0_ROWS})")
    return features


async def fetch_layer1_since(client: httpx.AsyncClient, since: date) -> list[dict]:
    where = (
        f"Inspection_Date >= timestamp '{since.isoformat()} 00:00:00' "
        f"AND (Violation_Description LIKE '%{VERMIN_CATEGORY}%' "
        f"OR Inspection_Result IN ({', '.join(repr(r) for r in sorted(CLOSURE_RESULTS | CRITICAL_RESULTS))}))"
    )
    return await fetch_paginated(
        client,
        HISTORY_LAYER,
        where=where,
        out_fields=",".join(sorted(EXPECTED_FIELDS)),
        assert_full_schema=True,
    )


async def fetch_vermin_counts(client: httpx.AsyncClient, months_back: date) -> dict[str, int]:
    where = f"Violation_Description LIKE '%{VERMIN_CATEGORY}%' AND Inspection_Date >= timestamp '{months_back.isoformat()} 00:00:00'"
    rows = await fetch_paginated(client, HISTORY_LAYER, where=where, out_fields="Facility_ID")
    counts: dict[str, int] = {}
    for row in rows:
        fid = row.get("attributes", {}).get("Facility_ID")
        if fid:
            counts[fid] = counts.get(fid, 0) + 1
    return counts
