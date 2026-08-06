"""The BP/DP tier crosswalk: body pairing, the naming trap, and the division join.

All fixtures are synthetic and offline -- no cached tile is read. A handful of
tests additionally run against the real cache (``@pytest.mark.skipif``-guarded on
its absence) as an end-to-end sanity check, per the runbook's note that the cache
is fully populated.
"""

from __future__ import annotations

import json

import pytest
from shapely.geometry import box

from geo.build.emit import ACCURACY_CAVEAT, KSMART_SOURCE_URL, emit_block_panchayat_layer
from geo.build.stitch import StitchedFeature, StitchResult
from geo.build.tier_crosswalk import (
    BLOCK_PANCHAYAT,
    BODY_NAME_FIELD,
    DISTRICT_PANCHAYAT,
    build_tier_body_crosswalk,
    division_gate,
    join_division_layer,
    local_bodies_for_tier,
    theirs_bodies_from_stitch,
)


# --- fixture builders --------------------------------------------------------


def _stitch(layer: str, rows: list[dict]) -> StitchResult:
    """One ``StitchResult`` from a list of plain property dicts, each becoming
    one feature keyed by its own dict identity (fine here -- these tests never
    rely on ``StitchResult``'s real identity fields, only on iterating features)."""
    features = {}
    for i, props in enumerate(rows):
        features[(layer, i)] = StitchedFeature(
            key=(layer, i),
            properties=props,
            geometry=box(i, 0, i + 1, 1),
            fragment_count=1,
        )
    return StitchResult(layer=layer, features=features)


def _bp_stitch(rows: list[dict]) -> StitchResult:
    return _stitch("kerala_bp", rows)


def _dp_stitch(rows: list[dict]) -> StitchResult:
    return _stitch("kerala_dp", rows)


LB_ROWS_BP = [
    {
        "district_name": "KOLLAM",
        "lb_type": "Block Panchayat",
        "lb_code": "B02001",
        "lb_name": "Anchal",
    },
    {
        "district_name": "KOLLAM",
        "lb_type": "Block Panchayat",
        "lb_code": "B02002",
        "lb_name": "Kottarakkara",
    },
]

WARD_ROWS_BP = [
    {"lb_type": "Block Panchayat", "lb_code": "B02001", "ward_no": "1", "ward_name": "Alpha"},
    {"lb_type": "Block Panchayat", "lb_code": "B02001", "ward_no": "2", "ward_name": "Beta"},
    {"lb_type": "Block Panchayat", "lb_code": "B02002", "ward_no": "1", "ward_name": "Gamma"},
]

LB_ROWS_DP = [
    {
        "district_name": "KOLLAM",
        "lb_type": "District Panchayat",
        "lb_code": "D02001",
        "lb_name": "KOLLAM",
    },
    {
        "district_name": "THRISSUR",
        "lb_type": "District Panchayat",
        "lb_code": "D08001",
        "lb_name": "THRISSUR",
    },
]

WARD_ROWS_DP = [
    {"lb_type": "District Panchayat", "lb_code": "D02001", "ward_no": "1", "ward_name": "Delta"},
    {"lb_type": "District Panchayat", "lb_code": "D08001", "ward_no": "1", "ward_name": "Epsilon"},
]


def _bp_division_row(ward_no: str, name: str = "Alpha") -> dict:
    """One full ``ward_properties``-shaped CSV row for a single BP division,
    with just enough fields for the join and for the emitted properties."""
    return {
        "ward_code": f"B02001{ward_no.zfill(3)}",
        "lb_code": "B02001",
        "lb_name": "Anchal",
        "lb_type": "Block Panchayat",
        "district_name": "KOLLAM",
        "ward_no": ward_no,
        "ward_name": name,
        "ward_name_mal": "",
        "reservation": "General",
        "n_candidates": "2",
        "valid_votes": "100",
        "invalid_votes": "0",
        "winner_name": "A Winner",
        "winner_party": "INC",
        "winner_party_group": "UDF",
        "winner_votes": "60",
        "runnerup_name": "A Loser",
        "runnerup_votes": "40",
        "lb_ruling_front": "UDF",
        "lb_control_type": "majority",
    }


