"""Pure scoring for a lead candidate. No I/O -- takes plain values, returns a
plain result, so it can be tuned and unit-tested without touching FieldRoutes
or the county feed.

Plan reference: docs/lead-scraper-plan.md section 3.2. The section header
reads "Score = (icp_fit + pest_signal + recency + reachability) x geo, max
100" -- the "max 100" clamp applies to that total, not to each sub-score
individually (several of the plan's own additive bonuses can push a
sub-score past its nominal ceiling, e.g. icp_fit's extra-permit and keyword
bonuses on top of a 30-point base); implementing it any other way would
silently disagree with the plan's own worked examples.
"""

from __future__ import annotations

from dataclasses import dataclass

HOT, WARM, COOL, PARK = "hot", "warm", "cool", "park"

# vermin_unclassified (a VERMIN category with no pest identified yet, e.g. the PDF
# hasn't been fetched) never outranks a confirmed infestation.
UNCLASSIFIED_SCORE_CAP = 65

_PEST_BASE = {
    "rodent": 40,
    "rodent + cockroach": 40,
    "cockroach": 36,
    "other_insect": 20,
    "unclassified": 22,  # "vermin_unclassified" in the plan's language
}


@dataclass(frozen=True)
class IcpInputs:
    base: int  # from the county permit-type mapping (2.2 / 2.6), 0 if excluded
    extra_permits: int = 0  # additional permits under the same facility, beyond the first
    meat_seafood_keyword: bool = False
    bakery_keyword: bool = False
    warehouse_dept_string: bool = False
    local_multi_location: bool = False


@dataclass(frozen=True)
class PestInputs:
    # "rodent" | "rodent + cockroach" | "cockroach" | "other_insect" | "unclassified"
    # | "closure" (non-vermin closure/suspension) | "critical" (critical/conditional,
    # no vermin) | "none" (no signal at all -- territory-lane candidates use this).
    kind: str = "none"
    closed_or_suspended: bool = False
    repeat_vermin_24mo: int = 0  # additional vermin-flagged inspections in 24 months
    no_pco: bool = False
    live_evidence: bool = False


@dataclass(frozen=True)
class ScoreResult:
    icp_fit: int
    pest_signal: int
    recency: int
    reachability: int
    geo: float
    total: int
    tier: str
    capped_unclassified: bool = False


def icp_fit(inputs: IcpInputs) -> int:
    score = inputs.base
    score += min(inputs.extra_permits, 2) * 5  # "+5 per extra permit (cap +10)"
    if inputs.meat_seafood_keyword:
        score += 6
    if inputs.bakery_keyword:
        score += 4
    if inputs.warehouse_dept_string:
        score += 3
    if inputs.local_multi_location:
        score += 4
    return max(score, 0)


def pest_signal(inputs: PestInputs) -> int:
    if inputs.kind == "closure":
        base = 8
    elif inputs.kind == "critical":
        base = 4
    elif inputs.kind == "none":
        base = 0
    else:
        base = _PEST_BASE.get(inputs.kind, 0)
    bonus = 0
    if inputs.closed_or_suspended:
        bonus += 8
    bonus += min(inputs.repeat_vermin_24mo, 2) * 6  # "+6 per additional ... cap +12"
    if inputs.no_pco:
        bonus += 6
    if inputs.live_evidence:
        bonus += 3
    return max(base + bonus, 0)


def recency_score(days_since_signal: int | None) -> int:
    if days_since_signal is None:
        return 0
    if days_since_signal <= 7:
        return 10
    if days_since_signal <= 30:
        return 8
    if days_since_signal <= 90:
        return 5
    if days_since_signal <= 180:
        return 2
    return 0


def reachability_score(
    *, has_header_phone: bool = False, owner_is_person: bool = False, has_places_contact: bool = False
) -> int:
    score = 0
    if has_header_phone:
        score += 5
    if owner_is_person:
        score += 3
    if has_places_contact:
        score += 2
    return score


def tier_for(total: int) -> str:
    if total >= 70:
        return HOT
    if total >= 50:
        return WARM
    if total >= 30:
        return COOL
    return PARK


def score_candidate(
    icp: IcpInputs,
    pest: PestInputs,
    *,
    days_since_signal: int | None,
    geo_multiplier: float,
    has_header_phone: bool = False,
    owner_is_person: bool = False,
    has_places_contact: bool = False,
) -> ScoreResult:
    icp_s = icp_fit(icp)
    pest_s = pest_signal(pest)
    rec_s = recency_score(days_since_signal)
    reach_s = reachability_score(
        has_header_phone=has_header_phone, owner_is_person=owner_is_person, has_places_contact=has_places_contact
    )
    total = round((icp_s + pest_s + rec_s + reach_s) * geo_multiplier)
    total = max(0, min(total, 100))
    capped = False
    if pest.kind == "unclassified" and total > UNCLASSIFIED_SCORE_CAP:
        total = UNCLASSIFIED_SCORE_CAP
        capped = True
    return ScoreResult(
        icp_fit=icp_s,
        pest_signal=pest_s,
        recency=rec_s,
        reachability=reach_s,
        geo=geo_multiplier,
        total=total,
        tier=tier_for(total),
        capped_unclassified=capped,
    )
