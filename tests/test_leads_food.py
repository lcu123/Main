"""The Food Facilities model: classification when the registry's own category is
wrong, the retail exclusion that keeps this tab out of the Leads tab's territory,
and the address-based merge that folds one plant's several registry rows together.

Every awkward case below was seen in a live 2026-09-09 pull.
"""

from __future__ import annotations

from fr_mcp.leads import calepa, cdfa, food, places


def _site(**kw) -> calepa.Site:
    base = dict(
        site_id="1", name="X", address="100 MAIN ST", city="Sacramento", zip5="95814",
        lat=38.58, lng=-121.49, naics_prefix="311",
    )
    base.update(kw)
    return calepa.Site(**base)


def _licensee(**kw) -> cdfa.Licensee:
    base = dict(
        license_number="1", name="X", address="100 MAIN ST", city="Sacramento",
        state="CA", zip5="95814", phone="9165551234", expires="2027-06-30",
    )
    base.update(kw)
    return cdfa.Licensee(**base)


# --- classification ------------------------------------------------------


def test_the_name_beats_the_registry_code_because_the_code_is_often_wrong():
    """CalEPA files J W AUTO WRECKERS under NAICS 311, food manufacturing. Seen
    live; it is not a one-off, registry categories are entered by hand."""
    assert food.from_calepa(_site(name="J W AUTO WRECKERS")) is None


def test_a_recognisable_name_classifies_without_a_review_flag():
    row = food.from_calepa(_site(name="MARY ANN'S BAKING CO"))
    assert row.category == food.CAT_BAKERY
    assert row.needs_review is False


def test_a_name_that_says_nothing_falls_back_to_naics_and_is_flagged():
    """"MO West Sac" is a real NAICS 311 site. The code is all we have, so the
    category is a guess and the rep is told so rather than sold a certainty."""
    row = food.from_calepa(_site(name="MO West Sac", naics_prefix="3118"))
    assert row.category == food.CAT_BAKERY
    assert row.needs_review is True
    assert "NAICS" in row.review_reason


def test_specific_categories_are_tested_before_general_ones():
    assert food.category_for("BLUE DIAMOND GROWERS")[0] == food.CAT_PRODUCE
    assert food.category_for("UNITED STATES COLD STORAGE")[0] == food.CAT_COLD
    assert food.category_for("Sudwerk Privatbrauerei Hubsch")[0] == food.CAT_ALCOHOL
    # "Sysco Sacramento Inc." carries no trade word at all, so the name says
    # nothing and the NAICS prefix has to answer -- flagged, as it should be.
    assert food.category_for("Sysco Sacramento Inc.") == (food.CAT_OTHER, True, food.category_for("Sysco")[2])
    assert food.category_for("Sysco Sacramento Inc.", "4244")[0] == food.CAT_DISTRIBUTION


# --- what does not belong here -------------------------------------------


def test_restaurants_and_convenience_stores_go_to_the_leads_tab_not_this_one():
    for name in ("Chipotle Mexican Grill #108", "7-ELEVEN #14064", "Joe's Pizzeria"):
        assert food.excluded_reason(name) is not None
        assert food.from_calepa(_site(name=name)) is None


def test_bakeries_breweries_and_wineries_survive_the_retail_filter_by_decision():
    """The owner's call: they are in scope even though many also have a counter."""
    for name in ("Freeport Bakery Cafe", "Track 7 Brewing Taproom", "Bogle Winery Bar"):
        assert food.excluded_reason(name) is None


def test_an_obviously_non_food_business_is_dropped_whatever_the_code_says():
    for name in ("Capital Auto Wreckers", "Delta Lumber Supply", "Green Leaf Dispensary"):
        assert food.excluded_reason(name) is not None


# --- CDFA-specific shaping -----------------------------------------------


