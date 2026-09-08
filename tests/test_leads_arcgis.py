"""Address parsing, the ICP permit-type mapping, chain/base-name detection, and
the hard filters -- all pure functions, tested against the real Description
strings and address formats verified live against the Sacramento County feed
(docs/lead-scraper-plan.md sections 2.1-2.2)."""

from __future__ import annotations

from datetime import date

from fr_mcp.leads import arcgis as ag

# --- address parsing -----------------------------------------------------


def test_parses_a_normal_address_with_zip_plus_four():
    p = ag.parse_address("1016 10th St, Sacramento 95814-3502")
    assert p == ag.ParsedAddress(street="1016 10th St", city="Sacramento", zip5="95814")


def test_strips_ca_usa_noise():
    p = ag.parse_address("1103 T Street, Sacramento, CA, USA, Sacramento 95811")
    assert p is not None
    assert p.zip5 == "95811"
    assert "CA, USA" not in p.street


def test_returns_none_for_an_address_with_no_zip():
    assert ag.parse_address("4331 Elkhorn Blvd Ste A, Sacramento") is None


def test_returns_none_for_empty_address():
    assert ag.parse_address("") is None


# --- permit classes --------------------------------------------------------


def test_every_description_from_the_feed_is_mapped():
    # The 20 Description values verified live 2026-09-07 (docs/lead-scraper-plan.md 2.2).
    live_descriptions = [
        "RESTAURANT", "FOOD PREP ESTAB ", "RETAIL MARKET (LESS THAN 6000 SQ FT)",
        "RESTAURANT WITH BAR", "MOBILE FOOD FACILITY CAT D",
        "SCHOOL AND/OR NONPROFIT SENIOR MEAL PROGRAM",
        "RETAIL MARKET (25SQFT<300SQFT PRE PKG NON-PHF", "RETAIL MARKET (15000+SQ.FT)",
        "BAR", "SCHOOL SATELLITE FACILITY - EACH FACILITY", "RETAIL MARKET (6000-14999 SQ.FT.)",
        "LICENSED HEALTH CARE FACILITY", "COMMISSARY", "CERTIFIED FARMERS' MARKET",
        "SATELLITE FOOD DISTRIBUTION FACILITY", "VETERAN'S ORGANIZATION FOOD FACILITY",
        "RESTRICTED FOOD SERVICE ESTABLISHMENT", "BAKERY--NO PREPARATION", "PRODUCE STAND", "FARM STAND",
    ]
    for desc in live_descriptions:
        cls = ag.permit_class(desc)
        assert cls != ag.PermitClass(0, excluded=True) or desc.strip() in (
            "MOBILE FOOD FACILITY CAT D", "SCHOOL AND/OR NONPROFIT SENIOR MEAL PROGRAM",
            "SCHOOL SATELLITE FACILITY - EACH FACILITY", "RETAIL MARKET (25SQFT<300SQFT PRE PKG NON-PHF",
            "BAR", "CERTIFIED FARMERS' MARKET", "VETERAN'S ORGANIZATION FOOD FACILITY",
            "RESTRICTED FOOD SERVICE ESTABLISHMENT", "PRODUCE STAND", "FARM STAND",
        ), f"{desc!r} fell through to the excluded default -- key mismatch?"


def test_food_prep_estab_trailing_space_is_handled():
    # The raw feed value has a trailing space; the mapping key doesn't (regression
    # test for a real bug caught while building this: stripping only on one side
    # of the lookup silently excluded the second-largest permit category).
    assert ag.permit_class("FOOD PREP ESTAB ").excluded is False
    assert ag.permit_class("FOOD PREP ESTAB ").base_icp == 8


def test_unknown_description_is_excluded_by_default():
    assert ag.permit_class("SOMETHING NEW THE COUNTY ADDED").excluded is True


def test_small_market_keyword_bump():
    cls = ag.permit_class("RETAIL MARKET (LESS THAN 6000 SQ FT)")
    assert ag.effective_icp_base(cls, "ARCO AM/PM") == 12
    assert ag.effective_icp_base(cls, "CHHUN SUPERMARKET") == 24
    assert ag.effective_icp_base(cls, "ALIBABA HALAL FOOD MARKET") == 24


