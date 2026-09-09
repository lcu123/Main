"""Apartment properties and the manager portfolios inferred from what they share.

The judgement calls here all come from one constraint discovered live: Sacramento
County's public parcel layer carries no owner, no mailing address and no unit
count, so neither ownership clustering nor a "16+ units" rule is possible. Posted
leasing-office hours and review counts stand in, and both are labelled as proxies.
"""

from __future__ import annotations

from datetime import date

from fr_mcp.leads import apartments as apt, places, sheet

TODAY = date(2026, 9, 9)


def _row(**kw) -> places.PlaceRow:
    base = dict(
        place_id="p1", name="Slate Creek Apartments",
        address="1 Slate Creek Dr, Roseville, CA 95678, USA",
        primary_type="apartment_complex", phone="9167511379",
        website="https://www.usamfm.com/slate-creek", business_status="OPERATIONAL",
        lat=38.75, lng=-121.28,
        opening_hours=("Monday: 9:00 AM – 6:00 PM", "Tuesday: 9:00 AM – 6:00 PM", "Sunday: Closed"),
        review_count=786,
    )
    base.update(kw)
    return places.PlaceRow(**base)


# --- what counts as a property -------------------------------------------


def test_only_googles_own_apartment_types_become_properties():
    """The keywords pull in management offices and listing agencies too. Those
    reach the managers tab through their properties, not as buildings."""
    assert apt.from_place(_row(primary_type="apartment_complex")) is not None
    assert apt.from_place(_row(primary_type="apartment_building")) is not None
    for t in ("real_estate_agency", "corporate_office", "storage", "hotel"):
        assert apt.from_place(_row(primary_type=t)) is None


def test_a_permanently_closed_property_is_dropped():
    assert apt.from_place(_row(business_status="CLOSED_PERMANENTLY")) is None


# --- the on-site signal --------------------------------------------------


def test_posted_hours_mean_a_staffed_leasing_office():
    """This replaces the "16 or more units means a resident manager" rule the plan
    opened with -- no public source carries unit counts, and hours observe the
    same fact directly."""
    assert apt.from_place(_row()).onsite_tier == apt.TIER_OFFICE


def test_no_hours_but_a_local_line_is_a_resident_manager():
    c = apt.from_place(_row(opening_hours=()))
    assert c.onsite_tier == apt.TIER_ONSITE


def test_no_hours_and_only_a_toll_free_number_is_not_an_on_site_contact():
    """An 800/833/844 number is a call centre; there is no evidence of anyone on
    the property."""
    c = apt.from_place(_row(opening_hours=(), phone="8332178358"))
    assert c.onsite_tier == apt.TIER_UNKNOWN
    assert apt.is_toll_free("8332178358") is True
    assert apt.is_toll_free("9167511379") is False


def test_the_week_is_summarised_rather_than_listed_day_by_day():
    c = apt.from_place(_row())
    assert "Mon-Tue" in c.hours
    assert "Sun Closed" in c.hours
    assert c.open_days == 2  # Sunday is closed and does not count


# --- size proxy ----------------------------------------------------------


def test_review_count_buckets_size_and_is_never_called_a_unit_count():
    assert apt.from_place(_row(review_count=786)).size_hint == "large"
    assert apt.from_place(_row(review_count=140)).size_hint == "medium"
    assert apt.from_place(_row(review_count=40)).size_hint == "small"
    assert apt.from_place(_row(review_count=3)).size_hint == "unknown"


# --- manager clustering --------------------------------------------------


def _complex(name: str, website: str = "", phone: str = "", reviews: int = 10, city: str = "Sacramento"):
    return apt.from_place(
        _row(place_id=name, name=name, website=website, phone=phone, review_count=reviews,
             address=f"1 Main St, {city}, CA 95814, USA")
    )


def test_two_properties_on_one_website_are_one_company():
    """usamfm.com carries 31 real properties; that is USA Multifamily Management."""
    members = [
        _complex("Terracina", "https://www.usamfm.com/terracina"),
        _complex("Slate Creek", "https://usamfm.com/slate-creek"),
    ]
    managers = apt.group_managers(members)
    assert len(managers) == 1
    assert managers[0].property_count == 2
    assert managers[0].basis == "shared website"


def test_a_domain_cluster_is_named_for_the_domain_not_for_one_of_its_properties():
    """Naming the group after its biggest property actively misleads -- the 31
    properties on usamfm.com are not "Terracina at Park Meadows"."""
    members = [
        _complex("Terracina at Park Meadows", "https://www.usamfm.com/a", reviews=99),
        _complex("Slate Creek Apartments", "https://www.usamfm.com/b", reviews=5),
    ]
    assert apt.group_managers(members)[0].name == "usamfm.com"


def test_a_single_property_company_is_not_a_manager():
    """The ask was for companies running several, not for every landlord."""
    assert apt.group_managers([_complex("Solo", "https://solo-apts.com")]) == []


def test_a_per_property_branded_domain_clusters_with_nothing():
    """Which is correct -- it is evidence of nothing either way."""
    members = [
        _complex("Larkspur Woods", "https://elevatetolarkspurwoods.com"),
        _complex("Capitol Yards", "https://thecapitolyards.com"),
    ]
    assert apt.group_managers(members) == []


def test_a_listing_portal_domain_never_forms_a_cluster():
    """Twenty unrelated properties all link to apartments.com."""
    members = [_complex(f"P{i}", "https://www.apartments.com/x") for i in range(4)]
    assert apt.group_managers(members) == []
    assert apt.is_generic_domain("apartments.com") is True


