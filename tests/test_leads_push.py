"""FieldRoutes dedupe, idempotency, dry-run, caps, and config-error tests
against a FakeFR that actually persists what it creates (leads_persistent_fake.py)
-- the shared conftest.py fixture only echoes writes back, which isn't enough
to prove "a second run creates nothing" the way this package's own docs
(docs/lead-scraper-plan.md 5.2, 7) promise.
"""

from __future__ import annotations

import os
from datetime import date

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

import conftest as ct
from fr_mcp import server
from fr_mcp.client import RateLimiter
from fr_mcp.leads import arcgis as ag
from fr_mcp.leads import fr_push as push
from fr_mcp.leads.classify import Classification
from fr_mcp.leads.pipeline import LANE_EVENT, LANE_TERRITORY, LeadCandidate
from fr_mcp.leads.reports import ReportHeader
from fr_mcp.leads.score import score_candidate, IcpInputs, PestInputs
from leads_persistent_fake import PersistentFakeFR

TODAY = date(2026, 9, 7)


def _client_for(fake: ct.FakeFR) -> ct.FieldRoutesClient:
    return ct.FieldRoutesClient(
        subdomain="zest", auth_key="key", auth_token="token",
        transport=httpx.MockTransport(fake.handler), rate_limiter=RateLimiter(limit=100000),
    )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> PersistentFakeFR:
    fr = PersistentFakeFR()
    monkeypatch.setattr(server, "_client", _client_for(fr))
    server._reset_cache()
    monkeypatch.setenv("FR_WRITES", "on")
    monkeypatch.delenv("FR_ALLOW_DELETE", raising=False)
    monkeypatch.delenv("FR_ALLOW_CHARGES", raising=False)
    monkeypatch.delenv("FR_WRITE_CUSTOMER_IDS", raising=False)
    monkeypatch.setenv("LEADS_SOURCE_ID", "10009")
    monkeypatch.setenv("LEADS_TASK_CATEGORY_ID", "10002")
    monkeypatch.setenv("LEADS_NOTE_TYPE_ID", "5")
    monkeypatch.setenv("LEADS_ASSIGN_TO", "12")
    return fr


