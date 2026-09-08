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
