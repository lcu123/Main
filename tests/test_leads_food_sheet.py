"""`sync_food_facilities`: the second call list on the same spreadsheet.

What is being pinned here is the contract the Leads tab already earned the hard
way -- write nothing until the end, never overwrite a rep's cell, share the DNC
list, and recognise a row on a rerun rather than duplicating it -- plus the two
things specific to this tab: the shared `DNC` list applies across both, and a
facility that is also in `Leads` is kept and flagged rather than dropped.
"""

from __future__ import annotations

from datetime import date

from fr_mcp.leads import food, sheet
from fr_mcp.leads.sheet import FakeSheetBackend

TODAY = date(2026, 9, 9)


def _facility(**kw) -> food.FoodFacility:
    base = dict(
        key="CALEPA:1", name="Blue Diamond Growers", phone="9164420771",
        phone_source="calepa:Operator", category=food.CAT_PRODUCE, address="1802 C ST",
        city="Sacramento", zip5="95811", distance_miles=7.6, distance_basis="coords",
        sources=["CalEPA"], found_via="CalEPA NAICS 311",
    )
    base.update(kw)
    return food.FoodFacility(**base)


def _backend() -> FakeSheetBackend:
    return FakeSheetBackend()


def test_a_first_run_creates_the_tab_and_appends_every_facility():
    backend = _backend()
    result = sheet.sync_food_facilities(backend, [_facility(), _facility(key="CDFA:2", name="Sunwest Foods")],
                                        today=TODAY)
    assert result.added == 2
    rows = backend.read_rows(sheet.FOOD_TAB)
    assert [r["facility"] for r in rows] == ["Blue Diamond Growers", "Sunwest Foods"]
    assert rows[0]["type of business"] == food.CAT_PRODUCE
    assert rows[0]["distance (mi)"] == 7.6
    assert rows[0]["status"] == "new"


def test_the_dialer_columns_lead_exactly_as_they_do_on_the_leads_tab():
    """The owner's decision: reps work both tabs and get one muscle memory."""
    assert sheet.FOOD_COLUMNS[:4] == sheet.DIALER_COLUMNS
    assert sheet.FOOD_COLUMNS[4:6] == ["type of business", "city"]


def test_rerunning_the_same_pull_changes_nothing():
    backend = _backend()
    sheet.sync_food_facilities(backend, [_facility()], today=TODAY)
    again = sheet.sync_food_facilities(backend, [_facility()], today=TODAY)
    assert (again.added, again.skipped_no_change) == (0, 1)
    assert len(backend.read_rows(sheet.FOOD_TAB)) == 1


def test_a_phone_found_later_backfills_a_row_that_had_none():
    backend = _backend()
    sheet.sync_food_facilities(backend, [_facility(phone="", phone_source="")], today=TODAY)
    result = sheet.sync_food_facilities(
        backend, [_facility(phone="9164420771", phone_source="google_places")], today=TODAY
    )
    assert result.enriched == 1
    assert backend.read_rows(sheet.FOOD_TAB)[0]["phone"] == "9164420771"


def test_a_number_a_rep_typed_is_never_overwritten():
    backend = _backend()
    sheet.sync_food_facilities(backend, [_facility(phone="9160000000")], today=TODAY)
    sheet.sync_food_facilities(backend, [_facility(phone="9164420771")], today=TODAY)
    assert backend.read_rows(sheet.FOOD_TAB)[0]["phone"] == "9160000000"


def test_rep_columns_are_left_alone_on_a_rerun():
    backend = _backend()
    sheet.sync_food_facilities(backend, [_facility(phone="")], today=TODAY)
    backend.update_row(sheet.FOOD_TAB, 0, {"notes": "spoke to plant manager", "status": "Callback"})
    sheet.sync_food_facilities(backend, [_facility(phone="9164420771")], today=TODAY)
    row = backend.read_rows(sheet.FOOD_TAB)[0]
    assert row["notes"] == "spoke to plant manager"
    assert row["status"] == "Callback"
    assert row["phone"] == "9164420771"


def test_the_dnc_list_is_shared_with_the_leads_tab():
    """A number a rep has been told not to call must not reappear on a second
    list under a different heading."""
    backend = _backend()
    backend.ensure_tab(sheet.DNC_TAB, sheet.DNC_COLUMNS)
    backend.append_rows(sheet.DNC_TAB, [{"phone": "(916) 442-0771", "reason": "asked not to be called"}])
    result = sheet.sync_food_facilities(backend, [_facility()], today=TODAY)
    assert (result.added, result.skipped_dnc) == (0, 1)


