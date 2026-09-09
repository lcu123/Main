"""Institutional/medical/residential classification and the red row marking.

The false positives here are the point. These words are common in street names
and brand names, and both cases below were real rows on the live sheet, not
hypotheticals.
"""

from __future__ import annotations

from datetime import date

from fr_mcp.leads import institutions, sheet

TODAY = date(2026, 9, 9)


# --- classification ------------------------------------------------------


def test_the_permit_beats_the_name_when_the_county_has_filed_one():
    """A county that bothers to file something as a licensed health-care facility
    is more reliable than any reading of its trading name. ELK GROVE POST ACUTE is
    a real row that carries it."""
    assert institutions.classify("ELK GROVE POST ACUTE", ["LICENSED HEALTH CARE FACILITY"]) == institutions.CLASS_CARE
    # Even a name that says nothing at all.
    assert institutions.classify("Oakmont Kitchen", ["LICENSED HEALTH CARE FACILITY"]) == institutions.CLASS_CARE


def test_a_hospital_is_medical_even_when_its_permit_is_a_food_one():
    """MERCY SAN JUAN HOSPITAL-LHCF is filed under a satellite food-distribution
    permit -- the cafeteria's permit, not the hospital's."""
    got = institutions.classify("MERCY SAN JUAN HOSPITAL-LHCF", ["SATELLITE FOOD DISTRIBUTION FACILITY"])
    assert got == institutions.CLASS_MEDICAL


def test_each_class_is_recognised():
    assert institutions.classify("Sunrise Memory Care") == institutions.CLASS_CARE
    assert institutions.classify("Eskaton Assisted Living") == institutions.CLASS_CARE
    assert institutions.classify("Kaiser Permanente Medical Center") == institutions.CLASS_MEDICAL
    assert institutions.classify("Northgate Dialysis") == institutions.CLASS_MEDICAL
    assert institutions.classify("Riverbank Apartments") == institutions.CLASS_RESIDENTIAL
    assert institutions.classify("Natomas Unified School District") == institutions.CLASS_SCHOOL


def test_a_street_name_is_not_a_care_home():
    """ELDER CREEK MARKET is a market on Elder Creek Road."""
    assert institutions.classify("ELDER CREEK MARKET", ["RETAIL MARKET (LESS THAN 6000 SQ FT)"]) is None


def test_a_skincare_brand_is_not_a_clinic():
    """"Lira Clinical Store of Sacramento" -- "clinical" is not "clinic"."""
    assert institutions.classify("Lira Clinical Store of Sacramento") is None


def test_a_bare_care_or_living_word_does_not_match_on_its_own():
    """Otherwise "Urgent Care Deli" and half the catering trade come along."""
    assert institutions.classify("Loving Care Catering") is None
    assert institutions.classify("Good Living Foods") is None
    assert institutions.classify("Medical Supply Warehouse") is None


def test_an_ordinary_business_is_not_classified():
    for name in ("Blue Diamond Growers", "Mary Ann's Baking Co", "Shamrock Foods"):
        assert institutions.classify(name) is None
        assert institutions.is_institutional(name) is False


# --- marking the sheet ---------------------------------------------------


def _seed(backend: sheet.SheetBackend, names: list[tuple[str, str]]) -> None:
    backend.ensure_tab(sheet.LEADS_TAB, sheet.LEADS_COLUMNS)
    backend.append_rows(
        sheet.LEADS_TAB,
        [{"key": f"K{i}", "facility": n, "permit types": p} for i, (n, p) in enumerate(names)],
    )


def test_matching_rows_are_shaded_and_their_class_recorded():
    backend = sheet.FakeSheetBackend()
    _seed(backend, [
        ("MERCY SAN JUAN HOSPITAL-LHCF", "SATELLITE FOOD DISTRIBUTION FACILITY"),
        ("Joe's Taqueria", "RESTAURANT"),
        ("ELK GROVE POST ACUTE", "LICENSED HEALTH CARE FACILITY"),
    ])
    result = sheet.mark_institutions(backend, sheet.LEADS_TAB)

    assert result["marked"] == 2
    assert result["byClass"] == {
        institutions.CLASS_MEDICAL: 1,
        institutions.CLASS_CARE: 1,
    }
    assert sorted(backend.shading[sheet.LEADS_TAB]) == [0, 2]
    assert all(c == sheet.INSTITUTION_RGB for c in backend.shading[sheet.LEADS_TAB].values())
    rows = backend.read_rows(sheet.LEADS_TAB)
    assert rows[0]["facility class"] == institutions.CLASS_MEDICAL
    assert rows[1]["facility class"] == ""
    assert rows[2]["facility class"] == institutions.CLASS_CARE