def test_food_prep_keyword_bump():
    cls = ag.permit_class("FOOD PREP ESTAB")
    assert ag.effective_icp_base(cls, "7 ELEVEN") == 8
    assert ag.effective_icp_base(cls, "VALERIO'S TROPICAL BAKE SHOP") == 18


# --- chain / base-name detection --------------------------------------------


def test_base_name_strips_store_numbers():
    assert ag.base_name("7 ELEVEN #14064C") == "7 ELEVEN"
    assert ag.base_name("7 ELEVEN 24276F") == "7 ELEVEN"
    assert ag.base_name("KFC/ A&W #181") == "KFC/ A&W"
    assert ag.base_name("99 RANCH MARKET #86") == "99 RANCH MARKET"
    assert ag.base_name("99 RANCH MARKET") == "99 RANCH MARKET"


def test_is_chain_name_matches_national_chains_not_independents():
    assert ag.is_chain_name("COSTCO WHOLESALE #1371") is True
    assert ag.is_chain_name("7 ELEVEN #14064C") is True
    assert ag.is_chain_name("99 RANCH MARKET") is False
    assert ag.is_chain_name("LA SUPERIOR # 2") is False


# --- normalize + signals ----------------------------------------------------


def _row(fid: str, name: str, address: str, description: str, *, lat=38.6, lng=-121.5, date_ms=1700000000000) -> dict:
    return {
        "attributes": {
            "Facility_ID": fid, "Facility_Name": name, "Facility_Address": address,
            "Description": description, "Inspection_Service": "INSPECTION", "Inspection_Type": "ROUTINE",
            "Inspection_Result": "PASS", "Inspection_Date": date_ms,
            "Inspection_Report": f"https://x/print/?pKey={fid}0000-0000-0000-0000-000000000000", "Violation_Description": None,
        },
        "geometry": {"x": lng, "y": lat},
    }


def test_normalize_layer0_builds_one_facility_per_row():
    rows = [_row("FA1", "TEST MARKET", "1 Main St, Sacramento 95814", "RETAIL MARKET (15000+SQ.FT)")]
    facs = ag.normalize_layer0(rows)
    assert set(facs) == {"FA1"}
    f = facs["FA1"]
    assert f.name == "TEST MARKET"
    assert f.zip5 == "95814"
    assert f.lat == 38.6 and f.lng == -121.5
    assert f.permits == {"RETAIL MARKET (15000+SQ.FT)"}


def test_normalize_layer0_keeps_the_latest_report_url_and_its_pkey():
    row = _row("FA1", "TEST MARKET", "1 Main St, Sacramento 95814", "RETAIL MARKET (15000+SQ.FT)")
    url = "https://inspections.myhealthdepartment.com/sacramento/print/?task=getPrintable&path=sacramento&pKey=D6DAA211-5335-4C15-AE99-FA1F380507BA"
    row["attributes"]["Inspection_Report"] = url
    f = ag.normalize_layer0([row])["FA1"]
    assert f.latest_report_url == url
    assert f.latest_pkey == "D6DAA211-5335-4C15-AE99-FA1F380507BA"


def test_attach_signals_collapses_shared_pkey_and_extends_permits():
    rows = [_row("FA1", "TEST MARKET", "1 Main St, Sacramento 95814", "RETAIL MARKET (15000+SQ.FT)")]
    facs = ag.normalize_layer0(rows)
    guid = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"
    history = [
        {
            "attributes": {
                "Facility_ID": "FA1", "Description": "BAKERY--NO PREPARATION", "Inspection_Result": "PASS",
                "Inspection_Type": "ROUTINE", "Inspection_Date": 1700000000000,
                "Violation_Description": "VERMIN AND ANIMAL CONTAMINATION",
                "Inspection_Report": f"https://x/print/?pKey={guid}",
            }
        },
        {
            # Same pKey, a different permit on the same facility -- must not double the signal.
            "attributes": {
                "Facility_ID": "FA1", "Description": "RETAIL MARKET (15000+SQ.FT)", "Inspection_Result": "PASS",
                "Inspection_Type": "ROUTINE", "Inspection_Date": 1700000000000,
                "Violation_Description": "VERMIN AND ANIMAL CONTAMINATION",
                "Inspection_Report": f"https://x/print/?pKey={guid}",
            }
        },
    ]
    ag.attach_signals(facs, history)
    f = facs["FA1"]
    assert f.permits == {"RETAIL MARKET (15000+SQ.FT)", "BAKERY--NO PREPARATION"}
    assert len(f.signals) == 1
    assert f.extra_permit_count == 1


