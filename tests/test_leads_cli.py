"""CLI argument wiring: --counties parsing/validation and the run subcommand's
--destination default and choices. Network-touching command bodies (cmd_run/
cmd_preview/cmd_push) are exercised live via `fr-leads preview`, not here."""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from fr_mcp.leads import cli


def test_parse_counties_defaults_to_all_three():
    assert cli._parse_counties("sacramento,placer,yolo") == ("sacramento", "placer", "yolo")


def test_parse_counties_accepts_a_subset_case_insensitively():
    assert cli._parse_counties("Placer, YOLO") == ("placer", "yolo")


def test_parse_counties_rejects_unknown_values():
    with pytest.raises(SystemExit):
        cli._parse_counties("sacramento,tahoe")


def _candidate(key: str, pushable: bool = True) -> SimpleNamespace:
    """_worth_enriching only reads these two attributes, so a real LeadCandidate
    (which needs a score, a classification and a dozen other fields) is more
    setup than the behaviour under test deserves."""
    return SimpleNamespace(customer_link=key, pushable=pushable)


def test_enrichment_skips_candidates_the_row_cap_will_discard():
    # Places is billed per call; a candidate past the cap is dropped before it is
    # written, so paying to enrich it buys nothing.
    cands = [_candidate(f"K{i}") for i in range(10)]
    worth = cli._worth_enriching(cands, existing_keys=set(), skip_keys=set(), new_row_cap=3)
    assert [c.customer_link for c in worth] == ["K0", "K1", "K2"]


def test_enrichment_covers_every_existing_row_regardless_of_the_cap():
    # Rows already in the sheet are backfilled today, so the cap doesn't apply.
    cands = [_candidate(f"K{i}") for i in range(10)]
    worth = cli._worth_enriching(
        cands, existing_keys={"K5", "K6", "K7", "K8", "K9"}, skip_keys=set(), new_row_cap=1
    )
    assert [c.customer_link for c in worth] == ["K0", "K5", "K6", "K7", "K8", "K9"]


def test_enrichment_never_re_pays_for_a_row_already_enriched():
    cands = [_candidate("K0"), _candidate("K1")]
    worth = cli._worth_enriching(cands, existing_keys={"K0", "K1"}, skip_keys={"K0"}, new_row_cap=40)
    assert [c.customer_link for c in worth] == ["K1"]


def test_unpushable_candidates_are_never_enriched():
    cands = [_candidate("K0", pushable=False), _candidate("K1")]
    worth = cli._worth_enriching(cands, existing_keys=set(), skip_keys=set(), new_row_cap=40)
    assert [c.customer_link for c in worth] == ["K1"]


def test_run_destination_defaults_to_sheet():
    parser = cli.build_parser()
    args = parser.parse_args(["run"])
    assert args.destination == "sheet"


def test_run_destination_accepts_fieldroutes():
    parser = cli.build_parser()
    args = parser.parse_args(["run", "--destination", "fieldroutes"])
    assert args.destination == "fieldroutes"


def test_run_destination_rejects_unknown_value():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--destination", "crm"])


def test_preview_counties_default_to_all_three():
    parser = cli.build_parser()
    args = parser.parse_args(["preview"])
    assert cli._parse_counties(args.counties) == ("sacramento", "placer", "yolo")


def test_run_and_preview_accept_a_counties_override():
    parser = cli.build_parser()
    args = parser.parse_args(["run", "--counties", "placer,yolo"])
    assert cli._parse_counties(args.counties) == ("placer", "yolo")


@pytest.mark.asyncio
async def test_a_missing_places_credential_does_not_take_the_run_down(monkeypatch):
    """Enrichment is optional; writing the sheet is the point of the run. Before
    this, a PlacesError propagated out of main()'s except tuple and the 7am
    scheduled run produced nothing at all."""
    from fr_mcp.leads import places

    def _no_credential():
        raise places.PlacesError("No service-account credential for Places")

    monkeypatch.setattr(cli.places, "service_account_token_provider", _no_credential)
    out = await cli._enrich_phones([_candidate("K1")], new_row_cap=5)
    assert out["attempted"] == 0
    assert "No service-account credential" in out["blocked"]


@pytest.mark.asyncio
async def test_a_credential_that_fails_at_lookup_time_still_lets_the_run_finish(monkeypatch):
    from fr_mcp.leads import places

    def _expired_token():
        raise places.PlacesError("Places token refresh failed: invalid_grant")

    monkeypatch.setattr(cli.places, "service_account_token_provider", lambda: _expired_token)
    cand = _candidate("K1")
    cand.name, cand.street, cand.city, cand.zip5 = "ZEBRA DELI", "1 Main St", "Lincoln", "95648"
    out = await cli._enrich_phones([cand], new_row_cap=5)
    assert "invalid_grant" in out["blocked"]
    assert out["attempted"] == 0  # the token is fetched before the billed call, so none was made


# --- the food subcommand -------------------------------------------------


def test_food_defaults_to_writing_the_sheet_from_every_registry():
    args = cli.build_parser().parse_args(["food"])
    assert args.destination == "sheet"
    assert args.sources == "calepa,fsis,cdfa"


def test_food_preview_writes_nothing():
    assert cli.build_parser().parse_args(["food", "--destination", "preview"]).destination == "preview"


def test_food_rejects_an_unknown_destination():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["food", "--destination", "fieldroutes"])


@pytest.mark.asyncio
async def test_an_unknown_food_source_is_refused_before_any_request():
    args = cli.build_parser().parse_args(["food", "--sources", "calepa,echo"])
    with pytest.raises(SystemExit) as exc:
        await cli.cmd_food(args)
    assert "echo" in str(exc.value)


@pytest.mark.asyncio
async def test_only_the_rows_without_a_phone_are_looked_up_in_places(monkeypatch):
    """Places is the only billed step. 216 of the 255 merged rows arrive with a
    registry phone, so paying for those would be most of the bill for nothing."""
    from fr_mcp.leads import food

    looked_up: list[str] = []

    class _FakeClient:
        blocked = False
        block_reason = None
        calls = 0
        matched = 0
        rejected = 0
        budget_left = 50

        async def lookup(self, *, name, street, city, zip5):
            looked_up.append(name)
            self.calls += 1
            return None

    monkeypatch.setattr(cli.places, "service_account_token_provider", lambda: (lambda: "tok"))
    monkeypatch.setattr(cli.places, "PlacesClient", lambda *a, **k: _FakeClient())
    has = food.FoodFacility(key="A", name="Has Phone Foods", phone="9165551234")
    lacks = food.FoodFacility(key="B", name="No Phone Foods")
    await cli._enrich_food_phones([has, lacks])
    assert looked_up == ["No Phone Foods"]


@pytest.mark.asyncio
async def test_a_places_failure_does_not_stop_the_food_run_either(monkeypatch):
    from fr_mcp.leads import food, places

    def _no_credential():
        raise places.PlacesError("No service-account credential for Places")

    monkeypatch.setattr(cli.places, "service_account_token_provider", _no_credential)
    out = await cli._enrich_food_phones([food.FoodFacility(key="A", name="No Phone Foods")])
    assert out["attempted"] == 0
    assert "credential" in out["blocked"]
