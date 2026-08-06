"""Unit 7: dissolving opendatakerala Grama Panchayats into Block/District Panchayats.

Everything here runs offline against small synthetic fixtures built in this file --
never against the 6 MB real release. An optional integration check at the bottom
uses the real cache if it happens to be present locally, and is skipped otherwise so
the suite passes on a clean clone.

The HTTP checksum tests mock ``requests`` with ``responses`` (already a project
dependency, used the same way in ``tests/test_phase1_discovery.py``) -- no real
network request is made.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import responses
from shapely.geometry import box, mapping

from geo.build.dissolve import (
    dissolve,
    dissolve_block_panchayats,
    dissolve_district_panchayats,
    load_features,
)
from geo.config import resolve_paths
from geo.fetch.opendatakerala import (
    RELEASE_URL,
    ChecksumMismatchError,
    fetch_release,
    sha256_bytes,
)

# --------------------------------------------------------------------------
# Fixture construction
# --------------------------------------------------------------------------
# Unit squares on an integer grid so every area is exact and adjacency is exact:
#
#   GP1 (0,0)-(1,1)   GP3 (0,1)-(1,2)
#   GP2 (1,0)-(2,1)   GP4 (2,0)-(3,1)  <- null Block_QID
#
# GP1+GP2 share the edge x=1 and both belong to block qB1.
# GP3 alone forms block qB2.
# GP4 has no Block_QID (must be reported, not dropped) but does have a DP_QID.
# All four GPs share district panchayat qD1.
# MUNI and CORP sit far away and must never be pulled into any dissolve.


def _gp_feature(
    lsgi_code: str,
    name: str,
    minx: float,
    miny: float,
    maxx: float,
    maxy: float,
    *,
    block_qid: str | None,
    dp_qid: str | None,
    district: str = "Test District",
    block_name: str = "Test Block",
    dp_name: str = "Test DP",
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {
            "LSGI_Code": lsgi_code,
            "name": name,
            "admin_leve": "8",
            "local_auth": "gram_panchayat",
            "District": district,
            "Block_QID": block_qid,
            "BlockName": block_name,
            "DP_QID": dp_qid,
            "DP_Name": dp_name,
        },
        "geometry": mapping(box(minx, miny, maxx, maxy)),
    }


def _other_feature(
    lsgi_code: str, name: str, minx: float, miny: float, maxx: float, maxy: float, tier: str
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {
            "LSGI_Code": lsgi_code,
            "name": name,
            "admin_leve": "8",
            "local_auth": tier,
            "District": "Test District",
            # Municipalities/Corporations carry these fields too in the real
            # data, sometimes populated -- excluded regardless of their value.
            "Block_QID": "qB1",
            "BlockName": "Test Block",
            "DP_QID": "qD1",
            "DP_Name": "Test DP",
        },
        "geometry": mapping(box(minx, miny, maxx, maxy)),
    }


GP1 = _gp_feature("G1", "GP One", 0, 0, 1, 1, block_qid="qB1", dp_qid="qD1")
GP2 = _gp_feature("G2", "GP Two", 1, 0, 2, 1, block_qid="qB1", dp_qid="qD1")
GP3 = _gp_feature("G3", "GP Three", 0, 1, 1, 2, block_qid="qB2", dp_qid="qD1")
GP4 = _gp_feature("G4", "GP Four", 2, 0, 3, 1, block_qid=None, dp_qid="qD1")
MUNI = _other_feature("M1", "Muni One", 5, 5, 6, 6, "municipality")
CORP = _other_feature("C1", "Corp One", 7, 7, 8, 8, "municipal_corporation")

ALL_FEATURES = [GP1, GP2, GP3, GP4, MUNI, CORP]


def _write_fixture(tmp_path: Path, features: list[dict[str, Any]]) -> Path:
    path = tmp_path / "fixture.geojson"
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8"
    )
    return path


# --------------------------------------------------------------------------
# load_features
# --------------------------------------------------------------------------


def test_load_features_reads_a_feature_collection(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path, ALL_FEATURES)
    features = load_features(path)
    assert len(features) == 6


def test_load_features_rejects_a_non_feature_collection(tmp_path: Path) -> None:
    path = tmp_path / "not_geojson.json"
    path.write_text(json.dumps({"type": "Something"}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_features(path)


# --------------------------------------------------------------------------
# Happy path: feature counts
# --------------------------------------------------------------------------


def test_block_dissolve_yields_one_feature_per_block() -> None:
    result = dissolve_block_panchayats(ALL_FEATURES)
    # qB1 (GP1+GP2) and qB2 (GP3). GP4 is excluded (null Block_QID, reported
    # separately) and MUNI/CORP are excluded entirely.
    assert {b.qid for b in result.bodies} == {"qB1", "qB2"}
    assert len(result.bodies) == 2


def test_district_dissolve_yields_one_feature_per_district() -> None:
    result = dissolve_district_panchayats(ALL_FEATURES)
    # All four GPs share DP_QID qD1 -- GP4 has a DP_QID even though it lacks a
    # Block_QID, so it must appear here.
    assert {b.qid for b in result.bodies} == {"qD1"}
    assert len(result.bodies) == 1
    (district,) = result.bodies
    assert set(district.member_codes) == {"G1", "G2", "G3", "G4"}


# --------------------------------------------------------------------------
# Happy path: area reconciliation
# --------------------------------------------------------------------------


def test_dissolved_block_area_equals_sum_of_member_gp_areas() -> None:
    result = dissolve_block_panchayats(ALL_FEATURES)
    qb1 = next(b for b in result.bodies if b.qid == "qB1")
    # GP1 and GP2 are each a 1x1 square: exact area 1.0 apiece.
    assert qb1.geometry.area == pytest.approx(2.0)
    assert set(qb1.member_codes) == {"G1", "G2"}


def test_dissolved_district_area_equals_sum_of_member_gp_areas() -> None:
    result = dissolve_district_panchayats(ALL_FEATURES)
    (district,) = result.bodies
    assert district.geometry.area == pytest.approx(4.0)


# --------------------------------------------------------------------------
# Edge case: municipalities/corporations excluded
# --------------------------------------------------------------------------


def test_municipalities_and_corporations_do_not_inflate_any_block() -> None:
    result = dissolve_block_panchayats(ALL_FEATURES)
    total_area = sum(b.geometry.area for b in result.bodies)
    # Only the three eligible GPs with a Block_QID (GP1, GP2, GP3) contribute;
    # MUNI (area 1) and CORP (area 1) must not appear anywhere.
    assert total_area == pytest.approx(3.0)
    all_members = {code for b in result.bodies for code in b.member_codes}
    assert "M1" not in all_members
    assert "C1" not in all_members


def test_municipalities_and_corporations_excluded_from_district_dissolve() -> None:
    result = dissolve_district_panchayats(ALL_FEATURES)
    all_members = {code for b in result.bodies for code in b.member_codes}
    assert "M1" not in all_members
    assert "C1" not in all_members


# --------------------------------------------------------------------------
# Edge case: null group key reported, not dropped
# --------------------------------------------------------------------------


def test_gp_with_null_block_qid_is_reported() -> None:
    result = dissolve_block_panchayats(ALL_FEATURES)
    assert len(result.missing) == 1
    missing = result.missing[0]
    assert missing.lsgi_code == "G4"
    assert missing.group_field == "Block_QID"
    # And it must not have quietly ended up inside some other block.
    all_members = {code for b in result.bodies for code in b.member_codes}
    assert "G4" not in all_members


def test_gp_with_populated_dp_qid_is_not_reported_missing_there() -> None:
    result = dissolve_district_panchayats(ALL_FEATURES)
    assert result.missing == ()


# --------------------------------------------------------------------------
# Edge case: adjacent GPs dissolve cleanly, no internal boundary left behind
# --------------------------------------------------------------------------


def test_adjacent_gps_dissolve_into_a_single_polygon_without_a_seam() -> None:
    result = dissolve_block_panchayats(ALL_FEATURES)
    qb1 = next(b for b in result.bodies if b.qid == "qB1")
    geometry = qb1.geometry
    # GP1 (0,0)-(1,1) and GP2 (1,0)-(2,1) share the edge x=1 and must merge into
    # one clean 2x1 rectangle -- not a MultiPolygon, and no interior ring left
    # behind by an imperfect union.
    assert geometry.geom_type == "Polygon"
    assert len(geometry.interiors) == 0
    assert geometry.area == pytest.approx(2.0)
    assert geometry.bounds == pytest.approx((0.0, 0.0, 2.0, 1.0))
    # The single-polygon, no-interior-ring, exact-area assertions above already
    # rule out a seam: an unclosed union along the shared edge x=1 would either
    # produce two polygons or leave a sliver gap that shows up as missing area.
    # unary_union may retain collinear vertices at the old shared corners
    # (1,0)/(1,1) without that indicating any leftover internal boundary.
    exterior_coords = list(geometry.exterior.coords)
    assert len(exterior_coords) >= 5


def test_carries_block_district_and_name_fields_through() -> None:
    result = dissolve_block_panchayats(ALL_FEATURES)
    qb1 = next(b for b in result.bodies if b.qid == "qB1")
    assert qb1.name == "Test Block"
    assert qb1.district == "Test District"

    dresult = dissolve_district_panchayats(ALL_FEATURES)
    (district,) = dresult.bodies
    assert district.name == "Test DP"
    assert district.district == "Test District"


def test_generic_dissolve_is_what_the_two_helpers_call() -> None:
    result = dissolve(ALL_FEATURES, group_field="Block_QID", name_field="BlockName")
    assert {b.qid for b in result.bodies} == {"qB1", "qB2"}


# --------------------------------------------------------------------------
# Error path: checksum mismatch on fetch fails (mocked HTTP, no real request)
# --------------------------------------------------------------------------


@responses.activate
def test_fetch_release_downloads_and_records_a_checksum(tmp_path: Path) -> None:
    content = b'{"type": "FeatureCollection", "features": []}'
    responses.add(responses.GET, RELEASE_URL, body=content, status=200)

    paths = resolve_paths(tmp_path)
    dest = fetch_release(paths)

    assert dest.read_bytes() == content
    checksum_path = dest.with_name(dest.name + ".sha256")
    assert checksum_path.read_text(encoding="utf-8").strip() == sha256_bytes(content)


@responses.activate
def test_fetch_release_rejects_a_download_that_does_not_match_expected_checksum(
    tmp_path: Path,
) -> None:
    content = b'{"type": "FeatureCollection", "features": []}'
    responses.add(responses.GET, RELEASE_URL, body=content, status=200)

    paths = resolve_paths(tmp_path)
    wrong_digest = hashlib.sha256(b"not the real content").hexdigest()

    with pytest.raises(ChecksumMismatchError):
        fetch_release(paths, expected_sha256=wrong_digest)

    # And it must not have cached the bad payload behind the failure.
    assert not (paths.releases / "kerala_lsg_data.geojson").exists()


def test_fetch_release_rejects_a_cache_that_has_drifted_from_its_recorded_checksum(
    tmp_path: Path,
) -> None:
    paths = resolve_paths(tmp_path)
    paths.releases.mkdir(parents=True, exist_ok=True)
    dest = paths.releases / "kerala_lsg_data.geojson"
    dest.write_bytes(b"original content")
    checksum_path = dest.with_name(dest.name + ".sha256")
    checksum_path.write_text(hashlib.sha256(b"original content").hexdigest(), encoding="utf-8")

    # Tamper with the cached file after the checksum was recorded.
    dest.write_bytes(b"corrupted content")

    with pytest.raises(ChecksumMismatchError):
        fetch_release(paths)


def test_fetch_release_is_a_cache_hit_when_checksum_matches(tmp_path: Path) -> None:
    paths = resolve_paths(tmp_path)
    paths.releases.mkdir(parents=True, exist_ok=True)
    dest = paths.releases / "kerala_lsg_data.geojson"
    dest.write_bytes(b"cached content")
    checksum_path = dest.with_name(dest.name + ".sha256")
    checksum_path.write_text(hashlib.sha256(b"cached content").hexdigest(), encoding="utf-8")

    # No responses registered: a real request would raise ConnectionError, so
    # this only passes if fetch_release never touches the network on a hit.
    result = fetch_release(paths)
    assert result == dest


# --------------------------------------------------------------------------
# Integration: only if the real cached release happens to be present locally
# --------------------------------------------------------------------------

_REAL_PATHS = resolve_paths()
_REAL_RELEASE = _REAL_PATHS.releases / "kerala_lsg_data.geojson"


@pytest.mark.skipif(
    not _REAL_RELEASE.exists(), reason="real opendatakerala release not cached locally"
)
def test_real_release_dissolves_to_the_expected_statewide_counts() -> None:
    features = load_features(_REAL_RELEASE)
    blocks = dissolve_block_panchayats(features)
    districts = dissolve_district_panchayats(features)
    assert len(blocks.bodies) == 152
    assert len(districts.bodies) == 14
