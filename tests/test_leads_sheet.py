"""sync_leads against FakeSheetBackend: dedupe, DNC honouring, new-signal
flagging without touching rep columns, the new-row cap, and dry-run leaving
the sheet untouched -- the phase-B exit criteria in
docs/lead-scraper-plan.md section 10."""

from __future__ import annotations

from datetime import date

from fr_mcp.leads import arcgis as ag
from fr_mcp.leads import sheet
from fr_mcp.leads.classify import Classification
from fr_mcp.leads.pipeline import LANE_EVENT, LANE_TERRITORY, LeadCandidate
from fr_mcp.leads.reports import ReportHeader
from fr_mcp.leads.score import IcpInputs, PestInputs, score_candidate

TODAY = date(2026, 9, 8)


def _event_candidate(facility_id="FA9001", *, pkey="PKEY-1", county="Sacramento", header=None) -> LeadCandidate:
    signal = ag.Signal(
        pkey=pkey, date=date(2026, 9, 2), result="MAJOR VIOLATION", inspection_type="ROUTINE",
        violation_description="VERMIN AND ANIMAL CONTAMINATION", report_url=f"https://x/{pkey}",
    )
    classification = Classification(("rodent",), "rodent", False, False, False, "Observed rodent droppings.")
    score = score_candidate(IcpInputs(base=30), PestInputs(kind="rodent"), days_since_signal=5, geo_multiplier=1.0)
    return LeadCandidate(
        facility_id=facility_id, county=county, customer_link=f"SACEMD:{facility_id}", name="TEST MARKET",
        street="123 Test St", city="Sacramento", zip5="95814", lat=38.6, lng=-121.5,
        permits=["RETAIL MARKET (15000+SQ.FT)"], lane=LANE_EVENT, score=score, classification=classification,
        signal=signal, header=header, distance_miles=5.0, region_id=3, region_name="Downtown",
        is_chain=False, days_since_signal=5,
    )


def _territory_candidate(facility_id="FA9002") -> LeadCandidate:
    score = score_candidate(IcpInputs(base=30), PestInputs(kind="none"), days_since_signal=None, geo_multiplier=0.9)
    return LeadCandidate(
        facility_id=facility_id, county="Sacramento", customer_link=f"SACEMD:{facility_id}", name="TERRITORY MARKET",
        street="456 Other Ave", city="Sacramento", zip5="95820", lat=38.5, lng=-121.4,
        permits=["RETAIL MARKET (6000-14999 SQ.FT.)"], lane=LANE_TERRITORY, score=score,
        classification=None, signal=None, header=None, distance_miles=12.0, region_id=4,
        region_name="South Sacramento", is_chain=False, days_since_signal=None,
    )


def test_new_facility_is_appended_with_tool_columns_filled_and_status_new():
    backend = sheet.FakeSheetBackend()
    result = sheet.sync_leads(backend, [_event_candidate()], today=TODAY)
    assert result.added == 1
    rows = backend.read_rows(sheet.LEADS_TAB)
    assert len(rows) == 1
    row = rows[0]
    assert row["key"] == "SACEMD:FA9001"
    assert row["county"] == "Sacramento"
    assert row["facility"] == "TEST MARKET"
    assert row["pest"] == "rodent"
    assert row["status"] == "new"
    assert row["rep"] == ""  # rep-owned columns start blank


def test_signals_tab_gets_one_row_per_new_signal():
    backend = sheet.FakeSheetBackend()
    sheet.sync_leads(backend, [_event_candidate()], today=TODAY)
    signals = backend.read_rows(sheet.SIGNALS_TAB)
    assert len(signals) == 1
    assert signals[0]["key"] == "SACEMD:FA9001"
    assert signals[0]["guid"] == "PKEY-1"


def test_a_same_day_rerun_adds_nothing():
    backend = sheet.FakeSheetBackend()
    sheet.sync_leads(backend, [_event_candidate()], today=TODAY)
    result = sheet.sync_leads(backend, [_event_candidate()], today=TODAY)
    assert result.added == 0
    assert result.skipped_duplicate == 1
    assert len(backend.read_rows(sheet.LEADS_TAB)) == 1
    assert len(backend.read_rows(sheet.SIGNALS_TAB)) == 1


def test_territory_lane_row_is_created_once_then_a_no_op():
    backend = sheet.FakeSheetBackend()
    r1 = sheet.sync_leads(backend, [_territory_candidate()], today=TODAY)
    assert r1.added == 1
    r2 = sheet.sync_leads(backend, [_territory_candidate()], today=TODAY)
    assert r2.added == 0
    assert r2.skipped_no_change == 1
    assert len(backend.read_rows(sheet.LEADS_TAB)) == 1


