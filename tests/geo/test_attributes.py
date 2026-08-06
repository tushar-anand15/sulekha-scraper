"""Unit 6: joining election attributes onto stitched geometry.

Everything here runs against small synthetic fixtures -- a hand-built
``StitchResult`` standing in for a real tile stitch, and a hand-built
``CrosswalkResult`` standing in for a real Unit 5 run -- so the suite never depends
on the tile cache or a full crosswalk run being present. See the module docstring in
``geo.build.attributes`` for why the join goes through the crosswalk in both
directions rather than joining CSV rows straight onto KSMART codes.
"""

from __future__ import annotations

from collections import Counter

import pytest
from shapely.geometry import box

from geo.build.attributes import (
    Unmatched,
    clean_optional_text,
    compute_margin,
    join_local_body_layer,
    join_ward_layer,
    local_bodies_for_crosswalk,
    local_body_properties,
    normalize_ward_no,
    ward_properties,
)
from geo.build.crosswalk import CrosswalkResult, LocalBody, Match
from geo.build.stitch import StitchedFeature, StitchResult

DISTRICT = "THIRUVANANTHAPURAM"


def _ward_row(**overrides):
    row = {
        "district_code": "D01001",
        "district_name": DISTRICT,
        "lb_type": "Grama Panchayat",
        "lb_code": "G01001",
        "lb_name": "Parassala",
        "lb_name_mal": "",
        "ward_code": "G01001001",
        "ward_no": "1",
        "ward_name": "Puthankada",
        "ward_name_mal": "",
        "n_candidates": "3",
        "valid_votes": "6928",
        "invalid_votes": "0",
        "winner_name": "A Winner",
        "winner_party": "INC",
        "winner_party_group": "UDF",
        "winner_votes": "4505",
        "runnerup_name": "A Runnerup",
        "runnerup_votes": "1765",
        "reservation": "General",
        "winner_role": "Member",
        "lsgd_match": "ok",
        "lb_ruling_front": "UDF",
        "lb_control_type": "majority",
    }
    row.update(overrides)
    return row


def _lb_row(**overrides):
    row = {
        "district_code": "D01001",
        "district_name": DISTRICT,
        "lb_type": "Grama Panchayat",
        "lb_code": "G01001",
        "lb_name": "Parassala",
        "lb_name_mal": "",
        "total_wards": "15",
        "lb_seats_udf": "12",
        "lb_seats_ldf": "3",
        "lb_seats_nda": "0",
        "lb_seats_oth": "0",
        "lb_majority_threshold": "8",
        "lb_largest_front": "UDF",
        "lb_largest_front_seats": "12",
        "lb_ruling_front": "UDF",
        "lb_control_type": "majority",
        "lb_head_role": "",
        "lb_head_party_group": "",
        "lb_head_cross_front": "",
    }
    row.update(overrides)
    return row


def _crosswalk(matches):
    return CrosswalkResult(
        matches=tuple(matches), unresolved=(), rejected=(), unclaimed=(), how=Counter()
    )


def _match(our_code="G01001", ksmart_code="G010001", district=DISTRICT):
    ours = LocalBody(code=our_code, name="Parassala", lb_type="Grama Panchayat", district=district)
    theirs = LocalBody(
        code=ksmart_code, name="Parassala", lb_type="Grama Panchayat", district=district
    )
    return Match(ours=ours, theirs=theirs, method="exact", ward_agreement=1.0)


def _wb_feature(objectid, *, ksmart_lb_code="G010001", ward_no=1, district=DISTRICT, area=(0, 0, 1, 1)):
    return StitchedFeature(
        key=(objectid,),
        properties={
            "OBJECTID": objectid,
            "lb_code": ksmart_lb_code,
            "Ward_No": ward_no,
            "Ward Eng": "Puthankada",
            "District": district,
        },
        geometry=box(*area),
        fragment_count=1,
    )


def _lb_feature(ksmart_lb_code="G010001", district=DISTRICT, area=(0, 0, 1, 1)):
    return StitchedFeature(
        key=(ksmart_lb_code,),
        properties={"lb_code": ksmart_lb_code, "DistName": district},
        geometry=box(*area),
        fragment_count=1,
    )


# --- compute_margin ----------------------------------------------------------


def test_margin_is_winner_minus_runnerup():
    assert compute_margin("4505", "1765") == 2740


def test_uncontested_ward_has_no_runnerup_and_null_margin_not_zero():
    """The edge case the plan calls out by name: a blank runner-up must not become
    a fabricated zero margin."""
    assert compute_margin("6928", "") is None


def test_margin_never_raises_on_blank_winner_either():
    assert compute_margin("", "") is None


# --- clean_optional_text -----------------------------------------------------


def test_blank_malayalam_name_becomes_none_not_empty_string():
    assert clean_optional_text("") is None
    assert clean_optional_text("   ") is None


def test_non_blank_text_passes_through_stripped():
    assert clean_optional_text("  ദ്വാരക  ") == "ദ്വാരക"


# --- normalize_ward_no --------------------------------------------------------


def test_ward_no_normalizes_across_formats():
    assert normalize_ward_no("01") == normalize_ward_no(1) == normalize_ward_no(" 1 ") == "1"


# --- ward_properties / local_body_properties ----------------------------------