def test_the_colour_is_paired_with_a_column_because_colour_survives_no_round_trip():
    """Nothing can later ask the sheet "which rows are care homes" from a fill
    colour, and a rep who copies a row loses it."""
    backend = sheet.FakeSheetBackend()
    _seed(backend, [("Sunrise Memory Care", "")])
    sheet.mark_institutions(backend, sheet.LEADS_TAB)
    assert "facility class" in sheet.TOOL_COLUMNS
    assert backend.read_rows(sheet.LEADS_TAB)[0]["facility class"] == institutions.CLASS_CARE


def test_a_row_that_stops_matching_has_its_stripe_and_class_removed():
    """A corrected facility name should take effect, not leave a stale red row."""
    backend = sheet.FakeSheetBackend()
    _seed(backend, [("Riverbank Apartments", "")])
    sheet.mark_institutions(backend, sheet.LEADS_TAB)
    assert backend.shading[sheet.LEADS_TAB] == {0: sheet.INSTITUTION_RGB}

    backend.update_row(sheet.LEADS_TAB, 0, {"facility": "Riverbank Cafe"})
    result = sheet.mark_institutions(backend, sheet.LEADS_TAB)
    assert (result["marked"], result["unmarked"]) == (0, 1)
    assert backend.shading[sheet.LEADS_TAB] == {}
    assert backend.read_rows(sheet.LEADS_TAB)[0]["facility class"] == ""


def test_re_running_is_idempotent():
    backend = sheet.FakeSheetBackend()
    _seed(backend, [("Sunrise Memory Care", "")])
    sheet.mark_institutions(backend, sheet.LEADS_TAB)
    again = sheet.mark_institutions(backend, sheet.LEADS_TAB)
    assert again["marked"] == 1
    assert backend.read_rows(sheet.LEADS_TAB)[0]["facility class"] == institutions.CLASS_CARE


def test_a_dry_run_reports_without_touching_the_sheet():
    backend = sheet.FakeSheetBackend()
    _seed(backend, [("Sunrise Memory Care", "")])
    result = sheet.mark_institutions(backend, sheet.LEADS_TAB, dry_run=True)
    assert result["marked"] == 1
    assert backend.shading.get(sheet.LEADS_TAB, {}) == {}
    assert backend.read_rows(sheet.LEADS_TAB)[0]["facility class"] == ""


def test_it_works_on_the_food_tab_too():
    backend = sheet.FakeSheetBackend()
    backend.ensure_tab(sheet.FOOD_TAB, sheet.FOOD_COLUMNS)
    backend.append_rows(sheet.FOOD_TAB, [{"key": "A", "facility": "Sutter Medical Center"}])
    result = sheet.mark_institutions(backend, sheet.FOOD_TAB)
    assert result["marked"] == 1
    assert backend.read_rows(sheet.FOOD_TAB)[0]["facility class"] == institutions.CLASS_MEDICAL


# --- the batched shading request ----------------------------------------


def test_contiguous_rows_collapse_into_one_request():
    """Shading 40 scattered rows one call at a time is how the earlier cell-by-cell
    writer took an HTTP 429; Sheets allows 60 write requests a minute."""
    requests = sheet._shade_requests(7, [0, 1, 2, 9], 12, sheet.INSTITUTION_RGB)
    assert len(requests) == 2
    first = requests[0]["repeatCell"]["range"]
    # Data row 0 is grid row 1 -- the header occupies grid row 0.
    assert (first["startRowIndex"], first["endRowIndex"]) == (1, 4)
    assert (first["startColumnIndex"], first["endColumnIndex"]) == (0, 12)
    second = requests[1]["repeatCell"]["range"]
    assert (second["startRowIndex"], second["endRowIndex"]) == (10, 11)


def test_clearing_sends_an_empty_format_rather_than_a_white_fill():
    """White is a colour; "no fill" is the absence of one, and only the second
    leaves a rep's own highlighting alone if they add some later."""
    requests = sheet._shade_requests(7, [3], 5, None)
    assert requests[0]["repeatCell"]["cell"]["userEnteredFormat"] == {}
    assert requests[0]["repeatCell"]["fields"] == "userEnteredFormat.backgroundColor"