def test_a_dnc_entry_by_key_also_blocks_the_row():
    backend = _backend()
    backend.ensure_tab(sheet.DNC_TAB, sheet.DNC_COLUMNS)
    backend.append_rows(sheet.DNC_TAB, [{"key": "CALEPA:1", "reason": "competitor"}])
    assert sheet.sync_food_facilities(backend, [_facility()], today=TODAY).skipped_dnc == 1


def test_a_facility_already_in_leads_is_kept_and_flagged_not_dropped():
    """Plan 13 answer 5 -- a processor that also holds a retail permit is still a
    processor lead, but the rep needs to know before opening with a pitch."""
    backend = _backend()
    backend.ensure_tab(sheet.LEADS_TAB, sheet.LEADS_COLUMNS)
    backend.append_rows(sheet.LEADS_TAB, [{"facility": "BLUE DIAMOND GROWERS INC", "zip": "95811"}])
    sheet.sync_food_facilities(backend, [_facility()], today=TODAY)
    assert backend.read_rows(sheet.FOOD_TAB)[0]["in leads tab"] == "yes"


def test_a_leads_row_matched_by_phone_alone_also_flags():
    backend = _backend()
    backend.ensure_tab(sheet.LEADS_TAB, sheet.LEADS_COLUMNS)
    backend.append_rows(sheet.LEADS_TAB, [{"facility": "Totally Different Name", "zip": "95999",
                                           "phone": "9164420771"}])
    sheet.sync_food_facilities(backend, [_facility()], today=TODAY)
    assert backend.read_rows(sheet.FOOD_TAB)[0]["in leads tab"] == "yes"


def test_an_unrelated_leads_row_does_not_flag():
    backend = _backend()
    backend.ensure_tab(sheet.LEADS_TAB, sheet.LEADS_COLUMNS)
    backend.append_rows(sheet.LEADS_TAB, [{"facility": "Some Taqueria", "zip": "95820", "phone": "9165550000"}])
    sheet.sync_food_facilities(backend, [_facility()], today=TODAY)
    assert backend.read_rows(sheet.FOOD_TAB)[0]["in leads tab"] == ""


def test_a_dry_run_writes_nothing_but_still_reports_what_it_would_do():
    backend = _backend()
    result = sheet.sync_food_facilities(backend, [_facility()], today=TODAY, dry_run=True)
    assert result.added == 1
    assert backend.read_rows(sheet.FOOD_TAB) == []


def test_the_row_cap_holds_back_the_overflow_rather_than_failing():
    backend = _backend()
    facilities = [_facility(key=f"CALEPA:{i}", name=f"Plant {i}") for i in range(5)]
    result = sheet.sync_food_facilities(backend, facilities, today=TODAY, new_row_cap=3)
    assert (result.added, result.skipped_cap) == (3, 2)


def test_review_flags_reach_the_sheet_with_their_reason():
    backend = _backend()
    sheet.sync_food_facilities(
        backend,
        [_facility(needs_review=True, review_reason="licensed to an individual")],
        today=TODAY,
    )
    row = backend.read_rows(sheet.FOOD_TAB)[0]
    assert row["needs review"] == "yes"
    assert "individual" in row["review reason"]


def test_tool_and_rep_columns_do_not_overlap():
    """The whole ownership contract rests on the two lists being disjoint by name."""
    assert not set(sheet.FOOD_TOOL_COLUMNS) & set(sheet.FOOD_REP_COLUMNS)
    # and every column in the display order is owned by exactly one of them
    assert set(sheet.FOOD_COLUMNS) == set(sheet.FOOD_TOOL_COLUMNS) | set(sheet.FOOD_REP_COLUMNS)


def test_keys_needing_phone_lists_only_the_rows_a_billed_lookup_could_help():
    backend = _backend()
    sheet.sync_food_facilities(
        backend,
        [_facility(key="A", phone=""), _facility(key="B", phone="9165551234")],
        today=TODAY,
    )
    assert sheet.food_keys_needing_phone(backend) == {"A"}
    assert sheet.food_existing_keys(backend) == {"A", "B"}