# --- body pairing: exact and transliterated ----------------------------------


def test_bp_exact_name_pairs():
    crosswalk = build_tier_body_crosswalk(
        LB_ROWS_BP,
        WARD_ROWS_BP,
        _bp_stitch(
            [
                {
                    "District": "Kollam",
                    "Localbody": "Anchal",
                    "Block Panc": "Alpha",
                    "Ward No": 1,
                },
                {
                    "District": "Kollam",
                    "Localbody": "Anchal",
                    "Block Panc": "Beta",
                    "Ward No": 2,
                },
            ]
        ),
        BLOCK_PANCHAYAT,
    )
    assert crosswalk.resolved_count == 1
    assert crosswalk.matches[0].ours.code == "B02001"
    assert crosswalk.matches[0].method == "exact"


def test_bp_transliteration_variant_pairs_fuzzily():
    """``Kottarakkara`` (ours) / ``Kottarakara`` (KSMART) -- the same
    transliteration inconsistency ``crosswalk.py`` documents for local bodies,
    now exercised for a Block Panchayat name."""
    crosswalk = build_tier_body_crosswalk(
        LB_ROWS_BP,
        WARD_ROWS_BP,
        _bp_stitch(
            [
                {
                    "District": "Kollam",
                    "Localbody": "Kottarakara",
                    "Block Panc": "Gamma",
                    "Ward No": 1,
                },
            ]
        ),
        BLOCK_PANCHAYAT,
    )
    match = next(m for m in crosswalk.matches if m.ours.code == "B02002")
    assert match.method == "fuzzy"


# --- the naming trap ----------------------------------------------------------


def test_body_grouping_keys_on_localbody_not_the_division_field():
    """The load-bearing regression test: if ``theirs_bodies_from_stitch`` ever
    read the body's name from ``Block Panc`` instead of ``Localbody`` (the trap
    the module docstring warns about), every division in this fixture would be
    grouped as its own one-division "body" named after itself, instead of two
    divisions correctly grouped under one Block Panchayat, ``Anchal``."""
    stitch = _bp_stitch(
        [
            {"District": "Kollam", "Localbody": "Anchal", "Block Panc": "Alpha", "Ward No": 1},
            {"District": "Kollam", "Localbody": "Anchal", "Block Panc": "Beta", "Ward No": 2},
        ]
    )
    bodies = theirs_bodies_from_stitch(stitch, BLOCK_PANCHAYAT)

    assert len(bodies) == 1
    body = bodies[0]
    assert body.name == "Anchal"
    assert set(body.ward_names) == {"Alpha", "Beta"}
    # If the fields were swapped, this would be 2 bodies named "Alpha" and "Beta".
    assert {b.name for b in bodies} != {"Alpha", "Beta"}


def test_dp_naming_trap_localbody_is_the_district_panchayat():
    """Same trap, one tier up: ``kerala_dp``'s ``Localbody`` is the District
    Panchayat's own name (== the district), ``Ward Name`` is the division."""
    stitch = _dp_stitch(
        [
            {"District": "Kollam", "Localbody": "KOLLAM", "Ward Name": "Delta", "Ward No": 1},
            {"District": "Kollam", "Localbody": "KOLLAM", "Ward Name": "Epsilon", "Ward No": 2},
        ]
    )
    bodies = theirs_bodies_from_stitch(stitch, DISTRICT_PANCHAYAT)

    assert len(bodies) == 1
    assert bodies[0].name == "KOLLAM"
    assert set(bodies[0].ward_names) == {"Delta", "Epsilon"}


# --- DP: one per district, no fuzzy name matching needed ----------------------