def test_a_new_signal_on_a_known_row_flags_it_without_touching_rep_columns():
    backend = sheet.FakeSheetBackend()
    sheet.sync_leads(backend, [_event_candidate(pkey="PKEY-1")], today=TODAY)
    # Simulate a rep having worked the row.
    backend.update_row(sheet.LEADS_TAB, 0, {"rep": "Alex", "status": "called", "notes": "left voicemail"})

    result = sheet.sync_leads(backend, [_event_candidate(pkey="PKEY-2")], today=date(2026, 9, 10))
    assert result.flagged == 1
    row = backend.read_rows(sheet.LEADS_TAB)[0]
    assert row["signal count"] == 2
    assert row["new-signal flag"] == "yes"
    # Rep-owned columns untouched by the flag.
    assert row["rep"] == "Alex"
    assert row["status"] == "called"
    assert row["notes"] == "left voicemail"
    signals = backend.read_rows(sheet.SIGNALS_TAB)
    assert {s["guid"] for s in signals} == {"PKEY-1", "PKEY-2"}


def test_a_later_run_backfills_blank_contact_columns_without_touching_rep_columns():
    backend = sheet.FakeSheetBackend()
    sheet.sync_leads(backend, [_event_candidate()], today=TODAY)  # no header: phone/owner land blank
    backend.update_row(sheet.LEADS_TAB, 0, {"rep": "Alex", "status": "called"})
    assert backend.read_rows(sheet.LEADS_TAB)[0]["phone"] == ""

    header = ReportHeader(owner="HAROON KHAN", is_entity=False, facility_id="FA9001", permit_id="PR1", phone="5103763395")
    result = sheet.sync_leads(backend, [_event_candidate(header=header)], today=date(2026, 9, 9))
    assert result.enriched == 1
    assert result.added == 0
    assert result.skipped_duplicate == 0  # the same signal is still a no-op for evidence, but the contact fill counts
    row = backend.read_rows(sheet.LEADS_TAB)[0]
    assert row["phone"] == "5103763395"
    assert row["phone source"] == "report_pdf"
    assert row["owner name"] == "HAROON KHAN"
    assert row["owner type"] == "person"
    assert row["last updated"] == "2026-09-09"
    assert row["rep"] == "Alex" and row["status"] == "called"
    assert len(backend.read_rows(sheet.SIGNALS_TAB)) == 1  # no duplicate signal row


def test_backfill_never_overwrites_a_contact_value_already_present():
    backend = sheet.FakeSheetBackend()
    header = ReportHeader(owner="J DOE", is_entity=False, facility_id="FA9001", permit_id="PR1", phone="9165550000")
    sheet.sync_leads(backend, [_event_candidate(header=header)], today=TODAY)
    backend.update_row(sheet.LEADS_TAB, 0, {"phone": "9165559999"})  # a rep corrected it by hand
    result = sheet.sync_leads(backend, [_event_candidate(header=header)], today=date(2026, 9, 9))
    assert result.enriched == 0
    assert backend.read_rows(sheet.LEADS_TAB)[0]["phone"] == "9165559999"


def test_every_row_update_in_a_run_goes_out_in_one_batched_call():
    # Sheets allows 60 writes/minute/user; a cell-at-a-time loop 429'd partway
    # through a 40-row backfill and left the sheet half-written.
    class _CountingBackend(sheet.FakeSheetBackend):
        batches = 0

        def update_rows(self, tab, updates):
            type(self).batches += 1
            super().update_rows(tab, updates)

    backend = _CountingBackend()
    header = ReportHeader(owner="J DOE", is_entity=False, facility_id="F", permit_id="P", phone="9165550000")
    candidates = [_event_candidate(facility_id=f"FA{i}", pkey=f"PK{i}") for i in range(5)]
    sheet.sync_leads(backend, candidates, today=TODAY)
    enriched = [_event_candidate(facility_id=f"FA{i}", pkey=f"PK{i}", header=header) for i in range(5)]
    result = sheet.sync_leads(backend, enriched, today=date(2026, 9, 9))
    assert result.enriched == 5
    assert _CountingBackend.batches == 1  # five rows, one call
    assert all(r["phone"] == "9165550000" for r in backend.read_rows(sheet.LEADS_TAB))


class _DuplicateHeaderBackend(sheet.FakeSheetBackend):
    """A sheet whose Leads header repeats a tool-owned column, the way a rep
    copying a column in the real spreadsheet does."""

    def header(self, tab: str) -> list[str]:
        cols = list(self.columns.get(tab, []))
        return cols + ["report link"] if tab == sheet.LEADS_TAB else cols