def test_a_shared_local_phone_forms_a_cluster_when_there_is_no_website():
    members = [_complex("A", "", "9164817098"), _complex("B", "", "9164817098")]
    managers = apt.group_managers(members)
    assert len(managers) == 1
    assert managers[0].basis == "shared phone line"


def test_a_shared_toll_free_number_is_too_weak_to_cluster_on():
    """It may be one manager, or the same lead-capture vendor sold to both."""
    members = [_complex("A", "", "8005551234"), _complex("B", "", "8005551234")]
    assert apt.group_managers(members) == []


def test_the_website_beats_the_phone_as_evidence():
    """A domain is bought by the company; a phone line may be rented from a vendor."""
    members = [
        _complex("A", "https://kjaxproperty.com/a", "9164284491"),
        _complex("B", "https://kjaxproperty.com/b", "9165550000"),
    ]
    assert apt.group_managers(members)[0].basis == "shared website"


def test_a_manager_row_carries_back_onto_each_property():
    members = [
        _complex("A", "https://tandemproperties.com/a"),
        _complex("B", "https://tandemproperties.com/b"),
    ]
    apt.group_managers(members)
    assert all(m.manager == "tandemproperties.com" for m in members)
    assert all(m.manager_properties == 2 for m in members)


def test_a_national_operator_is_kept_but_flagged():
    """Real accounts, but procurement is corporate -- the same call the Leads tab
    makes about grocery chains."""
    members = [
        _complex("Greystar at Midtown", "https://greystar-x.com/a"),
        _complex("Sister Property", "https://greystar-x.com/b"),
    ]
    assert apt.group_managers(members)[0].is_national is True


def test_min_properties_is_adjustable():
    members = [_complex(f"P{i}", "https://one.com/x") for i in range(3)]
    assert apt.group_managers(members, min_properties=4) == []
    assert apt.group_managers(members, min_properties=3)[0].property_count == 3


# --- the tabs ------------------------------------------------------------


def test_both_tabs_lead_with_the_dialer_columns():
    assert sheet.APARTMENT_COLUMNS[:4] == sheet.DIALER_COLUMNS
    assert sheet.MANAGER_COLUMNS[:4] == sheet.DIALER_COLUMNS


def test_staffed_offices_and_the_biggest_properties_come_first():
    """The ask was for the large ones -- a rep working top-down should reach a
    staffed leasing office before a resident-manager-only property."""
    backend = sheet.FakeSheetBackend()
    small_office = _complex("Small w/ office", "https://a.com", reviews=10)
    big_no_office = apt.from_place(_row(place_id="b", name="Big no office", opening_hours=(), review_count=900))
    sheet.sync_apartments(backend, [big_no_office, small_office], today=TODAY)
    assert [r["facility"] for r in backend.read_rows(sheet.APARTMENTS_TAB)] == [
        "Small w/ office", "Big no office",
    ]


def test_the_property_row_says_reviews_not_units():
    backend = sheet.FakeSheetBackend()
    sheet.sync_apartments(backend, [_complex("A", "https://a.com", reviews=786)], today=TODAY)
    row = backend.read_rows(sheet.APARTMENTS_TAB)[0]
    assert row["reviews"] == 786
    assert row["size"] == "large"
    assert "units" not in row


def test_the_manager_row_says_how_the_group_was_formed():
    """So a rep can weigh it: a shared website is strong, a shared phone could be
    a shared answering service."""
    backend = sheet.FakeSheetBackend()
    members = [_complex("A", "https://kjaxproperty.com/a"), _complex("B", "https://kjaxproperty.com/b")]
    sheet.sync_managers(backend, apt.group_managers(members), today=TODAY)
    row = backend.read_rows(sheet.MANAGERS_TAB)[0]
    assert row["properties"] == 2
    assert row["grouped by"] == "shared website"
    assert "A" in row["property names"] and "B" in row["property names"]


def test_reruns_are_idempotent_and_leave_rep_columns_alone():
    backend = sheet.FakeSheetBackend()
    c = _complex("A", "https://a.com")
    sheet.sync_apartments(backend, [c], today=TODAY)
    backend.update_row(sheet.APARTMENTS_TAB, 0, {"notes": "spoke to manager", "status": "Callback"})
    again = sheet.sync_apartments(backend, [c], today=TODAY)
    assert (again.added, again.skipped_no_change) == (0, 1)
    row = backend.read_rows(sheet.APARTMENTS_TAB)[0]
    assert row["notes"] == "spoke to manager"
    assert row["status"] == "Callback"


def test_the_dnc_tab_is_shared_with_every_other_call_list():
    backend = sheet.FakeSheetBackend()
    backend.ensure_tab(sheet.DNC_TAB, sheet.DNC_COLUMNS)
    backend.append_rows(sheet.DNC_TAB, [{"phone": "(916) 751-1379"}])
    result = sheet.sync_apartments(backend, [_complex("A", "https://a.com", "9167511379")], today=TODAY)
    assert (result.added, result.skipped_dnc) == (0, 1)


def test_apartment_tool_and_rep_columns_stay_disjoint():
    assert not set(sheet.APARTMENT_TOOL_COLUMNS) & set(sheet.APARTMENT_REP_COLUMNS)
    assert not set(sheet.MANAGER_TOOL_COLUMNS) & set(sheet.APARTMENT_REP_COLUMNS)
