"""Write guards for the Google Ads mutate tools.

Google publishes no read-only Ads scope, so the credential this server holds is
always write-capable. The read-only-ness of the upstream tools is a property of
the tool surface, not of the token. Once mutate tools exist, *these guards are
the entire safety story*. Mirrors the layered model in `src/fr_mcp/server.py`:

1. `GADS_WRITES` -- a deployment-wide master switch, off by default here
   (the FieldRoutes server defaults on; this one moves money, so it does not).
2. `GADS_WRITE_CUSTOMER_IDS` -- an allowlist of customer IDs writes may touch.
   Point it at the test account while validating.
3. `validate_only` -- a per-call dry run, on by default for every mutate tool,
   so the caller must consciously ask for a real write.
4. A budget-change ceiling, because `amount_micros` makes a 1000x slip easy.

There is deliberately no way to disable 3 and 4 via the environment: they are
per-call arguments, so an agent has to say out loud that it means it.
"""

from __future__ import annotations

import os
import re

from fastmcp.exceptions import ToolError

ENV_WRITES = "GADS_WRITES"
ENV_ALLOWLIST = "GADS_WRITE_CUSTOMER_IDS"
ENV_BUDGET_MAX_MULTIPLE = "GADS_BUDGET_MAX_MULTIPLE"

# A budget change beyond this multiple of the current daily amount is refused
# unless the caller passes allow_large_change=True. $1.00 = 1,000,000 micros, so
# a misplaced factor of a thousand is both easy to type and expensive to run.
DEFAULT_BUDGET_MAX_MULTIPLE = 3.0

# One million micros is one currency unit (verified against the proto comment on
# CampaignBudget.amount_micros).
MICROS_PER_UNIT = 1_000_000


def clean_customer_id(customer_id: str | int) -> str:
    """Strips formatting from a customer ID: 123-456-7890 -> 1234567890."""
    return re.sub(r"\D", "", str(customer_id))


def writes_enabled() -> bool:
    """Whether this deployment permits any mutate at all. Defaults to off."""
    return os.environ.get(ENV_WRITES, "off").strip().lower() in ("on", "1", "true", "yes")


def require_writes(tool: str) -> None:
    if not writes_enabled():
        raise ToolError(
            f"{tool}: writes are disabled on this deployment ({ENV_WRITES} is not 'on'). "
            "No change was made."
        )


def write_allowlist() -> set[str]:
    """Customer IDs writes are restricted to. Empty set means unrestricted."""
    raw = os.environ.get(ENV_ALLOWLIST, "").strip()
    if not raw:
        return set()
    return {clean_customer_id(part) for part in raw.split(",") if clean_customer_id(part)}


def require_customer_allowed(tool: str, customer_id: str | int | None) -> str:
    """Refuses a write that targets a customer outside the allowlist.

    Fails closed: when the allowlist is set and the customer is unknown, the
    write is refused rather than allowed through.
    """
    cid = clean_customer_id(customer_id) if customer_id is not None else ""
    if not cid:
        raise ToolError(f"{tool}: no customer_id resolved for this write; refusing.")

    allowed = write_allowlist()
    if allowed and cid not in allowed:
        raise ToolError(
            f"{tool}: writes are restricted to {sorted(allowed)} "
            f"({ENV_ALLOWLIST} is set); this write targets {cid}. No change was made."
        )
    return cid


def budget_max_multiple() -> float:
    raw = os.environ.get(ENV_BUDGET_MAX_MULTIPLE, "").strip()
    if not raw:
        return DEFAULT_BUDGET_MAX_MULTIPLE
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_BUDGET_MAX_MULTIPLE
    return value if value > 0 else DEFAULT_BUDGET_MAX_MULTIPLE


def format_micros(micros: int) -> str:
    """1_500_000 -> '1.50' (currency units), for human-readable errors."""
    return f"{micros / MICROS_PER_UNIT:,.2f}"


def check_budget_change(
    tool: str,
    current_micros: int,
    new_micros: int,
    allow_large_change: bool = False,
) -> None:
    """Refuses an implausibly large budget change unless explicitly allowed.

    Guards the 1000x-slip case in both directions: `amount_micros` means a daily
    budget of $50 is written as 50000000, and typing $50 directly would cut the
    budget to five thousandths of a cent.
    """
    if new_micros < 0:
        raise ToolError(f"{tool}: a negative budget ({new_micros} micros) is not valid.")
    if new_micros == 0:
        raise ToolError(
            f"{tool}: refusing to set a budget of zero. To stop spend, pause the "
            "campaign instead."
        )
    if allow_large_change or current_micros <= 0:
        return

    limit = budget_max_multiple()
    ratio = new_micros / current_micros
    if ratio > limit or ratio < (1 / limit):
        direction = "increase" if ratio > 1 else "decrease"
        raise ToolError(
            f"{tool}: refusing a {ratio:.2f}x {direction} "
            f"({format_micros(current_micros)} -> {format_micros(new_micros)} per day), "
            f"which is outside the {limit:g}x safety ceiling. "
            f"Remember amount_micros: 1 unit of currency = {MICROS_PER_UNIT:,} micros, "
            f"so {format_micros(new_micros)} is written as {new_micros:,}. "
            "If this is genuinely intended, confirm with the user and retry with "
            "allow_large_change=True."
        )


def guard_state() -> dict:
    """Current guard configuration, for health output."""
    allowed = sorted(write_allowlist())
    return {
        "writes": "on" if writes_enabled() else "off",
        "writeCustomerAllowlist": allowed or None,
        "budgetMaxMultiple": budget_max_multiple(),
    }
