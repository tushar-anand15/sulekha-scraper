"""Unit 8: crosswalking opendatakerala's OSM local bodies to our ``lb_code``.

Runs against small synthetic fixtures throughout, mirroring
``tests/geo/test_crosswalk.py``'s shape. The one real-data check at the bottom
is skipped when the release cache is absent, per the plan's guidance not to
depend on files a concurrent statewide fetch may still be populating.
"""

from __future__ import annotations

import csv

import pytest

from geo.build.crosswalk import LocalBody
from geo.build.dissolve import (
    DissolvedBody,
    dissolve_block_panchayats,
    dissolve_district_panchayats,
    load_features,
)
from geo.build.osm_crosswalk import (
    build_osm_crosswalk,
    district_by_lsgi_code,
    exact_code_matches,
    load_overrides,
    local_bodies_from_dissolved,
    local_bodies_from_osm_features,
    majority_district,
    resolve_district,
    strip_tier_suffix,
    write_crosswalk,
)
from geo.config import resolve_paths


def ours(code, name, lb_type="Grama Panchayat", district="KOLLAM"):
    return LocalBody(code=code, name=name, lb_type=lb_type, district=district)


def theirs(code, name, lb_type="Grama Panchayat", district="Kollam"):
    return LocalBody(code=code, name=name, lb_type=lb_type, district=district)


# --- strip_tier_suffix -------------------------------------------------------


def test_strips_block_panchayat_suffix():
    assert strip_tier_suffix("Manjeswaram Block Panchayat") == "Manjeswaram"


def test_strips_district_panchayat_suffix():
    assert strip_tier_suffix("Kollam District Panchayat") == "Kollam"


def test_strips_panchayath_spelling_case_insensitively():
    assert strip_tier_suffix("Thoonerry Block Panchayath") == "Thoonerry"
    assert strip_tier_suffix("thoonerry block panchayath") == "thoonerry"


def test_name_without_suffix_is_unchanged():
    assert strip_tier_suffix("Vorkady") == "Vorkady"


# --- resolve_district ---------------------------------------------------------


def test_kasaragod_spelling_is_aliased():
    assert resolve_district("Kasaragod") == "KASARGOD"


def test_other_districts_pass_through_unaliased():
    assert resolve_district("Kollam") == "Kollam"


def test_empty_district_resolves_to_empty_string():
    assert resolve_district(None) == ""
    assert resolve_district("") == ""


# --- majority_district --------------------------------------------------------


def test_majority_district_self_heals_one_mistagged_member():
    body = DissolvedBody(
        qid="Q1", name="Devikulam Block Panchayat", district="Ernakulam",
        geometry=None, member_codes=("GP1", "GP2", "GP3"),
    )
    district_by_code = {"GP1": "Ernakulam", "GP2": "Idukki", "GP3": "Idukki"}
    assert majority_district(body, district_by_code) == "Idukki"


def test_majority_district_falls_back_to_dissolved_district_when_unknown():
    body = DissolvedBody(
        qid="Q1", name="X Block Panchayat", district="Kollam",
        geometry=None, member_codes=("GPZ",),
    )
    assert majority_district(body, {}) == "Kollam"


# --- exact_code_matches --------------------------------------------------------


def test_identical_code_on_both_sides_is_an_exact_match():
    matches = exact_code_matches([ours("G01001", "Parassala")], [theirs("G01001", "Parassala")])
    assert matches == {"G01001": "G01001"}


def test_ambiguous_osm_code_is_excluded_from_auto_matching():
    """A code repeated on the OSM side (the real ``G08064`` collision) must not
    be auto-matched -- picking either candidate risks attaching the wrong
    body's election results to the wrong polygon."""
    matches = exact_code_matches(
        [ours("G08064", "Mattathur")],
        [
            theirs("G08064", "Mattannur", lb_type="Municipality", district="Kannur"),
            theirs("G08064", "Mattathur", district="Thrissur"),
        ],
    )
    assert matches == {}


def test_code_present_only_on_our_side_is_not_a_match():
    matches = exact_code_matches([ours("G01001", "Parassala")], [theirs("G01002", "Karode")])
    assert matches == {}