def test_an_individual_licensee_is_kept_but_flagged_as_maybe_having_no_facility():
    """Two thirds of CDFA's in-range rows are people, not plants -- produce brokers
    licensed personally. They are still worth a call, but a rep should know."""
    row = food.from_cdfa(_licensee(name="Gregory P Hardesty"))
    assert row.needs_review is True
    assert "individual" in row.review_reason
    assert row.category == food.CAT_HANDLER


def test_a_company_licensee_with_a_trade_word_is_not_flagged_as_an_individual():
    row = food.from_cdfa(_licensee(name="Saldana Bros Produce"))
    assert row.category == food.CAT_PRODUCE
    assert "individual" not in row.review_reason


def test_a_po_box_row_says_the_distance_is_where_the_mail_goes():
    row = food.from_cdfa(_licensee(name="Farmers Rice Cooperative", address="P.O. Box 15223", zip5="95851"))
    assert row.address_is_mailbox is True
    assert row.needs_review is True
    assert "PO Box" in row.review_reason
    assert row.distance_basis == "city"  # 95851 is a PO-Box zip with no ZCTA


def test_distance_prefers_real_coordinates_over_a_centroid():
    row = food.from_calepa(_site(lat=38.60, lng=-121.45))
    assert row.distance_basis == "coords"
    row = food.from_calepa(_site(lat=None, lng=None))
    assert row.distance_basis == "zip"


# --- merge ---------------------------------------------------------------


def test_one_plant_filed_twice_becomes_one_row_and_keeps_the_phone():
    """BLUE DIAMOND GROWERS is two CalEPA SiteIDs at one address -- one row carries
    the phone, the other does not. Two rows in the sheet is a rep calling twice."""
    a = food.from_calepa(_site(site_id="1", name="BLUE DIAMOND GROWERS", phone="", address="1802 C ST", zip5="95811"))
    b = food.from_calepa(
        _site(site_id="2", name="BLUE DIAMOND GROWERS", phone="9164420771", phone_role="Operator",
              address="1802 C ST", zip5="95811")
    )
    merged = food.merge([a, b])
    assert len(merged) == 1
    assert merged[0].phone == "9164420771"
    assert merged[0].key == "CALEPA:1"  # the first source to claim the address owns the key


def test_a_differently_named_row_at_the_same_address_still_merges():
    """"TONY'S FINE FOODS" and "TONY'S FINE FOODS/UNFI" are the same plant. Name
    matching would keep both; house number plus zip does not."""
    a = food.from_calepa(_site(site_id="1", name="TONY'S FINE FOODS/UNFI", address="4001 PARK RD", zip5="95691"))
    b = food.from_calepa(_site(site_id="2", name="TONY'S FINE FOODS", address="4001 Park Road", zip5="95691",
                               phone="9163744191", phone_role="Operator"))
    assert len(food.merge([a, b])) == 1


def test_two_sources_are_both_recorded_on_the_merged_row():
    a = food.from_calepa(_site(name="Sunwest Foods", address="1 Rice Way", zip5="95814"))
    b = food.from_cdfa(_licensee(name="Sunwest Foods Inc", address="1 Rice Way", zip5="95814"))
    merged = food.merge([a], [b])
    assert merged[0].sources == [food.SOURCE_CALEPA, food.SOURCE_CDFA]
    assert "CalEPA" in merged[0].found_via and "CDFA" in merged[0].found_via


def test_a_second_registry_confirming_a_guess_clears_the_review_flag():
    guessed = food.from_calepa(_site(name="MO West Sac", address="7 Dock St", zip5="95691"))
    named = food.from_cdfa(_licensee(name="MO West Sac Rice Milling", address="7 Dock St", zip5="95691"))
    assert guessed.needs_review is True
    assert food.merge([guessed], [named])[0].needs_review is False


def test_a_po_box_stays_flagged_even_when_a_second_source_agrees():
    """Corroboration answers "is this a real food business", not "is this address a
    place you can drive to"."""
    box = food.from_cdfa(_licensee(name="Farmers Rice Cooperative", address="P.O. Box 15223", zip5="95851"))
    other = food.from_cdfa(_licensee(license_number="2", name="Farmers Rice Cooperative",
                                     address="P.O. Box 15223", zip5="95851"))
    assert food.merge([box], [other])[0].needs_review is True