def _event_candidate(
    facility_id="FA9001", *, name="TEST MARKET", tier_kind="rodent", header=None, extra_permit_count=0
) -> LeadCandidate:
    signal = ag.Signal(
        pkey="AAAAAAAA-1111-2222-3333-444444444444", date=date(2026, 9, 2), result="MINOR VIOLATIONS",
        inspection_type="ROUTINE", violation_description="VERMIN AND ANIMAL CONTAMINATION",
        report_url="https://inspections.myhealthdepartment.com/sacramento/print/?pKey=AAAAAAAA-1111-2222-3333-444444444444",
    )
    classification = Classification((tier_kind,), tier_kind, False, False, False, "Observed rodent droppings.")
    score = score_candidate(IcpInputs(base=30), PestInputs(kind=tier_kind), days_since_signal=5, geo_multiplier=1.0)
    return LeadCandidate(
        facility_id=facility_id, county="Sacramento", customer_link=f"SACEMD:{facility_id}", name=name,
        street="123 Test St", city="Sacramento", zip5="95814", lat=38.6, lng=-121.5,
        permits=["RETAIL MARKET (15000+SQ.FT)"],
        lane=LANE_EVENT, score=score, classification=classification, signal=signal, header=header,
        distance_miles=5.0, region_id=3, region_name="Downtown", is_chain=False, days_since_signal=5,
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


# --- config -------------------------------------------------------------


def test_config_error_when_source_id_missing(fake, monkeypatch):
    monkeypatch.delenv("LEADS_SOURCE_ID", raising=False)
    with pytest.raises(push.ConfigError):
        push.source_id()


def test_task_category_falls_back_to_fr_default(fake, monkeypatch):
    monkeypatch.delenv("LEADS_TASK_CATEGORY_ID", raising=False)
    monkeypatch.setenv("FR_DEFAULT_TASK_CATEGORY_ID", "10002")
    assert push.task_category_id() == 10002


# --- create ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_lead_writes_customer_note_and_task(fake):
    header = ReportHeader(owner="HAROON KHAN", is_entity=False, facility_id="FA9001", permit_id="PR1", phone="9165551234")
    c = _event_candidate(header=header)
    result = await push.create_lead(c, today=TODAY, dry_run=False)
    assert result.action == "created"
    assert result.customer_id is not None

    customer = fake.data["customer"][result.customer_id]
    assert customer["companyName"] == "TEST MARKET"
    assert customer["fname"] == "HAROON"
    assert customer["lname"] == "KHAN"
    assert customer["status"] == "0"
    assert customer["commercialAccount"] == "1"
    assert customer["sourceID"] == "10009"
    assert customer["customerLink"] == "SACEMD:FA9001"
    assert customer["phone1"] == "9165551234"
    assert customer["smsReminders"] == "0" and customer["phoneReminders"] == "0" and customer["emailReminders"] == "0"
    assert "notes" not in customer  # Red Notes must never be touched by the scraper

    note = fake.data["note"][result.note_id]
    assert note["customerID"] == str(result.customer_id)
    assert "rodent" in note["notes"].lower()
    assert "internal only" in note["notes"].lower()

    task = fake.data["task"][result.task_id]
    assert task["customerID"] == str(result.customer_id)
    assert task["category"] == "10002"
    assert task["assignedTo"] == "12"


@pytest.mark.asyncio
async def test_create_lead_entity_owner_uses_spouse_field_not_fname_lname(fake):
    header = ReportHeader(owner="SUPER SHOT RAJPURA INC", is_entity=True, facility_id="FA9001", permit_id="PR1", phone=None)
    c = _event_candidate(header=header)
    result = await push.create_lead(c, today=TODAY, dry_run=False)
    customer = fake.data["customer"][result.customer_id]
    assert customer["fname"] == ""
    assert customer["lname"] == "TEST MARKET"
    assert customer["spouse"] == "Owner: SUPER SHOT RAJPURA INC"
    assert "phone1" not in customer  # header.phone was None


@pytest.mark.asyncio
async def test_dry_run_makes_zero_writes(fake):
    c = _event_candidate()
    result = await push.create_lead(c, today=TODAY, dry_run=True)
    assert result.action == "would_create"
    assert fake.writes_today == 0
    assert fake.data["customer"] == {1: fake.data["customer"][1], 2: fake.data["customer"][2], 3: fake.data["customer"][3]}


@pytest.mark.asyncio
async def test_create_lead_requires_writes_enabled(fake, monkeypatch):
    monkeypatch.setenv("FR_WRITES", "off")
    c = _event_candidate()
    with pytest.raises(ToolError):
        await push.create_lead(c, today=TODAY, dry_run=False)
    assert fake.writes_today == 0


@pytest.mark.asyncio
async def test_create_lead_refused_when_allowlist_is_set(fake, monkeypatch):
    monkeypatch.setenv("FR_WRITE_CUSTOMER_IDS", "1")
    c = _event_candidate()
    with pytest.raises(ToolError):
        await push.create_lead(c, today=TODAY, dry_run=False)
    assert fake.writes_today == 0


# --- dedupe / idempotency --------------------------------------------------


@pytest.mark.asyncio
async def test_second_push_of_the_same_candidate_finds_it_by_customer_link_and_retouches(fake):
    c = _event_candidate()
    first = await push.push_candidate(c, today=TODAY, dry_run=False)
    assert first.action == "created"

    # Same facility, a NEW inspection pKey -- must retouch (note+task), not create again.
    c2 = _event_candidate()
    c2.signal = ag.Signal(
        pkey="BBBBBBBB-1111-2222-3333-444444444444", date=date(2026, 9, 6), result="CLOSED",
        inspection_type="ROUTINE", violation_description="VERMIN AND ANIMAL CONTAMINATION", report_url="https://x/2",
    )
    second = await push.push_candidate(c2, today=TODAY, dry_run=False)
    assert second.action == "retouched"
    assert second.customer_id == first.customer_id
    assert len(fake.data["customer"]) == 4  # no second customer created (3 seeded + 1)


@pytest.mark.asyncio
async def test_re_running_the_exact_same_signal_is_skipped_not_duplicated(fake):
    c = _event_candidate()
    first = await push.push_candidate(c, today=TODAY, dry_run=False)
    notes_before = len(fake.data["note"])

    result = await push.push_candidate(c, today=TODAY, dry_run=False)  # same signal pKey
    assert result.action == "skipped_duplicate_signal"
    assert result.customer_id == first.customer_id
    assert len(fake.data["note"]) == notes_before  # no new note written


@pytest.mark.asyncio
async def test_address_fallback_finds_an_existing_lead_and_adopts_customer_link(fake):
    # Simulate a lead the office created by hand in the FieldRoutes UI: same address as
    # a facility the scraper will see later, status 0 (a lead, not a real customer), no
    # customerLink yet -- the scraper must find it by address+zip and adopt it, not
    # create a duplicate customer for the same property.
    manual = await push.create_lead(_event_candidate(facility_id="FA_MANUAL"), today=TODAY, dry_run=False)
    manual_id = manual.customer_id
    fake.data["customer"][manual_id]["customerLink"] = ""  # as if staff created it directly

    c = _event_candidate(facility_id="FA_MATCHES_MANUAL")
    c.street, c.city, c.zip5 = fake.data["customer"][manual_id]["address"], fake.data["customer"][manual_id]["city"], fake.data["customer"][manual_id]["zip"]
    c.header = None
    c.signal = ag.Signal("DDDDDDDD-1111-2222-3333-444444444444", date(2026, 9, 6), "CLOSED", "ROUTINE", "VERMIN AND ANIMAL CONTAMINATION", "https://x/4")
    result = await push.push_candidate(c, today=TODAY, dry_run=False)
    assert result.customer_id == manual_id
    assert result.action == "retouched"
    assert fake.data["customer"][manual_id]["customerLink"] == "SACEMD:FA_MATCHES_MANUAL"
    customer_count_before = 4  # 3 seeded + the one manual create above
    assert len(fake.data["customer"]) == customer_count_before  # no new customer created


@pytest.mark.asyncio
async def test_existing_active_customer_gets_upsold_not_treated_as_a_new_lead(fake):
    # Customer 1 is active (status 1) in the seed.
    c = _event_candidate(facility_id="FA_ACTIVE_CUSTOMER")
    c.street, c.city, c.zip5 = "123 Main St", "Folsom", "95630"
    c.header = None
    result = await push.push_candidate(c, today=TODAY, dry_run=False)
    assert result.action == "upsold"
    assert result.customer_id == 1


@pytest.mark.asyncio
async def test_territory_lead_is_never_retouched_on_a_second_run(fake):
    c = _territory_candidate()
    first = await push.push_candidate(c, today=TODAY, dry_run=False)
    assert first.action == "created"
    notes_before = len(fake.data["note"])

    second = await push.push_candidate(_territory_candidate(), today=TODAY, dry_run=False)
    assert second.action == "skipped_already_lead"
    assert len(fake.data["note"]) == notes_before


@pytest.mark.asyncio
async def test_retouch_does_not_stack_a_second_open_task(fake):
    c = _event_candidate()
    first = await push.push_candidate(c, today=TODAY, dry_run=False)
    tasks_before = len(fake.data["task"])

    c2 = _event_candidate()
    c2.signal = ag.Signal("CCCC1111-1111-2222-3333-444444444444", date(2026, 9, 6), "CLOSED", "ROUTINE", "VERMIN AND ANIMAL CONTAMINATION", "https://x/3")
    second = await push.push_candidate(c2, today=TODAY, dry_run=False)
    assert second.action == "retouched"
    assert second.task_id is None  # the first task is still open, no second one created
    assert len(fake.data["task"]) == tasks_before


# --- caps -------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_run_honours_the_daily_event_cap(fake, monkeypatch):
    monkeypatch.setenv("LEADS_DAILY_CAP", "1")
    candidates = [_event_candidate(f"FA900{i}", name=f"MARKET {i}") for i in range(3)]
    summary = await push.push_run(candidates, today=TODAY, dry_run=False)
    assert summary.created == 1
    assert summary.skipped_cap == 2


@pytest.mark.asyncio
async def test_push_run_honours_the_territory_cap_independently_of_the_event_cap(fake, monkeypatch):
    monkeypatch.setenv("LEADS_DAILY_CAP", "5")
    monkeypatch.setenv("LEADS_TERRITORY_CAP", "1")
    candidates = [_event_candidate("FA9001")] + [_territory_candidate(f"FA900{i}") for i in range(2, 5)]
    summary = await push.push_run(candidates, today=TODAY, dry_run=False)
    assert summary.created == 2  # 1 event + 1 territory
    assert summary.skipped_cap == 2


@pytest.mark.asyncio
async def test_push_run_dry_run_makes_zero_writes_across_multiple_candidates(fake):
    candidates = [_event_candidate("FA9001"), _territory_candidate("FA9002")]
    summary = await push.push_run(candidates, today=TODAY, dry_run=True)
    assert summary.created == 2
    assert fake.writes_today == 0


@pytest.mark.asyncio
async def test_push_run_aborts_up_front_on_missing_config(fake, monkeypatch):
    # A missing LEADS_* setting fails the whole run before touching FieldRoutes at all
    # (plan 7's "fail-closed startup checks"), not just the one candidate that hit it --
    # the CLI's main() turns this into a clean JSON error line and exit code 2.
    monkeypatch.delenv("LEADS_SOURCE_ID", raising=False)
    with pytest.raises(push.ConfigError):
        await push.push_run([_event_candidate()], today=TODAY, dry_run=False)
    assert fake.writes_today == 0


@pytest.mark.asyncio
async def test_push_run_records_an_allowlist_refusal_per_candidate_without_crashing(fake, monkeypatch):
    monkeypatch.setenv("FR_WRITE_CUSTOMER_IDS", "1")
    candidates = [_event_candidate("FA9001"), _event_candidate("FA9002")]
    summary = await push.push_run(candidates, today=TODAY, dry_run=False)
    assert summary.created == 0
    assert len(summary.errors) == 2  # both refused, run did not crash


# --- build_customer_params / build_note_text / build_task_text -----------


def test_build_customer_params_uses_facility_name_when_owner_missing(fake):
    c = _event_candidate(header=None)
    params = push.build_customer_params(c)
    assert params["lname"] == "TEST MARKET"
    assert params["fname"] == ""
    assert "phone1" not in params


def test_build_task_text_names_the_pest_and_the_offer():
    c = _event_candidate(tier_kind="rodent")
    text = push.build_task_text(c)
    assert "Rodent" in text
    assert "Commercial Rodent Risk Audit" in text


def test_territory_task_text_says_audit_offer_not_call():
    c = _territory_candidate()
    text = push.build_task_text(c)
    assert text.startswith("Audit offer:")


def test_note_text_never_exceeds_900_chars_and_carries_the_pkey():
    c = _event_candidate()
    text = push.build_note_text(c)
    assert len(text) <= 900
    assert c.signal.pkey in text
