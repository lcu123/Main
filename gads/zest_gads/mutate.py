"""Guarded write tools for Google Ads.

Mounted as the `mutate` namespace. Note that the namespace is *absent* from the
bundled `ads_mcp/tools_config.yaml`, and a namespace missing from an explicit
config resolves to disabled -- so these tools do not exist at all unless the
deployment points `GOOGLE_ADS_MCP_TOOLS_CONFIG` at a config that enables them.
That is a deliberate second interlock; keep it.

Three library behaviours below were verified by executing google-ads 32.0.0
against an intercepted transport. Each is a silent-failure trap:

1. `validate_only` is NOT a keyword argument on the flattened GAPIC methods --
   `mutate_campaigns(customer_id=..., operations=..., validate_only=True)` raises
   TypeError. It only exists on the *request object*. A `**kwargs` wrapper would
   drop it and perform a live mutate while reporting a dry run. Every call here
   builds an explicit request.
2. `protobuf_helpers.field_mask` compares values, not presence. `original` is a
   cleared copy, so any field set to its zero value compares equal and is
   omitted from the mask -- `cpc_bid_micros = 0` yields a successful no-op.
   `_assert_masked()` turns that into a raise.
3. `resource_name` appearing in the mask is correct and canonical (Google's own
   `update_campaign.py` produces the same mask). Do not "fix" it.
"""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from google.api_core import protobuf_helpers
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils

from . import guards

mutate_mcp = FastMCP("mutate")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _assert_masked(mask, *required: str) -> None:
    """Guards trap 2: a zero value silently dropped from the update mask."""
    missing = [field for field in required if field not in mask.paths]
    if missing:
        raise ToolError(
            f"Refusing to send a mutate whose update mask is missing {missing}. "
            "This happens when a field is set to its zero value, which "
            "protobuf_helpers.field_mask omits -- the API would accept the "
            "request and change nothing. No change was made."
        )


def _describe_ads_error(exc: GoogleAdsException) -> str:
    parts = []
    for error in exc.failure.errors:
        code = error.error_code
        # error_code is a oneof; the set field names the error family.
        which = code._pb.WhichOneof("error_code") if hasattr(code, "_pb") else None
        detail = getattr(code, which, None) if which else None
        label = f"{which}.{detail.name}" if which and hasattr(detail, "name") else which
        parts.append(f"{label}: {error.message}" if label else error.message)
    return f"Google Ads rejected the request (request_id={exc.request_id}): " + "; ".join(parts)


def _run(service_name: str, method: str, request) -> Any:
    """Executes a mutate, converting GoogleAdsException into a ToolError."""
    service = utils.get_googleads_service(service_name)
    try:
        return getattr(service, method)(request=request)
    except GoogleAdsException as exc:
        raise ToolError(_describe_ads_error(exc)) from exc


def _outcome(response, confirm: bool, what: str) -> dict:
    """Shapes a uniform result. Under validate_only the API returns no results."""
    if not confirm:
        return {
            "status": "validated",
            "applied": False,
            "what": what,
            "note": (
                "Dry run only -- Google validated this request and did not execute it. "
                "Confirm with the user, then retry with confirm=True to apply it."
            ),
        }
    results = [
        {"resourceName": r.resource_name} for r in getattr(response, "results", [])
    ]
    return {"status": "applied", "applied": True, "what": what, "results": results}


def _prepare(client, request, confirm: bool) -> None:
    """Sets the control fields every mutate request shares.

    partial_failure stays False: these are single-operation writes, and
    all-or-nothing keeps error handling on the exception path. With it on, a
    rejected operation returns a *success* response you must remember to inspect
    -- the exact shape of bug that silently loses a write.
    """
    request.partial_failure = False
    request.validate_only = not confirm
    request.response_content_type = client.enums.ResponseContentTypeEnum.RESOURCE_NAME_ONLY


def _budget_for_campaign(customer_id: str, campaign_budget_id: str) -> dict:
    """Reads a budget's current daily amount, for the change-ceiling check."""
    service = utils.get_googleads_service("GoogleAdsService")
    query = (
        "SELECT campaign_budget.id, campaign_budget.name, "
        "campaign_budget.amount_micros FROM campaign_budget "
        f"WHERE campaign_budget.id = {int(campaign_budget_id)}"
    )
    for batch in service.search_stream(customer_id=customer_id, query=query):
        for row in batch.results:
            return {
                "id": str(row.campaign_budget.id),
                "name": row.campaign_budget.name,
                "amountMicros": int(row.campaign_budget.amount_micros),
            }
    raise ToolError(
        f"No campaign budget with id {campaign_budget_id} on customer {customer_id}. "
        "Use the search tool to list campaign_budget rows first."
    )


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------


@mutate_mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def set_campaign_status(
    customer_id: str,
    campaign_id: str,
    status: Literal["ENABLED", "PAUSED"],
    confirm: bool = False,
) -> dict:
    """Pause or re-enable a campaign. Pausing stops its ads serving immediately.

    Defaults to a dry run: leave confirm=False to validate the change without
    applying it. Always confirm with the user before calling with confirm=True --
    this changes what is running on a live advertising account.
    """
    guards.require_writes("set_campaign_status")
    cid = guards.require_customer_allowed("set_campaign_status", customer_id)

    client = utils.get_googleads_client()
    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = client.get_service("CampaignService").campaign_path(
        cid, campaign_id
    )
    campaign.status = client.enums.CampaignStatusEnum[status]
    # status is never zero (ENABLED=2, PAUSED=3), so trap 2 cannot bite here --
    # asserted anyway so a future edit cannot reintroduce it silently.
    operation.update_mask.CopyFrom(protobuf_helpers.field_mask(None, campaign._pb))
    _assert_masked(operation.update_mask, "status")

    request = client.get_type("MutateCampaignsRequest")
    request.customer_id = cid
    request.operations = [operation]
    _prepare(client, request, confirm)

    response = _run("CampaignService", "mutate_campaigns", request)
    return _outcome(response, confirm, f"campaign {campaign_id} -> {status}")


