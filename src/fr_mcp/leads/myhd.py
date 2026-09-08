"""Placer and Yolo counties' inspection-portal JSON adapter.

Both counties publish on the same `myhealthdepartment.com` platform Sacramento's
own portal search API sits on (see `arcgis.py`'s docstring and plan section 2.5),
each under its own `path` and with its own field names and permit-type vocabulary
(plan section 2.6, verified live 2026-09-07/08). Unlike the Sacramento ArcGIS feed,
this is the *only* bulk source for these two counties -- no open-data feed exists
for either -- so the portal's 25-row page cap, 2-second politeness gap and
403/captcha circuit breaker are load-bearing here, not just a nicety.

Facility identity: Placer's key is the `PR…` permit number parsed straight out of
the row's `permitName` (its report PDF carries no owner/phone/email at all, so
there is no reason to fetch it just for the key -- plan section 4). Yolo's key is
the `FA…` facility ID from its report PDF header, which is fetched for every
surviving candidate anyway because that PDF is also Yolo's one free email source
(`reports.parse_yolo_header`). Both counties collapse multiple permit rows under
one facility by (base name, street address), the same way Placer's own facilities
do in the plan's own key notes -- a live Yolo pull already shows this is necessary
too (ARIANA FOOD MARKET carries both a bakery and a retail-market permit as two
separate rows).
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from . import arcgis as ag
from . import regions
from .classify import UNCLASSIFIED, Classification, classify_text
from .pipeline import EVENT_SIGNAL_WINDOW_DAYS, LANE_CHAIN, LANE_EVENT, LANE_TERRITORY, TERRITORY_ICP_MIN, LeadCandidate
from .reports import PoliteFetcher, ReportHeader, is_entity_name, parse_yolo_header
from .score import IcpInputs, PestInputs, score_candidate

SEARCH_URL = "https://inspections.myhealthdepartment.com/"
PAGE_SIZE = 25  # the portal's own cap (plan 2.6): a larger `count` is silently ignored
MIN_INTERVAL_SECONDS = 2.0
MAX_PAGES = 300  # circuit breaker on a runaway date window -- 7,500 rows, well past a 180-day backfill


class PortalError(RuntimeError):
    """The portal blocked us (403, captcha redirect, non-JSON) or answered with
    something that doesn't look like a searchInspections response."""


@dataclass(frozen=True)
class CountyConfig:
    key: str  # "Placer" | "Yolo" -- becomes LeadCandidate.county
    path: str  # the portal's own `path` value ("pchd" / "yolocountyeh")
    key_prefix: str  # customerLink prefix ("PCHD" / "YOLO")
    food_program: str  # programName value that means "a food inspection", not pool/body art
    zip_allowlist: frozenset[str]
    fetch_pdf: bool  # Yolo's report has the email; Placer's has nothing worth a fetch


PLACER = CountyConfig(
    key="Placer",
    path="pchd",
    key_prefix="PCHD",
    food_program="Retail Food",
    zip_allowlist=frozenset({"95661", "95678", "95747", "95746", "95677", "95765", "95648", "95650"}),
    fetch_pdf=False,
)
YOLO = CountyConfig(
    key="Yolo",
    path="yolocountyeh",
    key_prefix="YOLO",
    food_program="Food",
    zip_allowlist=frozenset({"95605", "95691"}),
    fetch_pdf=True,
)


class PortalCircuit:
    """Shared block flag across both counties and their PDF fetches for one run:
    once any portal call comes back 403/captcha/non-JSON, everything downstream
    backs off instead of retrying (plan section 8's "stops all portal calls for
    the rest of the run"). `reports.PoliteFetcher` carries its own, separate
    `.blocked` for the report-PDF endpoint; callers check both."""

    def __init__(self) -> None:
        self.blocked = False
        self.block_reason: str | None = None

    def trip(self, reason: str) -> None:
        self.blocked = True
        self.block_reason = reason


def report_url(config: CountyConfig, inspection_id: str) -> str:
    """Same URL shape as Sacramento's own report link (reports.py): the portal path
    doubles as both the URL prefix and the `path` query param. Yolo's real cached
    report (reports.parse_yolo_header's docstring) was fetched this way."""
    return f"https://inspections.myhealthdepartment.com/{config.path}/print/?task=getPrintable&path={config.path}&pKey={inspection_id}"


def _zip5(raw: str | None) -> str:
    return (raw or "").strip()[:5]


def _row_zip5(row: dict[str, Any]) -> str:
    return _zip5(row.get("zip"))


def _row_street(row: dict[str, Any]) -> str:
    parts = [row.get("addressLine1") or "", row.get("addressLine2") or ""]
    return ", ".join(p.strip() for p in parts if p and p.strip())


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None


# --- fetching (network) ------------------------------------------------------