def test_a_duplicated_tool_column_is_reported_without_stopping_the_run():
    backend = _DuplicateHeaderBackend()
    result = sheet.sync_leads(backend, [_event_candidate()], today=TODAY)
    assert result.added == 1  # the run still does its job
    assert any("report link" in e for e in result.errors)
    assert backend.read_rows(sheet.RUNS_TAB)[0]["errors"].startswith("the Leads tab has more than one")


def test_a_newly_shipped_tool_column_is_added_to_an_existing_sheet():
    # A live sheet predates any column we add later. Writes are addressed by name,
    # so without this the new column is silently dropped on every run.
    backend = sheet.FakeSheetBackend()
    backend.ensure_tab(sheet.LEADS_TAB, [c for c in sheet.LEADS_COLUMNS if c != "business phone"])
    backend.append_rows(sheet.LEADS_TAB, [{"key": "SACEMD:FA1", "facility": "OLD ROW"}])

    header = ReportHeader(owner="J DOE", is_entity=False, facility_id="F", permit_id="P", phone="9165550000")
    c = _event_candidate(header=header)
    c.business_phone = "9164445555"
    sheet.sync_leads(backend, [c], today=TODAY)

    assert "business phone" in backend.columns[sheet.LEADS_TAB]
    assert backend.columns[sheet.LEADS_TAB][-1] == "business phone"  # appended right, layout untouched
    assert backend.read_rows(sheet.LEADS_TAB)[0]["business phone"] == ""  # pre-existing row unharmed
    assert backend.read_rows(sheet.LEADS_TAB)[1]["business phone"] == "9164445555"


def test_the_county_phone_is_kept_when_places_supplies_a_business_number():
    # The report-header number is often the owner's personal mobile; the Places
    # number is the public line. A rep wants both, so neither overwrites the other.
    backend = sheet.FakeSheetBackend()
    header = ReportHeader(owner="J DOE", is_entity=False, facility_id="F", permit_id="P", phone="9165550000")
    c = _event_candidate(header=header)
    c.business_phone = "9164445555"
    sheet.sync_leads(backend, [c], today=TODAY)
    row = backend.read_rows(sheet.LEADS_TAB)[0]
    assert row["phone"] == "9165550000"
    assert row["phone source"] == "report_pdf"
    assert row["business phone"] == "9164445555"


def _with_county_phone(number: str, business_phone: str | None = None) -> LeadCandidate:
    header = ReportHeader(owner="J DOE", is_entity=False, facility_id="F", permit_id="P", phone=number)
    c = _event_candidate(header=header)
    c.business_phone = business_phone
    return c


def test_an_out_of_area_county_number_is_flagged_for_the_rep():
    # Audited live: every out-of-area county number checked was not the business's
    # line (a Long Island number on a Folsom restaurant, a transposed area code).
    backend = sheet.FakeSheetBackend()
    sheet.sync_leads(backend, [_with_county_phone("5165671898", "9167908152")], today=TODAY)
    row = backend.read_rows(sheet.LEADS_TAB)[0]
    assert row["phone"] == "5165671898"  # kept, not discarded
    assert row["business phone"] == "9167908152"
    assert row["phone flag"] == "out-of-area number -- use business phone"


def test_an_out_of_area_number_with_no_alternative_says_verify():
    backend = sheet.FakeSheetBackend()
    sheet.sync_leads(backend, [_with_county_phone("4258009999")], today=TODAY)
    assert backend.read_rows(sheet.LEADS_TAB)[0]["phone flag"] == "out-of-area number -- verify"


def test_an_out_of_area_number_places_confirms_is_not_flagged():
    # A business may legitimately publish an out-of-area line; four rows in the
    # first live backfill did. Flagging those teaches reps to ignore the column.
    backend = sheet.FakeSheetBackend()
    sheet.sync_leads(backend, [_with_county_phone("8318564132", "8318564132")], today=TODAY)
    assert backend.read_rows(sheet.LEADS_TAB)[0]["phone flag"] == ""


def test_a_local_number_is_not_flagged():
    backend = sheet.FakeSheetBackend()
    sheet.sync_leads(backend, [_with_county_phone("9164161664", "9163492951")], today=TODAY)
    assert backend.read_rows(sheet.LEADS_TAB)[0]["phone flag"] == ""


def test_plan_reorder_puts_the_dialer_columns_first_and_keeps_everything_else():
    header = ["key", "notes", "facility", "phone", "county", "rep"]
    new = sheet.plan_reorder(header, ["facility", "phone", "notes", "followup date"])
    assert new[:4] == ["facility", "phone", "notes", "followup date"]
    assert set(header) <= set(new)  # nothing dropped


