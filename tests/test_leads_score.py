"""Table-driven tests for the pure scoring model (docs/lead-scraper-plan.md 3.2)."""

from __future__ import annotations

import pytest

from fr_mcp.leads.score import (
    COOL,
    HOT,
    PARK,
    UNCLASSIFIED_SCORE_CAP,
    WARM,
    IcpInputs,
    PestInputs,
    icp_fit,
    pest_signal,
    reachability_score,
    recency_score,
    score_candidate,
    tier_for,
)


def test_icp_fit_base_only():
    assert icp_fit(IcpInputs(base=30)) == 30


def test_icp_fit_extra_permits_cap_at_two():
    assert icp_fit(IcpInputs(base=12, extra_permits=1)) == 17  # +5
    assert icp_fit(IcpInputs(base=12, extra_permits=2)) == 22  # +10
    assert icp_fit(IcpInputs(base=12, extra_permits=5)) == 22  # capped, not +25


def test_icp_fit_keyword_bonuses_stack():
    v = icp_fit(IcpInputs(base=12, meat_seafood_keyword=True, bakery_keyword=True, local_multi_location=True))
    assert v == 12 + 6 + 4 + 4


@pytest.mark.parametrize(
    "kind,expected_base",
    [
        ("rodent", 40),
        ("rodent + cockroach", 40),
        ("cockroach", 36),
        ("other_insect", 20),
        ("unclassified", 22),
        ("closure", 8),
        ("critical", 4),
        ("none", 0),
    ],
)
def test_pest_signal_base_by_kind(kind, expected_base):
    assert pest_signal(PestInputs(kind=kind)) == expected_base


def test_pest_signal_modifiers():
    p = pest_signal(PestInputs(kind="rodent", closed_or_suspended=True, no_pco=True, live_evidence=True))
    assert p == 40 + 8 + 6 + 3


def test_pest_signal_repeat_offender_caps_at_two_bonuses():
    assert pest_signal(PestInputs(kind="rodent", repeat_vermin_24mo=1)) == 40 + 6
    assert pest_signal(PestInputs(kind="rodent", repeat_vermin_24mo=2)) == 40 + 12
    assert pest_signal(PestInputs(kind="rodent", repeat_vermin_24mo=9)) == 40 + 12  # capped, not +54


@pytest.mark.parametrize(
    "days,expected",
    [(0, 10), (7, 10), (8, 8), (30, 8), (31, 5), (90, 5), (91, 2), (180, 2), (181, 0), (None, 0)],
)
def test_recency_score_steps(days, expected):
    assert recency_score(days) == expected


def test_reachability_score():
    assert reachability_score() == 0
    assert reachability_score(has_header_phone=True) == 5
    assert reachability_score(owner_is_person=True) == 3
    assert reachability_score(has_places_contact=True) == 2
    assert reachability_score(has_header_phone=True, owner_is_person=True, has_places_contact=True) == 10


@pytest.mark.parametrize("total,expected", [(0, PARK), (29, PARK), (30, COOL), (49, COOL), (50, WARM), (69, WARM), (70, HOT), (100, HOT)])
def test_tier_for_thresholds(total, expected):
    assert tier_for(total) == expected


def test_score_candidate_clamps_to_100():
    icp = IcpInputs(base=30, extra_permits=5, meat_seafood_keyword=True, bakery_keyword=True, local_multi_location=True)
    pest = PestInputs(kind="rodent", closed_or_suspended=True, repeat_vermin_24mo=9, no_pco=True, live_evidence=True)
    result = score_candidate(icp, pest, days_since_signal=1, geo_multiplier=1.0, has_header_phone=True, owner_is_person=True)
    assert result.total <= 100


def test_score_candidate_geo_multiplier_scales_the_total():
    icp, pest = IcpInputs(base=30), PestInputs(kind="rodent")
    close = score_candidate(icp, pest, days_since_signal=1, geo_multiplier=1.0)
    far = score_candidate(icp, pest, days_since_signal=1, geo_multiplier=0.5)
    assert far.total < close.total
    assert far.total == round(close.total * 0.5)


def test_unclassified_vermin_is_capped_below_a_confirmed_infestation():
    icp = IcpInputs(base=30)
    unclassified = score_candidate(icp, PestInputs(kind="unclassified", closed_or_suspended=True), days_since_signal=1, geo_multiplier=1.0)
    rodent = score_candidate(icp, PestInputs(kind="rodent", closed_or_suspended=True), days_since_signal=1, geo_multiplier=1.0)
    assert unclassified.total <= UNCLASSIFIED_SCORE_CAP
    assert unclassified.capped_unclassified is True
    assert unclassified.total < rodent.total
    assert rodent.capped_unclassified is False


def test_cockroach_is_not_capped_and_can_reach_hot():
    icp = IcpInputs(base=14)
    result = score_candidate(
        icp,
        PestInputs(kind="cockroach", closed_or_suspended=True, live_evidence=True),
        days_since_signal=6,
        geo_multiplier=1.0,
    )
    assert result.tier == HOT
    assert result.total > UNCLASSIFIED_SCORE_CAP


def test_territory_lane_style_candidate_no_signal_scores_from_icp_alone():
    result = score_candidate(IcpInputs(base=30), PestInputs(kind="none"), days_since_signal=None, geo_multiplier=1.0)
    assert result.icp_fit == 30
    assert result.pest_signal == 0
    assert result.recency == 0
    assert result.total == 30