def test_rows_outside_the_radius_are_dropped_and_the_rest_sort_by_distance():
    near = food.from_calepa(_site(site_id="1", name="Near Foods", lat=38.70, lng=-121.46, address="1 A St"))
    mid = food.from_calepa(_site(site_id="2", name="Mid Foods", lat=38.55, lng=-121.30, address="2 B St"))
    far = food.from_calepa(_site(site_id="3", name="Far Foods", lat=37.95, lng=-121.29, address="3 C St"))
    merged = food.merge([near, mid, far])
    assert [f.name for f in merged] == ["Near Foods", "Mid Foods"]


def test_the_merge_key_falls_back_to_the_name_when_there_is_no_house_number():
    a = food.FoodFacility(key="A", name="Acme Foods, Inc. #2", address="", zip5="95814")
    b = food.FoodFacility(key="B", name="ACME FOODS", address="", zip5="95814")
    assert a.merge_key == b.merge_key


def test_person_held_licences_sink_below_the_plants_in_the_call_order():
    """A plain distance sort puts four individual produce brokers above Blue
    Diamond Growers. A rep working top-down should reach the plants first."""
    broker = food.from_cdfa(_licensee(name="Carina Suastegui-Ponce", address="1 Elm St", zip5="95673"))
    plant = food.from_calepa(_site(name="BLUE DIAMOND GROWERS", address="1802 C ST", zip5="95811",
                                   lat=38.584903, lng=-121.4944))
    assert broker.distance_miles < plant.distance_miles  # the broker really is closer
    assert [f.name for f in food.merge([plant], [broker])] == ["BLUE DIAMOND GROWERS", "Carina Suastegui-Ponce"]


def test_a_second_source_that_knows_the_address_as_a_site_promotes_the_row():
    broker = food.from_cdfa(_licensee(name="Andres Rivas", address="500 Dock St", zip5="95691"))
    site = food.from_calepa(_site(name="Rivas Packing Co", address="500 DOCK ST", zip5="95691"))
    assert broker.is_facility is False
    assert food.merge([broker], [site])[0].is_facility is True


# --- Google sweep rows ---------------------------------------------------


def _row(**kw) -> places.PlaceRow:
    base = dict(
        place_id="abc", name="Mary Ann's Baking Co",
        address="4010 Seaport Blvd, West Sacramento, CA 95691, USA",
        primary_type="bakery", phone="9166817444", website="https://example.test",
        business_status="OPERATIONAL", lat=38.58, lng=-121.49,
    )
    base.update(kw)
    return places.PlaceRow(**base)


def test_a_permanently_closed_listing_never_reaches_a_rep():
    """Google keeps closed listings for years; a rep calling one wastes the call."""
    assert food.from_places(_row(business_status="CLOSED_PERMANENTLY")) is None


def test_a_retail_primary_type_is_dropped_however_the_keyword_found_it():
    for t in ("grocery_store", "restaurant", "market", "gas_station"):
        assert food.from_places(_row(primary_type=t)) is None


def test_a_processor_type_needs_no_review():
    row = food.from_places(_row(primary_type="manufacturer", name="Berber Food Manufacturing"))
    assert row.needs_review is False
    assert row.sources == [food.SOURCE_PLACES]
    assert row.key == "PLACES:abc"


def test_a_vague_type_is_kept_but_flagged_because_a_farm_stand_looks_the_same():
    """Blue Diamond Growers itself comes back as plain "food", so these cannot be
    dropped -- but neither can they be presented as confirmed plants."""
    row = food.from_places(_row(primary_type="food", name="Some Foods"))
    assert row.needs_review is True
    assert "confirm it is a production site" in row.review_reason