# --- local_bodies_from_osm_features -------------------------------------------


def _osm_feature(local_auth, lsgi_code, name, district):
    return {
        "properties": {
            "local_auth": local_auth,
            "LSGI_Code": lsgi_code,
            "name": name,
            "District": district,
        }
    }


def test_local_bodies_from_osm_features_maps_known_tiers():
    features = [
        _osm_feature("gram_panchayat", "G01001", "Parassala", "Thiruvananthapuram"),
        _osm_feature("municipality", "M02004", "Kottarakara", "Kollam"),
        _osm_feature("municipal_corporation", "C01001", "Thiruvananthapuram", "Thiruvananthapuram"),
    ]
    bodies = local_bodies_from_osm_features(features)
    assert {b.lb_type for b in bodies} == {"Grama Panchayat", "Municipality", "Corporation"}


def test_local_bodies_from_osm_features_skips_unknown_tiers():
    features = [_osm_feature("something_else", "X1", "Mystery", "Kollam")]
    assert local_bodies_from_osm_features(features) == []


def test_local_bodies_from_osm_features_applies_district_alias():
    features = [_osm_feature("gram_panchayat", "G14009", "Vorkady", "Kasaragod")]
    bodies = local_bodies_from_osm_features(features)
    assert bodies[0].district == "KASARGOD"


# --- local_bodies_from_dissolved ------------------------------------------------


def test_local_bodies_from_dissolved_strips_suffix_and_majority_districts():
    bodies = [
        DissolvedBody(
            qid="Q1", name="Devikulam Block Panchayat", district="Ernakulam",
            geometry=None, member_codes=("GP1", "GP2"),
        )
    ]
    district_by_code = {"GP1": "Idukki", "GP2": "Idukki"}
    out = local_bodies_from_dissolved(
        bodies, lb_type="Block Panchayat", district_by_code=district_by_code
    )
    assert len(out) == 1
    assert out[0].name == "Devikulam"
    assert out[0].district == "Idukki"
    assert out[0].code == "Q1"


def test_local_bodies_from_dissolved_skips_unnamed_bodies():
    bodies = [DissolvedBody(qid="Q1", name=None, district="Kollam", geometry=None, member_codes=())]
    assert local_bodies_from_dissolved(bodies, lb_type="Block Panchayat", district_by_code={}) == []


# --- build_osm_crosswalk end-to-end --------------------------------------------


def test_exact_code_match_resolves_without_the_name_cascade():
    r = build_osm_crosswalk(
        [ours("G01001", "Parassala")],
        [theirs("G01001", "Totally Different Name")],
    )
    assert r.resolved_count == 1
    assert r.matches[0].method == "override"


def test_residual_after_exact_codes_still_falls_back_to_name_cascade():
    r = build_osm_crosswalk(
        [ours("G02003", "Clappana")],
        [theirs("G099999", "Clappana")],
    )
    assert r.resolved_count == 1
    assert r.matches[0].method == "exact"


def test_hand_override_wins_over_an_auto_exact_code_match():
    """If the code happens to coincide with the wrong body, a human's
    correction in the override file must win."""
    r = build_osm_crosswalk(
        [ours("G02003", "Clappana")],
        [
            theirs("G02003", "Wrong Body"),
            theirs("G099999", "Clappana"),
        ],
        overrides={"G02003": "G099999"},
    )
    assert r.matches[0].theirs.code == "G099999"


def test_gate_fails_on_an_unresolved_body():
    r = build_osm_crosswalk([ours("G02003", "Zzyzx")], [theirs("G099999", "Somewhere Else")])
    assert r.resolved_count == 0
    assert r.gate(expected=1)