@mutate_mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def update_campaign_budget(
    customer_id: str,
    campaign_budget_id: str,
    daily_amount: float,
    confirm: bool = False,
    allow_large_change: bool = False,
) -> dict:
    """Change a campaign budget's daily amount, in whole currency units (e.g. 75.50).

    Pass the amount the way a person says it -- 75.50 means $75.50 per day. Do
    not pass micros; this tool converts. Changes beyond 3x the current budget in
    either direction are refused unless allow_large_change=True.

    Defaults to a dry run. This moves real money: confirm the new amount with the
    user before calling with confirm=True.
    """
    guards.require_writes("update_campaign_budget")
    cid = guards.require_customer_allowed("update_campaign_budget", customer_id)

    if daily_amount <= 0:
        raise ToolError(
            "update_campaign_budget: daily_amount must be positive. To stop spend, "
            "pause the campaign instead."
        )

    current = _budget_for_campaign(cid, campaign_budget_id)
    new_micros = int(round(daily_amount * guards.MICROS_PER_UNIT))
    guards.check_budget_change(
        "update_campaign_budget", current["amountMicros"], new_micros, allow_large_change
    )

    client = utils.get_googleads_client()
    operation = client.get_type("CampaignBudgetOperation")
    budget = operation.update
    budget.resource_name = client.get_service(
        "CampaignBudgetService"
    ).campaign_budget_path(cid, campaign_budget_id)
    budget.amount_micros = new_micros
    operation.update_mask.CopyFrom(protobuf_helpers.field_mask(None, budget._pb))
    # amount_micros is exactly the field trap 2 destroys, so this assert matters.
    _assert_masked(operation.update_mask, "amount_micros")

    request = client.get_type("MutateCampaignBudgetsRequest")
    request.customer_id = cid
    request.operations = [operation]
    _prepare(client, request, confirm)

    response = _run("CampaignBudgetService", "mutate_campaign_budgets", request)
    outcome = _outcome(
        response,
        confirm,
        f"budget {campaign_budget_id} ({current['name']}): "
        f"{guards.format_micros(current['amountMicros'])} -> "
        f"{guards.format_micros(new_micros)} per day",
    )
    outcome["previousDailyAmount"] = guards.format_micros(current["amountMicros"])
    outcome["newDailyAmount"] = guards.format_micros(new_micros)
    return outcome


@mutate_mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def set_ad_group_status(
    customer_id: str,
    ad_group_id: str,
    status: Literal["ENABLED", "PAUSED"],
    confirm: bool = False,
) -> dict:
    """Pause or re-enable an ad group within a campaign.

    Defaults to a dry run. Confirm with the user before calling with confirm=True.
    """
    guards.require_writes("set_ad_group_status")
    cid = guards.require_customer_allowed("set_ad_group_status", customer_id)

    client = utils.get_googleads_client()
    operation = client.get_type("AdGroupOperation")
    ad_group = operation.update
    ad_group.resource_name = client.get_service("AdGroupService").ad_group_path(
        cid, ad_group_id
    )
    ad_group.status = client.enums.AdGroupStatusEnum[status]
    operation.update_mask.CopyFrom(protobuf_helpers.field_mask(None, ad_group._pb))
    _assert_masked(operation.update_mask, "status")

    request = client.get_type("MutateAdGroupsRequest")
    request.customer_id = cid
    request.operations = [operation]
    _prepare(client, request, confirm)

    response = _run("AdGroupService", "mutate_ad_groups", request)
    return _outcome(response, confirm, f"ad group {ad_group_id} -> {status}")


@mutate_mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def add_negative_keyword(
    customer_id: str,
    ad_group_id: str,
    keyword: str,
    match_type: Literal["EXACT", "PHRASE", "BROAD"] = "PHRASE",
    confirm: bool = False,
) -> dict:
    """Add a negative keyword to an ad group, so its ads stop matching that term.

    Useful for cutting irrelevant traffic -- e.g. excluding "jobs" or "free" from
    a lawn-care campaign. Defaults to a dry run; confirm with the user before
    calling with confirm=True.
    """
    guards.require_writes("add_negative_keyword")
    cid = guards.require_customer_allowed("add_negative_keyword", customer_id)

    term = keyword.strip()
    if not term:
        raise ToolError("add_negative_keyword: keyword is empty.")

    client = utils.get_googleads_client()
    operation = client.get_type("AdGroupCriterionOperation")
    # Negative keywords are create-only: AdGroupCriterion.negative is immutable,
    # so there is no "make this keyword negative" update. To reverse one, remove
    # the criterion rather than flipping the flag.
    criterion = operation.create
    criterion.ad_group = client.get_service("AdGroupService").ad_group_path(cid, ad_group_id)
    criterion.negative = True
    criterion.keyword.text = term
    criterion.keyword.match_type = client.enums.KeywordMatchTypeEnum[match_type]

    request = client.get_type("MutateAdGroupCriteriaRequest")
    request.customer_id = cid
    request.operations = [operation]
    _prepare(client, request, confirm)

    # Note the method name: mutate_ad_group_criteria, not ..._criterions.
    response = _run("AdGroupCriterionService", "mutate_ad_group_criteria", request)
    return _outcome(
        response, confirm, f"negative {match_type} keyword {term!r} on ad group {ad_group_id}"
    )