def test_plan_reorder_collapses_a_duplicated_column():
    header = ["facility", "report link", "phone", "report link", "notes"]
    new = sheet.plan_reorder(header, sheet.DIALER_COLUMNS)
    assert new.count("report link") == 1


def test_plan_reorder_removes_a_column_we_retired():
    # A column the tool introduced and then thought better of is removed; one a
    # rep added is not ours to delete (see the test below).
    header = ["facility", "phone", "followup", "notes"]
    assert "followup" not in sheet.plan_reorder(header, sheet.DIALER_COLUMNS)


def test_plan_reorder_keeps_a_column_the_reps_added_themselves():
    header = ["facility", "phone", "notes", "my own column"]
    assert "my own column" in sheet.plan_reorder(header, sheet.DIALER_COLUMNS)


def test_reorder_moves_the_values_with_their_columns():
    backend = sheet.FakeSheetBackend()
    backend.ensure_tab(sheet.LEADS_TAB, ["key", "facility", "phone", "notes", "followup date"])
    backend.append_rows(sheet.LEADS_TAB, [{"key": "K1", "facility": "JOE'S", "phone": "9165551234", "notes": "left vm"}])
    result = sheet.reorder_leads_tab(backend, ["facility", "phone", "notes", "followup date"])
    assert result["reordered"] is True
    assert backend.header(sheet.LEADS_TAB)[:4] == ["facility", "phone", "notes", "followup date"]
    row = backend.read_rows(sheet.LEADS_TAB)[0]
    assert row["facility"] == "JOE'S" and row["phone"] == "9165551234"
    assert row["notes"] == "left vm"  # the rep's own writing survives the move
    assert row["key"] == "K1"


def test_reorder_renames_a_column_and_carries_the_reps_values_across():
    backend = sheet.FakeSheetBackend()
    backend.ensure_tab(sheet.LEADS_TAB, ["key", "facility", "phone", "next step date"])
    backend.append_rows(sheet.LEADS_TAB, [{"key": "K1", "next step date": "2026-09-15"}])
    result = sheet.reorder_leads_tab(backend, sheet.DIALER_COLUMNS, {"next step date": "followup date"})
    header = backend.header(sheet.LEADS_TAB)
    assert "followup date" in header and "next step date" not in header
    assert backend.read_rows(sheet.LEADS_TAB)[0]["followup date"] == "2026-09-15"
    assert result["renamed"] == {"next step date": "followup date"}


def test_reorder_is_idempotent():
    backend = sheet.FakeSheetBackend()
    backend.ensure_tab(sheet.LEADS_TAB, sheet.LEADS_COLUMNS)
    backend.append_rows(sheet.LEADS_TAB, [{"key": "K1", "facility": "JOE'S"}])
    sheet.reorder_leads_tab(backend)
    assert sheet.reorder_leads_tab(backend)["reordered"] is False


def test_keys_with_business_phone_reports_only_rows_already_enriched():
    backend = sheet.FakeSheetBackend()
    c1, c2 = _event_candidate(facility_id="FA1", pkey="P1"), _event_candidate(facility_id="FA2", pkey="P2")
    c1.business_phone = "9164445555"
    sheet.sync_leads(backend, [c1, c2], today=TODAY)
    assert sheet.keys_with_business_phone(backend) == {"SACEMD:FA1"}


def test_dnc_by_key_is_skipped_and_logged():
    backend = sheet.FakeSheetBackend()
    backend.ensure_tab(sheet.DNC_TAB, sheet.DNC_COLUMNS)
    backend.append_rows(sheet.DNC_TAB, [{"key": "SACEMD:FA9001", "reason": "asked not to be contacted"}])
    result = sheet.sync_leads(backend, [_event_candidate()], today=TODAY)
    assert result.added == 0
    assert result.skipped_dnc == 1
    assert backend.read_rows(sheet.LEADS_TAB) == []


def test_dnc_by_phone_is_skipped():
    backend = sheet.FakeSheetBackend()
    backend.ensure_tab(sheet.DNC_TAB, sheet.DNC_COLUMNS)
    backend.append_rows(sheet.DNC_TAB, [{"phone": "9164161664"}])
    header = ReportHeader(owner="J DOE", is_entity=False, facility_id="FA9001", permit_id="PR1", phone="9164161664")
    result = sheet.sync_leads(backend, [_event_candidate(header=header)], today=TODAY)
    assert result.skipped_dnc == 1


