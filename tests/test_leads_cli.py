"""CLI argument wiring: --counties parsing/validation and the run subcommand's
--destination default and choices. Network-touching command bodies (cmd_run/
cmd_preview/cmd_push) are exercised live via `fr-leads preview`, not here."""

from __future__ import annotations

import pytest

from fr_mcp.leads import cli


def test_parse_counties_defaults_to_all_three():
    assert cli._parse_counties("sacramento,placer,yolo") == ("sacramento", "placer", "yolo")


def test_parse_counties_accepts_a_subset_case_insensitively():
    assert cli._parse_counties("Placer, YOLO") == ("placer", "yolo")


def test_parse_counties_rejects_unknown_values():
    with pytest.raises(SystemExit):
        cli._parse_counties("sacramento,tahoe")


def _candidate(key: str, pushable: bool = True):
    class _C:
        customer_link = key
        pass_ = pushable

        @property
        def pushable(self):
            return self.pass_

    return _C()


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
