"""Pest classification from an inspection report's VERMIN AND ANIMAL CONTAMINATION
observation text: names the pest for the pitch and the task text, catches
"no provider"/"live evidence" phrasing, and pulls the quotable line.

Plan reference: docs/lead-scraper-plan.md section 3.3. Rules are regex, not ML --
tuned against the real PDF text extracted while planning (see
tests/fixtures/sacemd_reports.py), and meant to be adjusted from Sean's task
completion notes over time (phase 4 re-weighting), not treated as final.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CATEGORY_HEADING = "VERMIN AND ANIMAL CONTAMINATION"
# Any inspection-report numbered heading, e.g. "23.VERMIN AND ANIMAL CONTAMINATION" or
# "35.EQUIPMENT APPROVED AND MAINTAINED" -- used to find the end of one violation's text.
_HEADING_RE = re.compile(r"\n\s*\d+[a-z]?\.[A-Z][A-Z ,/&\-]{4,}")
_CODE_DESC_RE = re.compile(r"Code Description:.*", re.S)

_RODENT_RE = re.compile(
    r"\b(rodents?|rats?|mice|mouse|gnaw(?:ed|ing|s)?|burrows?|rub marks|snap traps?"
    r"|rodent (?:bait|trap|activity)|urine (?:stains?|odor))\b",
    re.I,
)
# "droppings" alone is ambiguous (bird/insect droppings exist too) -- only counts as a
# rodent signal when the nearest pest word within three words isn't an insect/bird one.
_DROPPINGS_RE = re.compile(r"\bdroppings\b", re.I)
_NON_RODENT_DROPPING_CONTEXT_RE = re.compile(
    r"\b(?:roach(?:es)?|cockroach\w*|insects?|flies?|birds?|pigeons?)\W+(?:\w+\W+){0,2}droppings"
    r"|droppings\W+(?:\w+\W+){0,2}(?:roach(?:es)?|cockroach\w*|insects?|flies?|birds?|pigeons?)\b",
    re.I,
)
_COCKROACH_RE = re.compile(r"\b(cockroach\w*|roach\w*|nymphs?|egg cases?|ootheca)\b", re.I)
_OTHER_INSECT_RE = re.compile(
    r"\b(fly|flies|fruit fl\w+|drain fl\w+|gnats?|ants?|maggots?|pupae?)\b", re.I
)
_NO_PCO_RE = re.compile(
    r"no pest control|invoice could not be located|no service records|could not (?:be )?locat\w* .{0,30}invoice",
    re.I,
)
_HAS_PCO_RE = re.compile(r"pest control company|serviced by|invoice from|pest control service invoice", re.I)
_LIVE_EVIDENCE_RE = re.compile(r"\b(live|adult|activity|fresh|nesting)\b", re.I)


@dataclass(frozen=True)
class Classification:
    pests: tuple[str, ...]  # subset of ("rodent", "cockroach", "other_insect"), best-first
    label: str  # "rodent", "cockroach", "rodent + cockroach", "other_insect", or "unclassified"
    no_pco: bool
    has_pco: bool
    live_evidence: bool
    quote: str  # up to 200 chars of the observation text, for the note/task

    @property
    def is_rodent(self) -> bool:
        return "rodent" in self.pests

    @property
    def is_cockroach(self) -> bool:
        return "cockroach" in self.pests


UNCLASSIFIED = Classification((), "unclassified", False, False, False, "")


def _strip_code_description(block: str) -> str:
    """Drop the CalCode boilerplate -- it contains "vermin", "rodents" and "insects"
    itself and would otherwise match the same regexes as a real observation."""
    block = _CODE_DESC_RE.sub("", block)
    # Boilerplate ("shall") sentences can precede "Code Description:" too (seen live);
    # drop any sentence containing " shall " rather than relying on the heading alone.
    sentences = re.split(r"(?<=[.!?])\s+", block)
    return " ".join(s for s in sentences if " shall " not in s)


def extract_vermin_blocks(report_text: str) -> list[str]:
    """Every VERMIN AND ANIMAL CONTAMINATION violation block's Observations text
    (Code Description and "shall" boilerplate stripped), across all pages."""
    blocks: list[str] = []
    for m in re.finditer(re.escape(CATEGORY_HEADING), report_text):
        start = m.end()
        rest = report_text[start:]
        end_m = _HEADING_RE.search(rest)
        raw = rest[: end_m.start()] if end_m else rest
        obs_m = re.search(r"Observations:\s*(.*)", raw, re.S)
        text = obs_m.group(1) if obs_m else raw
        cleaned = _strip_code_description(text).strip()
        if cleaned:
            blocks.append(cleaned)
    return blocks


def _has_rodent(text: str) -> bool:
    if _RODENT_RE.search(text):
        return True
    for m in _DROPPINGS_RE.finditer(text):
        window = text[max(0, m.start() - 40) : m.end() + 40]
        if not _NON_RODENT_DROPPING_CONTEXT_RE.search(window):
            return True
    return False


def classify_text(text: str) -> Classification:
    """Classify a single VERMIN block's already-cleaned observation text."""
    if not text.strip():
        return UNCLASSIFIED
    pests: list[str] = []
    if _has_rodent(text):
        pests.append("rodent")
    if _COCKROACH_RE.search(text):
        pests.append("cockroach")
    if not pests and _OTHER_INSECT_RE.search(text):
        pests.append("other_insect")
    if "rodent" in pests and "cockroach" in pests:
        label = "rodent + cockroach"
    elif pests:
        label = pests[0]
    else:
        label = "unclassified"
    no_pco = bool(_NO_PCO_RE.search(text))
    # "pest control service invoice could not be located" matches both regexes on the
    # same phrase (verified against a real report: NATOMAS FOOD & LIQUOR) -- an absent
    # invoice is unambiguously a no_pco case, so no_pco wins when both fire.
    has_pco = bool(_HAS_PCO_RE.search(text)) and not no_pco
    return Classification(
        pests=tuple(pests),
        label=label,
        no_pco=no_pco,
        has_pco=has_pco,
        live_evidence=bool(_LIVE_EVIDENCE_RE.search(text)),
        quote=" ".join(text.split())[:200],
    )


def classify_report(report_text: str) -> Classification:
    """Classify a whole report: combines every VERMIN block (a report can have more
    than one, e.g. a routine plus a reinspection page), rodent findings win the label
    when both pests appear anywhere in the report."""
    blocks = extract_vermin_blocks(report_text)
    if not blocks:
        return UNCLASSIFIED
    classified = [classify_text(b) for b in blocks]
    pests: set[str] = set()
    for c in classified:
        pests.update(c.pests)
    ordered = [p for p in ("rodent", "cockroach", "other_insect") if p in pests]
    if "rodent" in ordered and "cockroach" in ordered:
        label = "rodent + cockroach"
    elif ordered:
        label = ordered[0]
    else:
        label = "unclassified"
    # The quote and evidence flags come from whichever block actually named a pest,
    # preferring rodent evidence when more than one block qualifies.
    best = next((c for c in classified if c.pests), classified[0])
    return Classification(
        pests=tuple(ordered),
        label=label,
        no_pco=any(c.no_pco for c in classified),
        has_pco=any(c.has_pco for c in classified),
        live_evidence=any(c.live_evidence for c in classified),
        quote=best.quote,
    )