def test_dnc_by_email_is_skipped_case_insensitively():
    backend = sheet.FakeSheetBackend()
    backend.ensure_tab(sheet.DNC_TAB, sheet.DNC_COLUMNS)
    backend.append_rows(sheet.DNC_TAB, [{"email": "owner@example.com"}])
    c = _event_candidate()
    c.email = "Owner@Example.com"
    result = sheet.sync_leads(backend, [c], today=TODAY)
    assert result.skipped_dnc == 1


def test_new_row_cap_is_enforced_and_logged():
    backend = sheet.FakeSheetBackend()
    candidates = [_event_candidate(facility_id=f"FA900{i}", pkey=f"PKEY-{i}") for i in range(3)]
    result = sheet.sync_leads(backend, candidates, today=TODAY, new_row_cap=2)
    assert result.added == 2
    assert result.skipped_cap == 1
    assert len(backend.read_rows(sheet.LEADS_TAB)) == 2


def test_dry_run_writes_nothing_at_all():
    backend = sheet.FakeSheetBackend()
    result = sheet.sync_leads(backend, [_event_candidate()], today=TODAY, dry_run=True)
    assert result.added == 1  # counted, but not written
    assert backend.read_rows(sheet.LEADS_TAB) == []
    assert backend.read_rows(sheet.SIGNALS_TAB) == []
    assert backend.read_rows(sheet.RUNS_TAB) == []


def test_runs_tab_gets_one_row_per_real_run():
    backend = sheet.FakeSheetBackend()
    sheet.sync_leads(backend, [_event_candidate()], today=TODAY)
    sheet.sync_leads(backend, [_event_candidate(facility_id="FA9003", pkey="PKEY-3")], today=date(2026, 9, 9))
    runs = backend.read_rows(sheet.RUNS_TAB)
    assert len(runs) == 2
    assert runs[0]["rows added"] == 1


def test_non_pushable_candidates_are_never_written():
    backend = sheet.FakeSheetBackend()
    c = _event_candidate()
    c.lane = "chain"
    result = sheet.sync_leads(backend, [c], today=TODAY)
    assert result.added == 0
    assert backend.read_rows(sheet.LEADS_TAB) == []


def test_open_backend_requires_sheet_id(monkeypatch):
    monkeypatch.delenv("LEADS_SHEET_ID", raising=False)
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON_PATH", raising=False)
    try:
        sheet.open_backend()
        assert False, "expected ConfigError"
    except Exception as exc:
        assert "LEADS_SHEET_ID" in str(exc)


def test_open_backend_requires_credentials(monkeypatch):
    monkeypatch.setenv("LEADS_SHEET_ID", "abc123")
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON_PATH", raising=False)
    try:
        sheet.open_backend()
        assert False, "expected ConfigError"
    except Exception as exc:
        assert "GOOGLE_SERVICE_ACCOUNT_JSON" in str(exc)


def test_a_dnc_phone_typed_with_punctuation_still_blocks_the_lead():
    """The DNC tab is filled in by hand, so the number arrives however the rep
    wrote it. Comparing that against this pipeline's bare digits as-typed blocks
    nothing at all -- the entry looks present and does nothing."""
    backend = sheet.FakeSheetBackend()
    backend.ensure_tab(sheet.DNC_TAB, sheet.DNC_COLUMNS)
    backend.append_rows(sheet.DNC_TAB, [{"phone": "(916) 416-1664"}])
    header = ReportHeader(owner="J DOE", is_entity=False, facility_id="FA9001", permit_id="PR1", phone="9164161664")
    result = sheet.sync_leads(backend, [_event_candidate(header=header)], today=TODAY)
    assert result.skipped_dnc == 1


def test_the_row_caps_are_settable_because_the_readme_says_they_are(monkeypatch):
    """They were plain constants while the README documented them as environment
    variables, so setting one on the deployment did nothing at all."""
    monkeypatch.setenv("LEADS_SHEET_NEW_ROW_CAP", "125")
    assert sheet._cap("LEADS_SHEET_NEW_ROW_CAP", 40) == 125
    # Nonsense and negatives fall back rather than capping the run at zero rows.
    monkeypatch.setenv("LEADS_SHEET_NEW_ROW_CAP", "not-a-number")
    assert sheet._cap("LEADS_SHEET_NEW_ROW_CAP", 40) == 40
    monkeypatch.setenv("LEADS_SHEET_NEW_ROW_CAP", "-5")
    assert sheet._cap("LEADS_SHEET_NEW_ROW_CAP", 40) == 40
    monkeypatch.delenv("LEADS_SHEET_NEW_ROW_CAP")
    assert sheet._cap("LEADS_SHEET_NEW_ROW_CAP", 40) == 40