def test_an_untyped_result_is_also_flagged():
    assert food.from_places(_row(primary_type="", name="Wavesure")).needs_review is True


def test_googles_type_supplies_the_category_when_the_name_says_nothing():
    assert food.from_places(_row(primary_type="brewery", name="Sudwerk")).category == food.CAT_ALCOHOL
    assert food.from_places(_row(primary_type="butcher_shop", name="Corfini")).category == food.CAT_MEAT
    assert food.from_places(_row(primary_type="wholesaler", name="Calvada Co")).category == food.CAT_DISTRIBUTION


def test_the_name_still_wins_over_googles_type():
    row = food.from_places(_row(primary_type="manufacturer", name="Sacramento Cold Storage"))
    assert row.category == food.CAT_COLD


def test_the_address_is_split_out_of_the_one_formatted_string():
    row = food.from_places(_row())
    assert (row.address, row.city, row.zip5) == ("4010 Seaport Blvd", "West Sacramento", "95691")
    assert row.distance_basis == "coords"


def test_a_sweep_row_is_never_re_bought_from_places():
    """It came from Places; there is nothing further to ask about it."""
    assert food.from_places(_row()).places_checked is True


def test_a_sweep_row_merges_with_the_registry_row_for_the_same_address():
    registry = food.from_calepa(_site(name="MARY ANN'S BAKING CO", address="4010 SEAPORT BLVD", zip5="95691"))
    swept = food.from_places(_row())
    merged = food.merge([registry], [swept])
    assert len(merged) == 1
    assert merged[0].sources == [food.SOURCE_CALEPA, food.SOURCE_PLACES]


def test_a_farm_is_not_a_processing_site():
    """"Ruhstaller Farm", "Sunrise Orchards" and "Soil Born Farms" all came back
    under the produce keywords. The plan's scope is processing, manufacturing and
    storage -- a grower without a packing operation is out. One that does pack is
    typed `manufacturer` or named for it and survives on that instead."""
    assert food.from_places(_row(primary_type="farm", name="Sunrise Orchards")) is None
    assert food.from_places(_row(primary_type="manufacturer", name="Sunrise Packing")) is not None


def test_the_regulator_itself_is_not_a_lead():
    """CDPH-Food & Drug Branch came back under a food keyword, typed as a
    government office."""
    assert food.from_places(_row(primary_type="local_government_office", name="CDPH-Food & Drug Branch")) is None


def test_the_retail_types_that_leaked_through_the_first_live_sweep_are_closed_off():
    for t in ("department_store", "donut_shop", "fast_food_restaurant", "tea_store"):
        assert food.from_places(_row(primary_type=t)) is None


def test_a_retail_bakery_counter_is_not_a_commercial_bakery():
    """Google types a wholesale plant and a cupcake counter identically. The first
    live sweep returned 232 "confident" bakeries whose nearest members were Crumbl
    Cookies, Nothing Bundt Cakes and Paris Baguette."""
    counter = food.from_places(_row(primary_type="bakery", name="Nothing Bundt Cakes"))
    assert counter is not None and counter.needs_review is True  # kept for triage, not confirmed
    plant = food.from_places(_row(primary_type="bakery", name="Old Country Baking Co"))
    assert plant.needs_review is False
    assert plant.category == food.CAT_BAKERY


def test_a_bakery_counter_inside_a_supermarket_is_not_an_account_of_its_own():
    for name in ("Safeway Bakery", "Costco Bakery", "Raley's Bakery"):
        assert food.excluded_reason(name) is not None
        assert food.from_places(_row(primary_type="bakery", name=name)) is None


def test_the_retail_filters_are_accent_insensitive():
    """"Carnicería Atoyac Meat Market & Taquería" reached the confident list
    because \\bTAQUERIA\\b does not match "Taquería"."""
    assert food.excluded_reason("Carnicería Atoyac Meat Market & Taquería") is not None
    assert food.fold_accents("Taquería Jalisco") == "Taqueria Jalisco"