async def search_page(
    client: httpx.AsyncClient, config: CountyConfig, *, date_from: date, date_to: date, start: int
) -> list[dict[str, Any]]:
    body = {
        "task": "searchInspections",
        "data": {
            "path": config.path,
            "programName": "",
            "filters": {"date": f"{date_from.isoformat()} to {date_to.isoformat()}"},
            "start": start,
            "count": PAGE_SIZE,
            "searchStr": "",
            "lat": 0,
            "lng": 0,
            "sort": None,
        },
    }
    resp = await client.post(SEARCH_URL, json=body, headers={"User-Agent": ag.USER_AGENT})
    if resp.status_code != 200:
        raise PortalError(f"{config.key} portal returned HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise PortalError(f"{config.key} portal returned a non-JSON response (captcha page?)") from exc
    if not isinstance(data, list):
        raise PortalError(f"{config.key} portal returned an unexpected shape: {type(data).__name__}")
    return data


async def search_county(
    client: httpx.AsyncClient, config: CountyConfig, circuit: PortalCircuit, *, date_from: date, date_to: date
) -> list[dict[str, Any]]:
    """Pages the whole date window at the portal's 25-row cap with a politeness gap,
    tripping `circuit` (rather than raising) the moment a page fails -- so a block on
    Placer still lets a caller record the run and move on, and a block on either
    county stops Yolo's PDF fetches too (both share `circuit`)."""
    if circuit.blocked:
        return []
    rows: list[dict[str, Any]] = []
    start = 0
    last_fetch = 0.0
    for _ in range(MAX_PAGES):
        wait = MIN_INTERVAL_SECONDS - (time.monotonic() - last_fetch)
        if wait > 0:
            await asyncio.sleep(wait)
        last_fetch = time.monotonic()
        try:
            page = await search_page(client, config, date_from=date_from, date_to=date_to, start=start)
        except PortalError as exc:
            circuit.trip(str(exc))
            break
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return rows


# --- per-county permit -> icp_fit base (plan 2.6, 3.4) ------------------------
# Each function returns (base_icp, excluded). Regex-matched rather than an exact-
# string dict like Sacramento's PERMIT_CLASSES, because the live vocabulary has
# more size/seat-range phrasing variance than the plan's own summary table shows
# (e.g. a real Placer row reads "Market - With Food Prep < 5000 Sq Ft", not the
# "Equal To Or > 5000"/"< 500" phrasing the table documents) -- verified against
# real Placer and Yolo search-API pulls, 2026-09-08.


def _placer_icp_base(permit_type: str, facility_name: str) -> tuple[int, bool]:
    pt = re.sub(r"[,\s]+", " ", permit_type).strip().lower()
    if re.search(r"\bbar\b", pt) or "mobile food" in pt or "school cafeteria" in pt:
        return (0, True)
    if "market" in pt:
        if re.search(r"no food prep.{0,10}<\s*500\b", pt):
            return (0, True)  # a snack rack, not a real market -- e.g. a gym's vending permit
        if re.search(r"equal to or\s*>\s*5000\b", pt):
            return (30, False)
        if re.search(r">\s*500\s*-\s*5000\b", pt) or re.search(r"<\s*5000\b", pt):
            return (24, False) if ag.has_market_keyword(facility_name) else (12, False)
        return (0, True)  # unrecognized market-size phrasing: exclude rather than guess
    if "restaurant" in pt:
        if re.search(r"100\s*or\s*more\s*seats", pt):
            return (14, False)
        if re.search(r"50\s*-\s*99\s*seats", pt):
            return (12, False)
        if re.search(r"0\s*-\s*49\s*seats", pt):
            return (8, False)
        return (12, False)  # a restaurant permit whose seat bucket doesn't match: mid-tier default
    return (0, True)


def _yolo_icp_base(permit_type: str, facility_name: str) -> tuple[int, bool]:
    pt = permit_type.replace(",", "").strip().lower()
    if any(k in pt for k in ("school", "satellite", "pool", "spa", "temporary", "edible food recovery")):
        return (0, True)
    if "bakery" in pt:
        return (22, False)
    if "catering" in pt:
        return (24, False)
    if "market" in pt:
        if "5000+" in pt or re.search(r"equal to or\s*>\s*5000", pt):
            return (30, False)
        if re.search(r"2000\s*-\s*4999", pt):
            return (24, False)
        if "less than 2000" in pt:
            return (24, False) if ag.has_market_keyword(facility_name) else (12, False)
        return (0, True)
    if "restaurant" in pt:
        if "150+" in pt or re.search(r"150\s*or\s*more", pt):
            return (14, False)
        if re.search(r"50\s*-\s*149", pt):
            return (12, False)
        if re.search(r"26\s*-\s*49", pt) or re.search(r"0\s*-\s*25", pt):
            return (8, False)
        return (12, False)
    return (0, True)


def _icp_base(config: CountyConfig, permit_type: str, facility_name: str) -> tuple[int, bool]:
    return _placer_icp_base(permit_type, facility_name) if config is PLACER else _yolo_icp_base(permit_type, facility_name)


def _group_best_permit(config: CountyConfig, rows: list[dict[str, Any]], facility_name: str) -> tuple[int, bool, set[str]]:
    """Same rule as arcgis.Facility.best_permit_class: a facility survives on its
    best non-excluded permit, and only counts as excluded if every permit it holds
    is excluded (plan 3.1's "unless the facility also holds a non-excluded permit")."""
    permits = {r.get("permitType") or "" for r in rows if r.get("permitType")}
    scored = [_icp_base(config, p, facility_name) for p in permits]
    eligible = [base for base, excluded in scored if not excluded]
    if eligible:
        return max(eligible), False, permits
    return 0, True, permits


# --- per-row pest signal (plan 3.4: comments first, placard/outcome second) --

_PLACER_CLOSED_OUTCOMES = {"Red Placard"}
_PLACER_CRITICAL_OUTCOMES = {"Yellow Placard"}


def _row_pest(config: CountyConfig, row: dict[str, Any]) -> tuple[PestInputs, Classification, date | None]:
    d = _parse_date(row.get("inspectionDate"))
    classification = classify_text(row.get("comments") or "")
    outcome = (row.get("InspectionOutcome") or "").strip() if config is PLACER else ""
    if classification.pests:
        kind = classification.label
    elif outcome in _PLACER_CLOSED_OUTCOMES:
        kind = "closure"
    elif outcome in _PLACER_CRITICAL_OUTCOMES:
        kind = "critical"
    else:
        kind = "none"
    pest = PestInputs(
        kind=kind,
        closed_or_suspended=outcome in _PLACER_CLOSED_OUTCOMES,
        no_pco=classification.no_pco,
        live_evidence=classification.live_evidence,
    )
    return pest, classification, d


def _best_row_signal(
    config: CountyConfig, rows: list[dict[str, Any]]
) -> tuple[dict[str, Any], PestInputs, Classification, date] | None:
    """Most recent row carrying a real signal, preferring a directly-classified
    pest over a placard-only signal on a date tie (mirrors arcgis.best_signal)."""
    scored = []
    for row in rows:
        pest, classification, d = _row_pest(config, row)
        if pest.kind == "none" or d is None:
            continue
        strength = 2 if classification.pests else 1
        scored.append((d, strength, row, pest, classification))
    if not scored:
        return None
    d, _strength, row, pest, classification = max(scored, key=lambda t: (t[0], t[1]))
    return row, pest, classification, d


def _yolo_header_to_report_header(yolo_facility_id: str, yolo_permit_id: str, yolo_owner: str, yolo_phone: str | None) -> ReportHeader:
    """Adapts reports.YoloHeader's fields onto reports.ReportHeader so fr_push.py
    (which reads `header.owner`/`.phone`/`.is_entity` and is deliberately county-
    agnostic) needs no changes for a Yolo candidate."""
    return ReportHeader(
        owner=yolo_owner, is_entity=is_entity_name(yolo_owner), facility_id=yolo_facility_id, permit_id=yolo_permit_id, phone=yolo_phone
    )


def _group_key(row: dict[str, Any]) -> tuple[str, str]:
    return (ag.base_name(row.get("establishmentName") or ""), _row_street(row).upper())


def group_rows(rows: list[dict[str, Any]], config: CountyConfig) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Keeps only this county's food-inspection rows within the in-scope zip
    allowlist (plan 2.6 -- pools/body-art/other programs and out-of-scope cities
    are dropped here, before any scoring), collapsed by facility."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        if (row.get("programName") or "").strip() != config.food_program:
            continue
        if _row_zip5(row) not in config.zip_allowlist:
            continue
        groups.setdefault(_group_key(row), []).append(row)
    return groups


async def build_candidates(
    rows: list[dict[str, Any]],
    config: CountyConfig,
    *,
    today: date,
    fetcher: PoliteFetcher | None = None,
    circuit: PortalCircuit | None = None,
) -> list[LeadCandidate]:
    """Same shape as pipeline.build_candidates, adapted to the portal's flat search
    rows instead of ArcGIS's Facility/Signal objects -- built directly rather than
    forced through arcgis.Facility, since these counties' permit vocabulary and
    per-row pest signal don't map onto that class's Sacramento-specific fields."""
    groups = group_rows(rows, config)
    out: list[LeadCandidate] = []
    for member_rows in groups.values():
        latest_row = max(member_rows, key=lambda r: _parse_date(r.get("inspectionDate")) or date.min)
        name = " ".join((latest_row.get("establishmentName") or "").split())
        street = _row_street(latest_row)
        city = (latest_row.get("city") or "").strip()
        zip5 = _row_zip5(latest_row)

        base_icp, excluded, permits = _group_best_permit(config, member_rows, name)
        if excluded:
            continue

        latest_inspection = max(
            (d for r in member_rows if (d := _parse_date(r.get("inspectionDate"))) is not None), default=None
        )
        if latest_inspection and (today - latest_inspection).days > ag.MAX_STALE_DAYS:
            continue

        centroid = regions.centroid_for_zip(zip5)
        lat, lng = centroid if centroid else (None, None)
        distance = regions.haversine_miles(lat, lng) if lat is not None else None
        if distance is not None and distance > ag.MAX_MILES:
            continue

        is_chain = ag.is_chain_name(name)
        signal_hit = _best_row_signal(config, member_rows)
        days_since = (today - signal_hit[3]).days if signal_hit else None
        event_eligible = signal_hit is not None and days_since is not None and days_since <= EVENT_SIGNAL_WINDOW_DAYS

        header: ReportHeader | None = None
        email: str | None = None
        email_source: str | None = None
        if config.fetch_pdf and fetcher is not None and not (circuit and circuit.blocked):
            pkey = latest_row.get("inspectionID") or ""
            if pkey:
                text = await fetcher.fetch_text(report_url(config, pkey), pkey)
                if text:
                    yolo_header = parse_yolo_header(text)
                    if yolo_header:
                        header = _yolo_header_to_report_header(
                            yolo_header.facility_id, yolo_header.permit_id, yolo_header.owner, yolo_header.phone
                        )
                        if yolo_header.email:
                            email, email_source = yolo_header.email, "yolo_pdf"

        if event_eligible:
            lane = LANE_CHAIN if is_chain else LANE_EVENT
            _, pest, classification, _ = signal_hit
        elif base_icp >= TERRITORY_ICP_MIN:
            lane = LANE_CHAIN if is_chain else LANE_TERRITORY
            pest = PestInputs(kind="none")
            classification = UNCLASSIFIED
        else:
            continue

        icp = IcpInputs(
            base=base_icp,
            extra_permits=max(len(permits) - 1, 0),
            meat_seafood_keyword=ag.has_meat_seafood_keyword(name),
            bakery_keyword=ag.has_bakery_keyword(name),
        )
        region_id, region_name = regions.region_for_zip(zip5)
        geo = regions.geo_multiplier(distance, region_mapped=region_id != 0)
        result = score_candidate(
            icp,
            pest,
            days_since_signal=days_since if event_eligible else None,
            geo_multiplier=geo,
            has_header_phone=bool(header and header.phone),
            owner_is_person=bool(header and header.owner and not header.is_entity),
        )

        if config is PLACER:
            pr_m = re.search(r"(PR\d+)", latest_row.get("permitName") or "")
            interim_key = pr_m.group(1) if pr_m else (latest_row.get("permitID") or "")
        else:
            interim_key = header.facility_id if header else (latest_row.get("permitID") or "")

        signal = None
        if event_eligible:
            row, _, _, d = signal_hit
            signal = ag.Signal(
                pkey=row.get("inspectionID") or "",
                date=d,
                result=row.get("InspectionOutcome") or "",
                inspection_type=row.get("inspectionType") or row.get("purpose") or "",
                violation_description=row.get("comments"),
                report_url=report_url(config, row.get("inspectionID") or ""),
            )

        out.append(
            LeadCandidate(
                facility_id=interim_key,
                county=config.key,
                customer_link=f"{config.key_prefix}:{interim_key}",
                name=name,
                street=street,
                city=city,
                zip5=zip5,
                lat=lat,
                lng=lng,
                permits=sorted(permits),
                lane=lane,
                score=result,
                classification=classification,
                signal=signal,
                header=header,
                distance_miles=distance,
                region_id=region_id,
                region_name=region_name,
                is_chain=is_chain,
                days_since_signal=days_since if event_eligible else None,
                email=email,
                email_source=email_source,
            )
        )
    return out


async def pull_and_build(
    client: httpx.AsyncClient,
    config: CountyConfig,
    circuit: PortalCircuit,
    *,
    lookback_days: int,
    today: date,
    fetcher: PoliteFetcher | None,
) -> list[LeadCandidate]:
    """Live network pull + build for one county. Callers share one `circuit` (and,
    for Yolo, one `fetcher`) across both counties so a block on either stops the
    rest of the run's portal traffic, per plan section 8."""
    rows = await search_county(client, config, circuit, date_from=today - timedelta(days=lookback_days), date_to=today)
    return await build_candidates(rows, config, today=today, fetcher=fetcher, circuit=circuit)