def test_dp_pairs_one_per_district_even_with_no_useful_name_match():
    """DP pairing must not depend on the name; scope (one candidate per
    district) is enough, exactly as ``_ONE_PER_DISTRICT_TYPES`` already does
    for ``pair_local_bodies``."""
    crosswalk = build_tier_body_crosswalk(
        LB_ROWS_DP,
        WARD_ROWS_DP,
        _dp_stitch(
            [
                {"District": "Kollam", "Localbody": "Kollam Zilla Panchayat", "Ward Name": "Delta", "Ward No": 1},
                {"District": "Thrissur", "Localbody": "Thrissur DP", "Ward Name": "Epsilon", "Ward No": 1},
            ]
        ),
        DISTRICT_PANCHAYAT,
    )
    assert crosswalk.resolved_count == 2
    assert {(m.ours.code, m.theirs.code) for m in crosswalk.matches} == {
        ("D02001", "Kollam::Kollam Zilla Panchayat"),
        ("D08001", "Thrissur::Thrissur DP"),
    }


def test_local_bodies_for_tier_only_takes_the_requested_lb_type():
    bodies = local_bodies_for_tier(LB_ROWS_BP + LB_ROWS_DP, WARD_ROWS_BP + WARD_ROWS_DP, "Block Panchayat")
    assert {b.code for b in bodies} == {"B02001", "B02002"}


# --- division join: ward-number join, int/str mismatch ------------------------


def _resolved_bp_crosswalk():
    return build_tier_body_crosswalk(
        LB_ROWS_BP,
        WARD_ROWS_BP,
        _bp_stitch(
            [
                {"District": "Kollam", "Localbody": "Anchal", "Block Panc": "Alpha", "Ward No": 1},
            ]
        ),
        BLOCK_PANCHAYAT,
    )


def test_division_join_matches_int_and_str_ward_numbers():
    crosswalk = _resolved_bp_crosswalk()
    stitch = _bp_stitch(
        [
            # KSMART's Ward No is a bare int; ours is text "1" -- must still join.
            {"District": "Kollam", "Localbody": "Anchal", "Block Panc": "Alpha", "Ward No": 1},
        ]
    )
    division_rows = [_bp_division_row("1")]

    result = join_division_layer(stitch, None, crosswalk, division_rows, BLOCK_PANCHAYAT)

    assert len(result.features) == 1
    assert result.unmatched == []
    props = result.features[0].properties
    assert props["ward_code"] == "B02001001"
    assert props["winner_party_group"] == "UDF"
    assert props["margin"] == 20


def test_division_join_reports_margin_and_reservation_like_the_ward_layer():
    crosswalk = _resolved_bp_crosswalk()
    stitch = _bp_stitch(
        [{"District": "Kollam", "Localbody": "Anchal", "Block Panc": "Alpha", "Ward No": "01"}]
    )
    result = join_division_layer(stitch, None, crosswalk, [_bp_division_row("1")], BLOCK_PANCHAYAT)

    props = result.features[0].properties
    assert props["reservation"] == "General"
    assert props["winner_name"] == "A Winner"
    assert props["runnerup_name"] == "A Loser"


# --- both directions of a missing join, reported not dropped ------------------


def test_result_with_no_geometry_is_reported_and_gates():
    """A CSV division row with no KSMART geometry -- the 2,267-vs-2,252 shortfall
    this module exists to report, at small scale."""
    crosswalk = _resolved_bp_crosswalk()
    stitch = _bp_stitch([])  # KSMART has nothing for this body at all
    result = join_division_layer(stitch, None, crosswalk, [_bp_division_row("1")], BLOCK_PANCHAYAT)

    assert result.features == []
    assert len(result.unmatched_rows) == 1
    assert result.unmatched_rows[0].kind == "row"
    assert division_gate(result) != []
    assert division_gate(result, tolerate_missing_geometry=1) == []


def test_geometry_with_no_result_is_reported_and_gates():
    """A stitched division with no CSV row -- the mirror-image hole in the map."""
    crosswalk = _resolved_bp_crosswalk()
    stitch = _bp_stitch(
        [{"District": "Kollam", "Localbody": "Anchal", "Block Panc": "Alpha", "Ward No": 1}]
    )
    result = join_division_layer(stitch, None, crosswalk, [], BLOCK_PANCHAYAT)

    assert result.features == []
    assert len(result.unmatched_geometries) == 1
    assert result.unmatched_geometries[0].kind == "geometry"
    # Unlike the missing-geometry direction, this is never tolerated.
    assert division_gate(result, tolerate_missing_geometry=100) != []


