"""Pull, normalise, filter, classify and score -- the read-only half of the
pipeline. `fr_push.py` is the write half; nothing in this module touches
FieldRoutes, so it's cheap to run in `preview`/`--dry-run` mode.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import httpx

from . import arcgis as ag
from . import regions
from .classify import UNCLASSIFIED, Classification
from .reports import ParsedReport, PoliteFetcher, ReportHeader, parse_report
from .score import IcpInputs, PestInputs, ScoreResult, score_candidate

SOURCE_PREFIX = "SACEMD"
EVENT_SIGNAL_WINDOW_DAYS = 180  # plan 3.5: "an unhandled ... inspection within 180 days"
TERRITORY_ICP_MIN = 24  # plan 3.5: "independent ICP-A facilities (icp_fit 24 or more)"
LOCAL_MULTI_LOCATION_RANGE = (2, 4)
LANE_EVENT = "event"
LANE_TERRITORY = "territory"
LANE_CHAIN = "chain"  # parked: scored for visibility, never pushed


@dataclass
class LeadCandidate:
    facility_id: str
    county: str  # "Sacramento" | "Placer" | "Yolo" -- which adapter produced this row
    customer_link: str
    name: str
    street: str
    city: str
    zip5: str
    lat: float | None
    lng: float | None
    permits: list[str]
    lane: str
    score: ScoreResult
    classification: Classification
    signal: ag.Signal | None
    header: ReportHeader | None
    distance_miles: float | None
    region_id: int
    region_name: str | None
    is_chain: bool
    days_since_signal: int | None
    # Only Yolo's report PDF carries an email at signal time (plan 6.1); Sacramento and
    # Placer leave this for phase C enrichment (Places/website/ZoomInfo/finder).
    email: str | None = None
    email_source: str | None = None

    @property
    def pushable(self) -> bool:
        """Event lane: anything but Park (Park means "no real signal, skip"). Territory
        lane: always, once it cleared the icp_fit >= 24 gate at build time -- it has no
        pest signal to score, so a low total (and therefore a low tier label) doesn't
        mean skip it, the way it does for an event-lane candidate. Chains never push."""
        if self.lane == LANE_CHAIN:
            return False
        if self.lane == LANE_TERRITORY:
            return True
        return self.score.tier != "park"


def _pest_inputs_for(facility: ag.Facility, signal: ag.Signal | None, classification: Classification) -> PestInputs:
    if signal is None:
        return PestInputs(kind="none")
    if signal.is_vermin:
        kind = classification.label if classification.label != "unclassified" else "unclassified"
        return PestInputs(
            kind=kind,
            closed_or_suspended=signal.is_closure,
            repeat_vermin_24mo=facility.vermin_count_24mo,
            no_pco=classification.no_pco,
            live_evidence=classification.live_evidence,
        )
    if signal.is_closure:
        return PestInputs(kind="closure", closed_or_suspended=True, repeat_vermin_24mo=facility.vermin_count_24mo)
    if signal.is_critical:
        return PestInputs(kind="critical", repeat_vermin_24mo=facility.vermin_count_24mo)
    return PestInputs(kind="none")


def _icp_inputs_for(facility: ag.Facility, *, local_multi_location: bool) -> IcpInputs:
    cls = facility.best_permit_class
    base = ag.effective_icp_base(cls, facility.name)
    return IcpInputs(
        base=base,
        extra_permits=facility.extra_permit_count,
        meat_seafood_keyword=ag.has_meat_seafood_keyword(facility.name),
        bakery_keyword=ag.has_bakery_keyword(facility.name),
        local_multi_location=local_multi_location,
    )


def _base_name_counts(facilities: dict[str, ag.Facility]) -> Counter:
    return Counter(ag.base_name(f.name) for f in facilities.values())


async def build_candidates(
    facilities: dict[str, ag.Facility],
    *,
    today: date,
    fetcher: PoliteFetcher | None = None,
) -> list[LeadCandidate]:
    """Score every facility that survives the hard filters into an event-lane,
    territory-lane, or parked-chain candidate. `fetcher`, when given, is used
    to read one inspection-report PDF per pushable candidate: it is the only
    place the owner's name and phone live (the feed has neither), so an event-
    lane candidate fetches its signal's report and a territory-lane candidate
    fetches its latest routine one. Vermin signals additionally get their
    narrative classified from that same fetch. None means "no network": vermin
    signals score as vermin_unclassified and no candidate gets a phone -- used
    by `preview --no-fetch` and tests.
    """
    counts = _base_name_counts(facilities)
    out: list[LeadCandidate] = []
    for facility in facilities.values():
        distance = (
            regions.haversine_miles(facility.lat, facility.lng)
            if facility.lat is not None and facility.lng is not None
            else None
        )
        if ag.hard_filter_reason(facility, today=today, distance_miles=distance) is not None:
            continue

        base = ag.base_name(facility.name)
        is_chain = ag.is_chain_name(facility.name) or counts[base] >= 5
        local_multi = (not is_chain) and LOCAL_MULTI_LOCATION_RANGE[0] <= counts[base] <= LOCAL_MULTI_LOCATION_RANGE[1]

        signal = ag.best_signal(facility)
        days_since = (today - signal.date).days if signal else None
        event_eligible = signal is not None and days_since is not None and days_since <= EVENT_SIGNAL_WINDOW_DAYS

        icp = _icp_inputs_for(facility, local_multi_location=local_multi)
        region_id, region_name = regions.region_for_zip(facility.zip5)
        geo = regions.geo_multiplier(distance, region_mapped=region_id != 0)

        if event_eligible:
            lane = LANE_CHAIN if is_chain else LANE_EVENT
            report_url, report_pkey = signal.report_url, signal.pkey
        elif icp.base >= TERRITORY_ICP_MIN:
            # Would have been a territory-lane prospect if it weren't a chain -- keep it
            # recorded (never pushed) so a future corporate play has the list ready;
            # a chain facility below the ICP-A bar (a 7-Eleven, a gas station) is just
            # dropped, same as any other facility with no signal and low icp_fit.
            lane = LANE_CHAIN if is_chain else LANE_TERRITORY
            report_url, report_pkey = facility.latest_report_url, facility.latest_pkey
        else:
            continue  # no lane: not a fresh signal, not ICP-A enough for the territory lane

        classification = UNCLASSIFIED
        header: ReportHeader | None = None
        # Chains are never pushed, so their report isn't worth a request against the portal.
        if fetcher is not None and lane != LANE_CHAIN and report_pkey:
            text = await fetcher.fetch_text(report_url, report_pkey)
            if text:
                parsed: ParsedReport = parse_report(text)
                header = parsed.header
                if event_eligible and signal.is_vermin:
                    classification = parsed.classification

        pest = _pest_inputs_for(facility, signal, classification) if event_eligible else PestInputs(kind="none")

        result = score_candidate(
            icp,
            pest,
            days_since_signal=days_since if event_eligible else None,
            geo_multiplier=geo,
            has_header_phone=bool(header and header.phone),
            owner_is_person=bool(header and not header.is_entity),
        )
        out.append(
            LeadCandidate(
                facility_id=facility.facility_id,
                county="Sacramento",
                customer_link=f"{SOURCE_PREFIX}:{facility.facility_id}",
                name=facility.name,
                street=facility.street,
                city=facility.city,
                zip5=facility.zip5,
                lat=facility.lat,
                lng=facility.lng,
                permits=sorted(facility.permits),
                lane=lane,
                score=result,
                classification=classification,
                signal=signal if event_eligible else None,
                header=header,
                distance_miles=distance,
                region_id=region_id,
                region_name=region_name,
                is_chain=is_chain,
                days_since_signal=days_since if event_eligible else None,
            )
        )
    return out


def rank(candidates: list[LeadCandidate]) -> list[LeadCandidate]:
    """Event lane first (Hot before Warm before Cool), then the territory lane
    ordered by distance, then recency of inspection (plan 3.5)."""

    def event_key(c: LeadCandidate) -> tuple:
        tier_rank = {"hot": 0, "warm": 1, "cool": 2, "park": 3}[c.score.tier]
        return (tier_rank, -c.score.total)

    def territory_key(c: LeadCandidate) -> tuple:
        return (c.distance_miles if c.distance_miles is not None else 999, -c.score.total)

    events = sorted((c for c in candidates if c.lane == LANE_EVENT), key=event_key)
    territory = sorted((c for c in candidates if c.lane == LANE_TERRITORY), key=territory_key)
    chains = [c for c in candidates if c.lane == LANE_CHAIN]
    return events + territory + chains


async def pull_and_normalize(
    client: httpx.AsyncClient, *, lookback_days: int, today: date
) -> dict[str, ag.Facility]:
    """Live network pull: full layer-0 facilities plus recent layer-1 signals and
    the 24-month vermin repeat count. Split out from `build_candidates` so tests
    can call `build_candidates` directly against hand-built `Facility` objects."""
    layer0 = await ag.fetch_layer0_full(client)
    facilities = ag.normalize_layer0(layer0)
    since = today - timedelta(days=lookback_days)
    history = await ag.fetch_layer1_since(client, since)
    ag.attach_signals(facilities, history)
    counts = await ag.fetch_vermin_counts(client, today - timedelta(days=730))
    ag.apply_vermin_counts(facilities, counts)
    return facilities
