"""build_candidates / rank: lane assignment (event/territory/chain), the
territory-lane pushable-despite-a-low-tier fix, and ranking order."""

from __future__ import annotations

import asyncio
from datetime import date

from fr_mcp.leads import arcgis as ag
from fr_mcp.leads import pipeline as pl

TODAY = date(2026, 9, 7)


def _facility(fid, name, description, *, zip5="95814", lat=38.68, lng=-121.45, latest=date(2026, 8, 1)) -> ag.Facility:
    return ag.Facility(
        facility_id=fid, name=name, street="1 Main St", city="Sacramento", zip5=zip5,
        lat=lat, lng=lng, permits={description}, latest_inspection=latest, latest_result="PASS",
    )


def _run(facilities: dict[str, ag.Facility]) -> list[pl.LeadCandidate]:
    return asyncio.run(pl.build_candidates(facilities, today=TODAY, fetcher=None))


def test_a_facility_with_a_recent_vermin_signal_is_event_lane():
    fac = _facility("FA1", "LOCAL MARKET", "RETAIL MARKET (LESS THAN 6000 SQ FT)")
    fac.signals.append(ag.Signal("p1", date(2026, 9, 1), "MINOR VIOLATIONS", "ROUTINE", "VERMIN AND ANIMAL CONTAMINATION", "https://x/1"))
    candidates = _run({"FA1": fac})
    assert len(candidates) == 1
    assert candidates[0].lane == pl.LANE_EVENT
    assert candidates[0].pushable is True


def test_a_signal_older_than_180_days_falls_back_to_territory_or_chain():
    fac = _facility("FA1", "LOCAL MARKET", "RETAIL MARKET (15000+SQ.FT)")
    fac.signals.append(ag.Signal("p1", date(2025, 1, 1), "MINOR VIOLATIONS", "ROUTINE", "VERMIN AND ANIMAL CONTAMINATION", "https://x/1"))
    candidates = _run({"FA1": fac})
    assert len(candidates) == 1
    assert candidates[0].lane == pl.LANE_TERRITORY  # icp_fit 30 >= 24, not a chain
    assert candidates[0].signal is None  # stale signal isn't carried into a territory candidate


def test_independent_icp_a_with_no_signal_is_territory_and_always_pushable():
    fac = _facility("FA1", "INDEPENDENT MARKET", "RETAIL MARKET (6000-14999 SQ.FT.)", lat=38.9, lng=-121.8)
    candidates = _run({"FA1": fac})
    assert len(candidates) == 1
    c = candidates[0]
    assert c.lane == pl.LANE_TERRITORY
    # Far enough that geo drags the total under 30 ("park" by label) -- must still push.
    assert c.pushable is True


def test_low_icp_facility_with_no_signal_is_dropped_entirely():
    fac = _facility("FA1", "SMALL PLACE", "RESTAURANT")  # base 12, below the 24 territory gate
    candidates = _run({"FA1": fac})
    assert candidates == []


def test_excluded_permit_type_is_dropped_entirely():
    fac = _facility("FA1", "SOME BAR", "BAR")
    candidates = _run({"FA1": fac})
    assert candidates == []


def test_chain_with_no_signal_but_icp_a_is_parked_not_territory():
    fac = _facility("FA1", "COSTCO WHOLESALE #123", "RETAIL MARKET (15000+SQ.FT)")
    candidates = _run({"FA1": fac})
    assert len(candidates) == 1
    assert candidates[0].lane == pl.LANE_CHAIN
    assert candidates[0].pushable is False


def test_chain_below_the_territory_gate_is_dropped_not_parked():
    fac = _facility("FA1", "7 ELEVEN #999", "FOOD PREP ESTAB")  # base 8, below 24
    candidates = _run({"FA1": fac})
    assert candidates == []


def test_five_or_more_same_base_name_locations_count_as_a_chain():
    facs = {
        f"FA{i}": _facility(f"FA{i}", f"REGIONAL CHAIN #{i}", "RETAIL MARKET (15000+SQ.FT)")
        for i in range(1, 6)
    }
    candidates = _run(facs)
    assert all(c.lane == pl.LANE_CHAIN for c in candidates)


def test_two_to_four_same_base_name_locations_get_the_local_multi_location_bonus():
    facs = {
        f"FA{i}": _facility(f"FA{i}", f"LOCAL MARKET CHAIN #{i}", "RETAIL MARKET (6000-14999 SQ.FT.)")
        for i in range(1, 3)
    }
    candidates = _run(facs)
    assert len(candidates) == 2
    for c in candidates:
        assert c.lane == pl.LANE_TERRITORY
        assert c.score.icp_fit == 30 + 4  # base + local_multi_location bonus


def test_rank_orders_event_before_territory_before_chain_and_hot_before_warm():
    hot = _facility("FA1", "HOT MARKET", "RETAIL MARKET (15000+SQ.FT)")
    hot.signals.append(ag.Signal("p1", date(2026, 9, 5), "CLOSED", "ROUTINE", "VERMIN AND ANIMAL CONTAMINATION", "https://x/1"))
    hot.vermin_count_24mo = 3
    warm = _facility("FA2", "WARM SPOT", "RESTAURANT", zip5="95814")
    warm.signals.append(ag.Signal("p2", date(2026, 8, 20), "MINOR VIOLATIONS", "ROUTINE", "VERMIN AND ANIMAL CONTAMINATION", "https://x/2"))
    territory = _facility("FA3", "TERRITORY MARKET", "RETAIL MARKET (15000+SQ.FT)", lat=38.9, lng=-121.9)
    chain = _facility("FA4", "COSTCO WHOLESALE #1", "RETAIL MARKET (15000+SQ.FT)")
    candidates = _run({"FA1": hot, "FA2": warm, "FA3": territory, "FA4": chain})
    ranked = pl.rank(candidates)
    lanes = [c.lane for c in ranked]
    assert lanes.index(pl.LANE_EVENT) < lanes.index(pl.LANE_TERRITORY) < lanes.index(pl.LANE_CHAIN)
    event_rows = [c for c in ranked if c.lane == pl.LANE_EVENT]
    assert event_rows[0].facility_id == "FA1"  # hot before warm within the event lane