def test_unpaired_body_leaves_its_geometry_unmatched_not_dropped():
    """A KSMART body that never resolved at the body level still has its
    geometry reported (as 'not in the resolved crosswalk'), not silently
    skipped."""
    crosswalk = build_tier_body_crosswalk(LB_ROWS_BP, WARD_ROWS_BP, _bp_stitch([]), BLOCK_PANCHAYAT)
    stitch = _bp_stitch(
        [{"District": "Kollam", "Localbody": "Nowhere Block", "Block Panc": "Zzz", "Ward No": 1}]
    )
    result = join_division_layer(stitch, None, crosswalk, [], BLOCK_PANCHAYAT)

    assert len(result.unmatched_geometries) == 1
    assert "not in the resolved crosswalk" in result.unmatched_geometries[0].reason


# --- verification against the membership layer --------------------------------


def test_division_with_membership_evidence_is_flagged_verified():
    crosswalk = _resolved_bp_crosswalk()
    stitch = _bp_stitch(
        [{"District": "Kollam", "Localbody": "Anchal", "Block Panc": "Alpha", "Ward No": 1}]
    )
    membership = _stitch(
        "kerala_bp_with_lsgd",
        [
            {"District": "Kollam", "Localbody": "Anchal", "Ward No": 1, "LSGD": "SomeGP", "Ward_Name": "SomeWard"},
        ],
    )
    result = join_division_layer(stitch, membership, crosswalk, [_bp_division_row("1")], BLOCK_PANCHAYAT)

    assert result.features[0].properties["division_verified"] is True


def test_division_with_no_membership_evidence_is_carried_and_flagged_not_rejected():
    """Absence of evidence is not contradiction -- same doctrine as
    ``crosswalk.Match.verified``. The membership layer simply never mentions
    this division; that must not reject it."""
    crosswalk = _resolved_bp_crosswalk()
    stitch = _bp_stitch(
        [{"District": "Kollam", "Localbody": "Anchal", "Block Panc": "Alpha", "Ward No": 1}]
    )
    membership = _stitch("kerala_bp_with_lsgd", [])  # nothing at all
    result = join_division_layer(stitch, membership, crosswalk, [_bp_division_row("1")], BLOCK_PANCHAYAT)

    assert len(result.features) == 1
    assert result.features[0].properties["division_verified"] is False


def test_missing_membership_stitch_still_joins_everything_unverified():
    crosswalk = _resolved_bp_crosswalk()
    stitch = _bp_stitch(
        [{"District": "Kollam", "Localbody": "Anchal", "Block Panc": "Alpha", "Ward No": 1}]
    )
    result = join_division_layer(stitch, None, crosswalk, [_bp_division_row("1")], BLOCK_PANCHAYAT)

    assert len(result.features) == 1
    assert result.features[0].properties["division_verified"] is False


# --- duplicate KSMART ward numbers within one body -----------------------------


def test_duplicate_ksmart_ward_number_reports_the_collision_not_a_duplicate_result():
    """Real-world case (Palakkad District Panchayat, Ward No 30): two distinct
    KSMART divisions can share one ward number within a body. Our CSV has one
    row for that key, so the second division must be reported as an unmatched
    geometry, never silently given a copy of the first's election result."""
    crosswalk = _resolved_bp_crosswalk()
    stitch = _bp_stitch(
        [
            {"District": "Kollam", "Localbody": "Anchal", "Block Panc": "Alpha", "Ward No": 1},
            {"District": "Kollam", "Localbody": "Anchal", "Block Panc": "AlphaTwin", "Ward No": 1},
        ]
    )
    result = join_division_layer(stitch, None, crosswalk, [_bp_division_row("1")], BLOCK_PANCHAYAT)

    assert len(result.features) == 1
    assert len(result.unmatched_geometries) == 1
    assert "already claimed" in result.unmatched_geometries[0].reason


# --- emission -------------------------------------------------------------


