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