def test_attach_signals_skips_attempted_inspections():
    rows = [_row("FA1", "TEST MARKET", "1 Main St, Sacramento 95814", "RESTAURANT")]
    facs = ag.normalize_layer0(rows)
    history = [
        {
            "attributes": {
                "Facility_ID": "FA1", "Description": "RESTAURANT", "Inspection_Result": "NOT APPLICABLE",
                "Inspection_Type": "ATTEMPTED - UNABLE TO INSPECT", "Inspection_Date": 1700000000000,
                "Violation_Description": "VERMIN AND ANIMAL CONTAMINATION",
                "Inspection_Report": "https://x/print/?pKey=11111111-1111-1111-1111-111111111111",
            }
        }
    ]
    ag.attach_signals(facs, history)
    assert facs["FA1"].signals == []


def test_best_signal_prefers_vermin_over_a_same_date_critical_only_row():
    rows = [_row("FA1", "TEST", "1 Main St, Sacramento 95814", "RESTAURANT")]
    facs = ag.normalize_layer0(rows)
    d = date(2026, 8, 1)
    facs["FA1"].signals = [
        ag.Signal("p1", d, "CRITICAL VIOLATIONS", "ROUTINE", None, "https://x/1"),
        ag.Signal("p2", d, "PASS", "ROUTINE", "VERMIN AND ANIMAL CONTAMINATION", "https://x/2"),
    ]
    best = ag.best_signal(facs["FA1"])
    assert best.pkey == "p2"


def test_apply_vermin_counts():
    rows = [_row("FA1", "TEST", "1 Main St, Sacramento 95814", "RESTAURANT")]
    facs = ag.normalize_layer0(rows)
    ag.apply_vermin_counts(facs, {"FA1": 4, "FA_UNKNOWN": 9})
    assert facs["FA1"].vermin_count_24mo == 4


# --- hard filters ------------------------------------------------------


def test_hard_filter_excludes_excluded_permit_types():
    rows = [_row("FA1", "SOME BAR", "1 Main St, Sacramento 95814", "BAR")]
    facs = ag.normalize_layer0(rows)
    assert ag.hard_filter_reason(facs["FA1"], today=date(2026, 9, 7), distance_miles=1.0) is not None


def test_hard_filter_excludes_stale_facilities():
    rows = [_row("FA1", "OLD PLACE", "1 Main St, Sacramento 95814", "RESTAURANT")]
    facs = ag.normalize_layer0(rows)
    facs["FA1"].latest_inspection = date(2020, 1, 1)
    reason = ag.hard_filter_reason(facs["FA1"], today=date(2026, 9, 7), distance_miles=1.0)
    assert reason is not None and "stale" in reason


def test_hard_filter_excludes_far_facilities():
    # Recent inspection (2026-08-01) so the distance check, not staleness, is what fires.
    rows = [_row("FA1", "FAR PLACE", "1 Main St, Sacramento 95814", "RESTAURANT", date_ms=1785542400000)]
    facs = ag.normalize_layer0(rows)
    reason = ag.hard_filter_reason(facs["FA1"], today=date(2026, 9, 7), distance_miles=50.0)
    assert reason is not None and "35 miles" in reason


def test_hard_filter_passes_a_normal_nearby_recent_facility():
    rows = [_row("FA1", "GOOD PLACE", "1 Main St, Sacramento 95814", "RESTAURANT")]
    facs = ag.normalize_layer0(rows)
    facs["FA1"].latest_inspection = date(2026, 8, 1)
    assert ag.hard_filter_reason(facs["FA1"], today=date(2026, 9, 7), distance_miles=5.0) is None
