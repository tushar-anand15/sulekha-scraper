"""Unit 6: emitting the 2025 GeoJSON layers with provenance.

Runs entirely against a hand-built ``JoinResult`` -- there is no dependency on a real
tile cache or CSV file, so the suite passes on a clean clone even while the
statewide tile fetch (noted in the plan) is still in flight.
"""

from __future__ import annotations

import json
from pathlib import Path

from shapely.geometry import box

from geo.build.attributes import JoinedFeature, JoinResult
from geo.build.emit import (
    ACCURACY_CAVEAT,
    KSMART_SCRAPE_ZOOM,
    KSMART_SOURCE_URL,
    emit_local_body_layer,
    emit_ward_layer,
    ksmart_provenance,
    to_feature_collection,
    write_feature_collection,
)
from geo.config import resolve_paths


def _ward_join_result(n=2):
    features = [
        JoinedFeature(
            geometry=box(i, 0, i + 1, 1),
            properties={
                "ward_code": f"G01001{i:03d}",
                "lb_code": "G01001",
                "ward_no": str(i),
                "ward_name": f"Ward {i}",
                "ward_name_mal": None,
                "winner_name": "A Winner",
                "winner_party_group": "UDF",
                "margin": 100 * i,
            },
        )
        for i in range(1, n + 1)
    ]
    return JoinResult(layer="wb_kerala", features=features, unmatched=[])


def _lb_join_result(n=1):
    features = [
        JoinedFeature(
            geometry=box(0, 0, 5, 5),
            properties={"lb_code": "G01001", "lb_seats_udf": 12},
        )
        for _ in range(n)
    ]
    return JoinResult(layer="lb_kerala", features=features, unmatched=[])


# --- provenance ------------------------------------------------------------


def test_provenance_has_required_fields():
    prov = ksmart_provenance(layer="wb_kerala", fetched="2026-07-30", built="2026-08-06")
    assert prov["source_url"] == KSMART_SOURCE_URL
    assert prov["scrape_zoom"] == KSMART_SCRAPE_ZOOM
    assert prov["fetched"] == "2026-07-30"
    assert prov["built"] == "2026-08-06"
    assert "licence" in prov and prov["licence"]
    assert prov["accuracy"] == ACCURACY_CAVEAT


def test_provenance_is_present_at_the_top_level_not_per_feature():
    result = _ward_join_result()
    prov = ksmart_provenance(layer="wb_kerala", fetched="x", built="y")
    collection = to_feature_collection(result, prov)

    assert collection["provenance"] == prov
    for feature in collection["features"]:
        assert "provenance" not in feature
        assert "provenance" not in feature["properties"]


# --- structural validity ----------------------------------------------------


def test_to_feature_collection_is_structurally_valid_geojson():
    result = _ward_join_result(n=3)
    prov = ksmart_provenance(layer="wb_kerala", fetched="x", built="y")
    collection = to_feature_collection(result, prov)

    assert collection["type"] == "FeatureCollection"
    assert len(collection["features"]) == 3
    for feature in collection["features"]:
        assert feature["type"] == "Feature"
        assert feature["geometry"]["type"] == "Polygon"
        assert isinstance(feature["properties"], dict)


def test_feature_count_equals_crosswalk_row_count_for_the_tier():
    result = _ward_join_result(n=5)
    prov = ksmart_provenance(layer="wb_kerala", fetched="x", built="y")
    collection = to_feature_collection(result, prov)
    assert len(collection["features"]) == len(result.features) == 5


# --- write + parse round trip -----------------------------------------------


def test_emitted_ward_geojson_parses_and_has_provenance(tmp_path: Path):
    paths = resolve_paths(tmp_path)
    result = _ward_join_result(n=4)

    out_path = emit_ward_layer(paths, result, fetched="2026-07-30", built="2026-08-06")

    assert out_path.exists()
    loaded = json.loads(out_path.read_text(encoding="utf-8"))
    assert loaded["type"] == "FeatureCollection"
    assert len(loaded["features"]) == 4
    assert loaded["provenance"]["source_url"] == KSMART_SOURCE_URL
    assert loaded["provenance"]["accuracy"] == ACCURACY_CAVEAT


def test_emitted_local_body_geojson_parses_and_has_provenance(tmp_path: Path):
    paths = resolve_paths(tmp_path)
    result = _lb_join_result(n=1)

    out_path = emit_local_body_layer(paths, result, fetched="2026-07-30", built="2026-08-06")

    loaded = json.loads(out_path.read_text(encoding="utf-8"))
    assert loaded["type"] == "FeatureCollection"
    assert len(loaded["features"]) == 1
    assert loaded["features"][0]["properties"]["lb_seats_udf"] == 12
    assert "provenance" in loaded


def test_write_feature_collection_writes_under_paths_final(tmp_path: Path):
    paths = resolve_paths(tmp_path)
    result = _ward_join_result(n=1)
    prov = ksmart_provenance(layer="wb_kerala", fetched="x", built="y")

    out_path = paths.final / "custom.geojson"
    write_feature_collection(out_path, result, prov)

    assert out_path.parent == paths.final
    assert out_path.exists()


def test_empty_join_result_still_emits_valid_empty_feature_collection(tmp_path: Path):
    paths = resolve_paths(tmp_path)
    result = JoinResult(layer="wb_kerala", features=[], unmatched=[])

    out_path = emit_ward_layer(paths, result, fetched="x", built="y")
    loaded = json.loads(out_path.read_text(encoding="utf-8"))
    assert loaded["features"] == []
    assert loaded["type"] == "FeatureCollection"
