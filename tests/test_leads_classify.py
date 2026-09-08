"""Pest classification against real Sacramento County inspection report text
(tests/leads_fixtures.py) and a few synthetic edge cases for the rules that
real fixtures don't happen to exercise."""

from __future__ import annotations

from fr_mcp.leads.classify import UNCLASSIFIED, classify_report, classify_text, extract_vermin_blocks

import leads_fixtures as fx


def test_natomas_is_rodent_with_no_provider_and_no_live_evidence():
    c = classify_report(fx.NATOMAS)
    assert c.label == "rodent"
    assert c.pests == ("rodent",)
    assert c.no_pco is True
    assert c.has_pco is False  # "invoice could not be located" must not also read as has_pco
    assert c.live_evidence is False  # droppings only, nothing described as live
    assert "rodent droppings" in c.quote.lower()


def test_seapot_is_cockroach_with_live_evidence():
    c = classify_report(fx.SEAPOT)
    assert c.label == "cockroach"
    assert c.live_evidence is True
    assert c.no_pco is False and c.has_pco is False


def test_kfc_page1_has_no_vermin_block():
    assert extract_vermin_blocks(fx.KFC_AW_PAGE1) == []
    assert classify_report(fx.KFC_AW_PAGE1) == UNCLASSIFIED


def test_kfc_vermin_block_is_cockroach_despite_a_fly_mention():
    c = classify_report(fx.KFC_AW_VERMIN)
    assert c.label == "cockroach"
    assert "cockroach" in c.pests


def test_curries_is_cockroach_closure():
    c = classify_report(fx.CURRIES)
    assert c.label == "cockroach"
    assert c.live_evidence is True


def test_divine_pacific_is_cockroach_with_no_confirmed_provider():
    # "email proof of pest control service to mataj@saccounty.gov" is the county telling
    # the operator to go get service and prove it afterward -- not confirmation that a
    # provider is already on contract, so this should read the same as NATOMAS's
    # "invoice could not be located": no confirmed pest-control relationship on file.
    c = classify_report(fx.DIVINE)
    assert c.label == "cockroach"
    assert c.has_pco is False
    assert c.no_pco is False  # neither phrase from the no_pco regex list appears verbatim


def test_food4less_is_other_insect_not_cockroach_or_rodent():
    c = classify_report(fx.FOOD4LESS)
    assert c.label == "other_insect"
    assert c.pests == ("other_insect",)


def test_buds_buffet_has_no_vermin_section_at_all():
    assert classify_report(fx.BUDS_NO_VERMIN) == UNCLASSIFIED


def test_code_description_boilerplate_is_not_mistaken_for_a_finding():
    # The boilerplate itself says "rodents and insects" -- a block with only
    # boilerplate (no real observation) must not classify as anything.
    text = (
        "23.VERMIN AND ANIMAL CONTAMINATION\n"
        "Observations: No violations noted.\n"
        "Code Description: A food facility shall at all times be equipped, maintained, and operated "
        "as to prevent the entrance and harborage of animals, birds, and vermin, including rodents and insects.\n"
        "35.EQUIPMENT APPROVED AND MAINTAINED\nObservations: fine."
    )
    c = classify_report(text)
    assert c.label == "unclassified"


def test_droppings_near_a_roach_mention_do_not_count_as_rodent():
    text = "Observed roach droppings near the sink and dead cockroach bodies on the floor."
    c = classify_text(text)
    assert "rodent" not in c.pests
    assert "cockroach" in c.pests


def test_droppings_alone_with_no_insect_context_counts_as_rodent():
    text = "Fresh rodent droppings observed under the prep table."
    c = classify_text(text)
    assert "rodent" in c.pests


def test_mixed_rodent_and_cockroach_labels_both():
    text = "Rodent droppings observed near the pantry. Two live German cockroaches under the sink."
    c = classify_text(text)
    assert c.label == "rodent + cockroach"
    assert set(c.pests) == {"rodent", "cockroach"}


def test_empty_text_is_unclassified():
    assert classify_text("") == UNCLASSIFIED
    assert classify_text("   ") == UNCLASSIFIED