def test_synthetic_full_crosswalk_resolves_all_bodies():
    """A small end-to-end run covering all three shapes this module handles:
    a direct GP matched on code, a Block Panchayat matched by name after
    suffix stripping, and a District Panchayat matched one-per-district."""
    tvm = "THIRUVANANTHAPURAM"
    # Suffix-strip and district-alias happen in the conversion helpers, not in
    # build_osm_crosswalk itself, so their_bodies is pre-stripped here to
    # isolate the crosswalk's own pairing logic.
    our_bodies = [
        ours("G01001", "Parassala", district=tvm),
        ours("B01001", "Parassala", lb_type="Block Panchayat", district=tvm),
        ours("D01001", tvm, lb_type="District Panchayat", district=tvm),
    ]
    their_bodies = [
        theirs("G01001", "Parassala", district="Thiruvananthapuram"),
        theirs("Q1", "Parassala", lb_type="Block Panchayat", district="Thiruvananthapuram"),
        theirs(
            "Q2", "Thiruvananthapuram", lb_type="District Panchayat", district="Thiruvananthapuram"
        ),
    ]
    r = build_osm_crosswalk(our_bodies, their_bodies)
    assert r.gate(expected=3) == []
    assert r.resolved_count == 3


# --- write_crosswalk ------------------------------------------------------------


def test_written_crosswalk_uses_osm_column_names(tmp_path):
    r = build_osm_crosswalk([ours("G01001", "Parassala")], [theirs("G01001", "Parassala")])
    out = tmp_path / "osm_cw.csv"
    write_crosswalk(r, out)
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert rows[0]["osm_code"] == "G01001"
    assert rows[0]["osm_name"] == "Parassala"


# --- overrides file ---------------------------------------------------------


def test_overrides_file_round_trips(tmp_path):
    p = tmp_path / "ov.csv"
    p.write_text("lb_code,osm_code\nG02003,Q12345\n", encoding="utf-8")
    assert load_overrides(p) == {"G02003": "Q12345"}


def test_missing_overrides_file_is_not_an_error(tmp_path):
    assert load_overrides(tmp_path / "absent.csv") == {}


def test_committed_overrides_file_is_at_least_header_only():
    """R3's gate needs this file to exist and be readable even before any
    override is hand-added -- a missing file and an empty-but-present file
    must not be conflated by a build script checking for its presence."""
    paths = resolve_paths()
    path = paths.reference / "osm_lb_overrides.csv"
    assert path.exists()
    header = path.read_text(encoding="utf-8").splitlines()[0]
    assert header == "lb_code,osm_code"


# --- real-data integration ----------------------------------------------------

_RELEASE = resolve_paths().releases / "kerala_lsg_data.geojson"
_LOCAL_BODIES_2020 = resolve_paths().elections / "2020" / "local_bodies_2020.csv"


@pytest.mark.skipif(
    not (_RELEASE.exists() and _LOCAL_BODIES_2020.exists()),
    reason="real opendatakerala release or local_bodies_2020.csv not present locally",
)
def test_full_osm_crosswalk_resolves_against_real_local_bodies_2020():
    features = load_features(_RELEASE)
    district_by_code = district_by_lsgi_code(features)
    their_bodies = local_bodies_from_osm_features(features)
    bp = dissolve_block_panchayats(features)
    dp = dissolve_district_panchayats(features)
    their_bodies += local_bodies_from_dissolved(
        bp.bodies, lb_type="Block Panchayat", district_by_code=district_by_code
    )
    their_bodies += local_bodies_from_dissolved(
        dp.bodies, lb_type="District Panchayat", district_by_code=district_by_code
    )

    our_bodies = []
    with _LOCAL_BODIES_2020.open(encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            our_bodies.append(
                LocalBody(
                    code=row["lb_code"], name=row["lb_name"],
                    lb_type=row["lb_type"], district=row["district_name"],
                )
            )

    overrides = load_overrides(resolve_paths().reference / "osm_lb_overrides.csv")
    result = build_osm_crosswalk(our_bodies, their_bodies, overrides=overrides)

    # Measured, reported honestly: 1,031/1,033 direct bodies resolve on an
    # exact LSGI_Code match alone (~99.8%). The full crosswalk, including the
    # dissolved Block/District Panchayats and the four hand overrides, must
    # resolve every one of the 1,199 rows in local_bodies_2020.csv.
    assert result.gate(expected=len(our_bodies)) == []
    assert result.resolved_count == len(our_bodies) == 1199