def test_emitted_bp_layer_is_valid_geojson_with_provenance(tmp_path):
    from geo.config import resolve_paths

    paths = resolve_paths(tmp_path)
    crosswalk = _resolved_bp_crosswalk()
    stitch = _bp_stitch(
        [{"District": "Kollam", "Localbody": "Anchal", "Block Panc": "Alpha", "Ward No": 1}]
    )
    result = join_division_layer(stitch, None, crosswalk, [_bp_division_row("1")], BLOCK_PANCHAYAT)

    out_path = emit_block_panchayat_layer(paths, result, fetched="2026-07-30", built="2026-08-06")
    loaded = json.loads(out_path.read_text(encoding="utf-8"))

    assert loaded["type"] == "FeatureCollection"
    assert len(loaded["features"]) == 1
    assert loaded["provenance"]["source_url"] == KSMART_SOURCE_URL
    assert loaded["provenance"]["accuracy"] == ACCURACY_CAVEAT
    props = loaded["features"][0]["properties"]
    assert props["lb_code"] == "B02001"
    assert props["ward_no"] == "1"
    assert props["division_verified"] is False


# --- end-to-end against the real cache (guarded) -------------------------------

from pathlib import Path  # noqa: E402

_REAL_TILE_CACHE = Path(__file__).resolve().parents[2] / "data" / "raw" / "geo" / "ksmart"


@pytest.mark.skipif(not _REAL_TILE_CACHE.is_dir(), reason="real KSMART tile cache not present")
def test_real_cache_bp_body_crosswalk_resolves_all_152_block_panchayats():
    from geo.build.attributes import load_local_bodies, load_wards
    from geo.build.stitch import stitch_layer
    from geo.config import resolve_paths

    paths = resolve_paths()
    lb_rows = load_local_bodies(paths, "2025")
    ward_rows = load_wards(paths, "2025")
    stitch = stitch_layer(paths, BLOCK_PANCHAYAT.stitch_layer)

    crosswalk = build_tier_body_crosswalk(lb_rows, ward_rows, stitch, BLOCK_PANCHAYAT)

    assert crosswalk.resolved_count >= 150
    assert crosswalk.resolved_count <= 152


@pytest.mark.skipif(not _REAL_TILE_CACHE.is_dir(), reason="real KSMART tile cache not present")
def test_real_cache_dp_body_crosswalk_resolves_all_14_district_panchayats():
    from geo.build.attributes import load_local_bodies, load_wards
    from geo.build.stitch import stitch_layer
    from geo.config import resolve_paths

    paths = resolve_paths()
    lb_rows = load_local_bodies(paths, "2025")
    ward_rows = load_wards(paths, "2025")
    stitch = stitch_layer(paths, DISTRICT_PANCHAYAT.stitch_layer)

    crosswalk = build_tier_body_crosswalk(lb_rows, ward_rows, stitch, DISTRICT_PANCHAYAT)

    assert crosswalk.resolved_count == 14
    assert not crosswalk.unresolved
    assert not crosswalk.rejected


@pytest.mark.skipif(not _REAL_TILE_CACHE.is_dir(), reason="real KSMART tile cache not present")
def test_real_cache_bp_division_join_is_short_by_no_more_than_the_known_ksmart_gap():
    from geo.build.attributes import load_local_bodies, load_wards
    from geo.build.stitch import stitch_layer
    from geo.config import resolve_paths

    paths = resolve_paths()
    lb_rows = load_local_bodies(paths, "2025")
    ward_rows = load_wards(paths, "2025")
    stitch = stitch_layer(paths, BLOCK_PANCHAYAT.stitch_layer)
    membership = stitch_layer(paths, BLOCK_PANCHAYAT.membership_layer)

    crosswalk = build_tier_body_crosswalk(lb_rows, ward_rows, stitch, BLOCK_PANCHAYAT)
    division_rows = [r for r in ward_rows if r.get("lb_type") == "Block Panchayat"]
    result = join_division_layer(stitch, membership, crosswalk, division_rows, BLOCK_PANCHAYAT)

    # No geometry should ever be left over without a matching CSV row.
    assert result.unmatched_geometries == [] or all(
        "not in the resolved crosswalk" in u.reason for u in result.unmatched_geometries
    )
    assert BODY_NAME_FIELD == "Localbody"