def test_ward_properties_carries_winner_and_party_group():
    props = ward_properties(_ward_row())
    assert props["winner_name"] == "A Winner"
    assert props["winner_party_group"] == "UDF"
    assert props["margin"] == 2740


def test_ward_properties_blank_malayalam_name_is_null():
    props = ward_properties(_ward_row(ward_name_mal=""))
    assert props["ward_name_mal"] is None


def test_local_body_properties_carries_seat_counts_and_control_type():
    props = local_body_properties(_lb_row())
    assert props["lb_seats_udf"] == 12
    assert props["lb_ruling_front"] == "UDF"
    assert props["lb_control_type"] == "majority"
    assert props["lb_head_role"] is None  # blank in the CSV


# --- local_bodies_for_crosswalk ------------------------------------------------


def test_local_bodies_for_crosswalk_populates_ward_names_from_the_ward_csv():
    lb_rows = [_lb_row()]
    ward_rows = [_ward_row(ward_no="1", ward_name="Puthankada"), _ward_row(ward_no="2", ward_name="Kollayil")]
    bodies = local_bodies_for_crosswalk(lb_rows, ward_rows)
    assert len(bodies) == 1
    assert set(bodies[0].ward_names) == {"Puthankada", "Kollayil"}


# --- join_ward_layer -----------------------------------------------------------


def test_ward_feature_carries_winner_and_party_group_from_matching_csv_row():
    stitch = StitchResult(layer="wb_kerala", features={(1,): _wb_feature(1, ward_no=1)})
    crosswalk = _crosswalk([_match()])
    result = join_ward_layer(stitch, crosswalk, [_ward_row(ward_no="1")])

    assert len(result.features) == 1
    props = result.features[0].properties
    assert props["winner_name"] == "A Winner"
    assert props["winner_party_group"] == "UDF"
    assert props["margin"] == 2740
    assert not result.gate()


def test_uncontested_ward_join_emits_null_margin_without_crashing():
    stitch = StitchResult(layer="wb_kerala", features={(1,): _wb_feature(1, ward_no=1)})
    crosswalk = _crosswalk([_match()])
    row = _ward_row(ward_no="1", runnerup_name="", runnerup_votes="")

    result = join_ward_layer(stitch, crosswalk, [row])

    assert result.features[0].properties["margin"] is None
    assert result.features[0].properties["runnerup_votes"] is None
    assert not result.gate()


def test_geometry_with_no_csv_row_is_reported_per_district_and_fails_gate():
    # Ward 99 has geometry but wards_2025.csv only has ward 1.
    stitch = StitchResult(
        layer="wb_kerala",
        features={
            (1,): _wb_feature(1, ward_no=1),
            (2,): _wb_feature(2, ward_no=99, district="KOLLAM"),
        },
    )
    crosswalk = _crosswalk([_match()])
    result = join_ward_layer(stitch, crosswalk, [_ward_row(ward_no="1")])

    assert len(result.features) == 1
    geoms = result.unmatched_geometries
    assert len(geoms) == 1
    assert geoms[0].district == "KOLLAM"

    problems = result.gate()
    assert problems
    assert any("no matching CSV" in p for p in problems)


def test_csv_row_with_no_geometry_is_reported_per_district_and_fails_gate():
    # wards_2025.csv has ward 2, but only ward 1's geometry stitched.
    stitch = StitchResult(layer="wb_kerala", features={(1,): _wb_feature(1, ward_no=1)})
    crosswalk = _crosswalk([_match()])
    rows = [_ward_row(ward_no="1"), _ward_row(ward_no="2", ward_code="G01001002")]

    result = join_ward_layer(stitch, crosswalk, rows)

    rows_unmatched = result.unmatched_rows
    assert len(rows_unmatched) == 1
    assert rows_unmatched[0].district == DISTRICT

    problems = result.gate()
    assert problems
    assert any("no matching geometry" in p for p in problems)


def test_geometry_whose_local_body_never_resolved_is_reported_not_dropped():
    stitch = StitchResult(
        layer="wb_kerala",
        features={(1,): _wb_feature(1, ksmart_lb_code="UNRESOLVED", ward_no=1)},
    )
    crosswalk = _crosswalk([])  # no matches at all
    result = join_ward_layer(stitch, crosswalk, [_ward_row(ward_no="1")])

    assert result.features == []
    assert len(result.unmatched_geometries) == 1
    assert len(result.unmatched_rows) == 1  # the CSV row was never claimed either
    assert result.gate()


# --- join_local_body_layer ------------------------------------------------------


def test_local_body_feature_carries_seat_counts():
    stitch = StitchResult(layer="lb_kerala", features={("G010001",): _lb_feature()})
    crosswalk = _crosswalk([_match()])
    result = join_local_body_layer(stitch, crosswalk, [_lb_row()])

    assert len(result.features) == 1
    assert result.features[0].properties["lb_seats_udf"] == 12
    assert not result.gate()


def test_local_body_row_with_no_geometry_fails_gate():
    stitch = StitchResult(layer="lb_kerala", features={})
    crosswalk = _crosswalk([_match()])
    result = join_local_body_layer(stitch, crosswalk, [_lb_row()])

    assert result.features == []
    assert len(result.unmatched_rows) == 1
    assert result.gate()
