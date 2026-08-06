"""Unit 6: emitting the 2025 GeoJSON layers with provenance.

Runs entirely against a hand-built ``JoinResult`` -- there is no dependency on a real
tile cache or CSV file, so the suite passes on a clean clone even while the
statewide tile fetch (noted in the plan) is still in flight.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from shapely.geometry import Polygon, box

from geo.build.attributes import JoinedFeature, JoinResult
from geo.build.crosswalk import CrosswalkResult, LocalBody, Match
from geo.build.emit import (
    ACCURACY_CAVEAT,
    EARLIER_CYCLE_FILENAMES,
    KSMART_SCRAPE_ZOOM,
    KSMART_SOURCE_URL,
    OSM_BOUNDARY_VINTAGE,
    OSM_LICENCE,
    OSM_SOURCE_URL,
    SimplificationError,
    collection_size_bytes,
    emit_earlier_cycle_local_body_layer,
    emit_local_body_layer,
    emit_ward_layer,
    join_osm_local_bodies,
    ksmart_provenance,
    osm_provenance,
    seam_gap_and_overlap,
    simplify_geometry,
    simplify_per_polygon,
    to_feature_collection,
    topology_preserving_simplify,
    write_feature_collection,
    write_simplified_feature_collection,
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


# --- Unit 8: OSM provenance -------------------------------------------------


def test_osm_provenance_states_the_shared_boundary_vintage_explicitly():
    """R6: every OSM-derived layer must say, in its own provenance, that it
    reuses one November-2020 snapshot rather than a per-cycle delimitation --
    this is the load-bearing labelling requirement of Unit 8."""
    prov = osm_provenance(year="2015", fetched="2026-07-30", built="2026-08-06")
    assert prov["source_url"] == OSM_SOURCE_URL
    assert prov["cycle"] == "2015"
    assert prov["boundary_vintage"] == OSM_BOUNDARY_VINTAGE
    assert "2020" in prov["boundary_vintage"]
    assert prov["per_cycle_delimitation"] is False
    assert "per-cycle delimitation" in prov["accuracy"].lower()


def test_osm_provenance_carries_odbl_attribution_requirement():
    """ODbL requires attribution on redistribution -- this must be recorded
    per layer, not only in the runbook a copy of the file will never see."""
    prov = osm_provenance(year="2020", fetched="x", built="y")
    assert prov["licence"] == OSM_LICENCE
    assert "ODbL" in prov["licence"]
    assert "OpenStreetMap" in prov["licence"]
    assert "Attribution required" in prov["licence"]


def test_osm_and_ksmart_provenance_are_never_cross_labelled():
    """The KSMART layers have no stated open licence at all (see
    ``KSMART_LICENCE``); an OSM layer's ODbL notice must never leak onto one,
    and vice versa."""
    osm = osm_provenance(year="2015", fetched="x", built="y")
    ksmart = ksmart_provenance(layer="lb_kerala", fetched="x", built="y")
    assert "ODbL" not in ksmart["licence"]
    assert "ODbL" in osm["licence"]
    assert osm["source_url"] != ksmart["source_url"]


# --- Unit 8: joining CSV rows onto OSM geometry -----------------------------


def _match(our_code, osm_code, district="THIRUVANANTHAPURAM"):
    ours = LocalBody(code=our_code, name="Parassala", lb_type="Grama Panchayat", district=district)
    theirs = LocalBody(
        code=osm_code, name="Parassala", lb_type="Grama Panchayat", district=district
    )
    return Match(ours=ours, theirs=theirs, method="exact", ward_agreement=1.0)


def _crosswalk(*matches):
    return CrosswalkResult(matches=tuple(matches), unresolved=(), rejected=(), unclaimed=(), how={})


def test_join_osm_local_bodies_attaches_csv_row_to_its_geometry():
    crosswalk = _crosswalk(_match("G01001", "G01001"))
    geometries = {"G01001": box(0, 0, 1, 1)}
    rows = [{"lb_code": "G01001", "lb_name": "Parassala", "lb_type": "Grama Panchayat",
             "district_name": "THIRUVANANTHAPURAM", "lb_seats_udf": "8"}]

    result = join_osm_local_bodies(geometries, crosswalk, rows)

    assert len(result.features) == 1
    assert result.unmatched == []
    assert result.features[0].properties["lb_seats_udf"] == 8
    assert result.features[0].geometry.equals(box(0, 0, 1, 1))


def test_join_osm_local_bodies_reports_geometry_with_no_crosswalk_match():
    crosswalk = _crosswalk()
    geometries = {"G09999": box(0, 0, 1, 1)}

    result = join_osm_local_bodies(geometries, crosswalk, [])

    assert result.features == []
    assert len(result.unmatched) == 1
    assert result.unmatched[0].kind == "geometry"


def test_join_osm_local_bodies_reports_row_with_no_geometry():
    crosswalk = _crosswalk(_match("G01001", "G01001"))
    rows = [
        {"lb_code": "G01001", "lb_name": "Parassala", "district_name": "THIRUVANANTHAPURAM"},
        {"lb_code": "G02002", "lb_name": "Nowhere", "district_name": "KOLLAM"},
    ]

    result = join_osm_local_bodies({"G01001": box(0, 0, 1, 1)}, crosswalk, rows)

    assert len(result.features) == 1
    assert len(result.unmatched) == 1
    assert result.unmatched[0].kind == "row"
    assert result.unmatched[0].identity == "G02002"


# --- Unit 8: emitting the 2010/2015/2020 local-body layers ------------------


def _osm_lb_result():
    return JoinResult(
        layer="lb_osm",
        features=[JoinedFeature(geometry=box(0, 0, 5, 5), properties={"lb_code": "G01001"})],
        unmatched=[],
    )


@pytest.mark.parametrize("year", ["2015", "2020"])
def test_emit_earlier_cycle_local_body_layer_writes_the_right_filename(tmp_path: Path, year: str):
    paths = resolve_paths(tmp_path)
    out_path = emit_earlier_cycle_local_body_layer(
        paths, year, _osm_lb_result(), fetched="2026-07-30", built="2026-08-06"
    )
    assert out_path.name == EARLIER_CYCLE_FILENAMES[year]
    loaded = json.loads(out_path.read_text(encoding="utf-8"))
    assert loaded["type"] == "FeatureCollection"
    assert loaded["provenance"]["cycle"] == year
    assert loaded["provenance"]["per_cycle_delimitation"] is False


def test_emit_earlier_cycle_local_body_layer_rejects_unknown_year(tmp_path: Path):
    paths = resolve_paths(tmp_path)
    with pytest.raises(ValueError):
        emit_earlier_cycle_local_body_layer(paths, "1999", _osm_lb_result(), fetched="x", built="y")


# --- Unit 8: simplification --------------------------------------------------
#
# The fixture below is not arbitrary: it is a fixed (seeded, then hand-frozen)
# pair of adjacent polygons whose shared border is an irregular, many-vertex
# line, constructed so that Shapely's Douglas-Peucker simplifier -- applied
# independently to each polygon's own ring, as a naive per-polygon approach
# would -- provably diverges on the shared edge (measured directly: gap area
# 0.174, overlap area 0.186 at tolerance 0.2). The exact same border,
# simplified once via ``topology_preserving_simplify``, produces zero gap and
# zero overlap on this fixture. This demonstrates the failure mode the plan
# calls out, rather than asserting it without evidence.

_ADJACENT_HEIGHT = 13.536840165002925
_ADJACENT_SHARED_BORDER = [
    (5, 0.0), (4.71742897718209, 0.33), (5.2947604312587515, 0.66),
    (4.735138419206154, 0.99), (5.020016111087356, 1.32), (5.175434474903986, 1.65),
    (5.098728221697389, 1.98), (5.193583819749518, 2.31), (4.743197836366379, 2.64),
    (4.982383818180076, 2.97), (5.127010925195766, 3.3), (4.869575608878419, 3.63),
    (5.0445396593146965, 3.96), (4.827342424469583, 4.29), (4.797366447479445, 4.62),
    (5.162711852978326, 4.95), (5.132655772856706, 5.28), (5.0798981186219665, 5.61),
    (4.968324072276222, 5.94), (4.867507719897218, 6.27), (4.749356881509099, 6.93),
    (4.987280861142817, 7.26), (5.173078521710089, 7.59), (4.842544666608891, 7.92),
    (5.038945694115675, 8.25), (5.209405167243428, 8.58), (5.213746671047638, 8.91),
    (4.836283063305527, 9.24), (5, 9.57),
]


def _adjacent_polygons() -> tuple[Polygon, Polygon]:
    """Two polygons sharing ``_ADJACENT_SHARED_BORDER`` bit-for-bit -- the
    precondition ``topology_preserving_simplify`` relies on (see its
    docstring), and the realistic case for OSM-derived data where two local
    bodies literally trace the same digitized way."""
    reversed_border = list(reversed(_ADJACENT_SHARED_BORDER))
    left = Polygon([(0, 0), (0, _ADJACENT_HEIGHT), *reversed_border, (0, 0)])
    right = Polygon([(10, 0), (10, _ADJACENT_HEIGHT), *reversed_border, (10, 0)])
    assert left.is_valid and right.is_valid and left.touches(right)
    return left, right


def test_topology_preserving_simplify_keeps_neighbours_seamless():
    left, right = _adjacent_polygons()
    simplified = topology_preserving_simplify({"left": left, "right": right}, tolerance=0.2)

    gap, overlap = seam_gap_and_overlap(
        left, right, simplified["left"], simplified["right"], band_width=0.1
    )
    assert gap < 1e-6
    assert overlap < 1e-6
    # And it actually simplified something -- this is not a no-op tolerance.
    assert len(simplified["left"].exterior.coords) < len(left.exterior.coords)


def test_naive_per_polygon_simplify_opens_a_measurable_seam():
    """The tradeoff topology-preserving simplification exists to avoid,
    measured rather than asserted -- see the fixture note above."""
    left, right = _adjacent_polygons()
    naive = simplify_per_polygon({"left": left, "right": right}, tolerance=0.2)

    gap, overlap = seam_gap_and_overlap(left, right, naive["left"], naive["right"], band_width=0.1)
    assert gap > 0.01
    assert overlap > 0.01


def test_simplification_preserves_feature_count_and_identity_attributes(tmp_path: Path):
    left, right = _adjacent_polygons()
    result = JoinResult(
        layer="lb_osm",
        features=[
            JoinedFeature(geometry=left, properties={"lb_code": "G01001", "lb_name": "Left"}),
            JoinedFeature(geometry=right, properties={"lb_code": "G01002", "lb_name": "Right"}),
        ],
        unmatched=[],
    )
    prov = osm_provenance(year="2020", fetched="x", built="y")

    out_path = tmp_path / "simplified.geojson"
    collection = write_simplified_feature_collection(out_path, result, prov, tolerance=0.2)

    assert len(collection["features"]) == 2
    written = json.loads(out_path.read_text(encoding="utf-8"))
    assert {f["properties"]["lb_code"] for f in written["features"]} == {"G01001", "G01002"}
    assert {f["properties"]["lb_name"] for f in written["features"]} == {"Left", "Right"}
    assert written["provenance"]["simplification"]["tolerance_degrees"] == 0.2
    assert written["provenance"]["simplification"]["original_size_bytes"] > 0
    assert written["provenance"]["simplification"]["simplified_size_bytes"] > 0


def test_small_island_survives_simplification_instead_of_collapsing():
    mainland = box(0, 0, 100, 100)
    island = box(500, 500, 500.05, 500.05)  # far smaller than the tolerance

    simplified = topology_preserving_simplify(
        {"mainland": mainland, "island": island}, tolerance=1.0
    )

    assert not simplified["island"].is_empty
    assert simplified["island"].area > 0
    # Falls back to the original geometry rather than emitting a mangled one.
    assert simplified["island"].equals(island)


class _AlwaysInvalidAfterSimplify:
    """A stand-in for a geometry whose ``simplify()`` always returns a
    genuinely invalid (self-intersecting) polygon.

    Real ``shapely``/GEOS is, in practice, very reluctant to hand back an
    actually-invalid polygon from ``simplify()`` even with
    ``preserve_topology=False`` -- several deliberately adversarial fixtures
    (stars, near-closed spirals, thin notches) were tried while writing this
    test and none broke it in the installed GEOS version. Rather than the
    test's correctness depending on a specific GEOS version's internal
    robustness, this stub exercises the rejection contract directly: *given*
    an invalid result, :func:`simplify_geometry` must raise, not return it.
    """

    area = 1.0

    def simplify(self, tolerance, preserve_topology=False):
        # A literal bowtie: (0,0)-(1,1)-(1,0)-(0,1)-(0,0) crosses itself.
        return Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)])


def test_a_tolerance_that_invalidates_geometry_is_rejected():
    assert not Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)]).is_valid
    with pytest.raises(SimplificationError):
        simplify_geometry(_AlwaysInvalidAfterSimplify(), tolerance=1.0)


def test_collection_size_bytes_matches_the_written_file(tmp_path: Path):
    result = _lb_join_result(n=2)
    prov = ksmart_provenance(layer="lb_kerala", fetched="x", built="y")
    collection = to_feature_collection(result, prov)

    out_path = tmp_path / "sized.geojson"
    out_path.write_text(json.dumps(collection, ensure_ascii=False), encoding="utf-8")

    assert collection_size_bytes(collection) == out_path.stat().st_size


def test_every_emitted_layer_still_parses_as_valid_geojson(tmp_path: Path):
    """Integration: run the whole Unit 8 write path for one cycle and confirm
    both the full-fidelity and simplified files round-trip through
    ``json.loads``, with their sizes recorded."""
    paths = resolve_paths(tmp_path)
    left, right = _adjacent_polygons()
    result = JoinResult(
        layer="lb_osm",
        features=[
            JoinedFeature(geometry=left, properties={"lb_code": "G01001"}),
            JoinedFeature(geometry=right, properties={"lb_code": "G01002"}),
        ],
        unmatched=[],
    )

    full_path = emit_earlier_cycle_local_body_layer(
        paths, "2015", result, fetched="2026-07-30", built="2026-08-06"
    )
    full_loaded = json.loads(full_path.read_text(encoding="utf-8"))
    assert full_loaded["type"] == "FeatureCollection"
    full_size = full_path.stat().st_size

    simplified_path = paths.final / "local_bodies_2015.simplified.geojson"
    prov = osm_provenance(year="2015", fetched="2026-07-30", built="2026-08-06")
    write_simplified_feature_collection(simplified_path, result, prov, tolerance=0.2)
    simplified_loaded = json.loads(simplified_path.read_text(encoding="utf-8"))
    assert simplified_loaded["type"] == "FeatureCollection"
    simplified_size = simplified_path.stat().st_size

    assert full_size > 0
    assert simplified_size > 0


# --- coordinate precision ----------------------------------------------------


def test_coordinates_are_rounded_to_the_configured_precision():
    """Full float64 repr is ~17 digits per ordinate against a 60 cm data quantum.

    On the ward layer that padding was most of a 354 MB file -- precision bloat,
    not detail.
    """
    from shapely.geometry import Polygon

    from geo.build.attributes import JoinResult, JoinedFeature
    from geo.build.emit import to_feature_collection

    poly = Polygon([(76.123456789012, 10.987654321098), (76.2, 10.9), (76.1, 11.0)])
    result = JoinResult(layer="wb_kerala", features=[JoinedFeature(geometry=poly, properties={})], unmatched=[])
    coll = to_feature_collection(result, {}, precision=6)
    x, y = coll["features"][0]["geometry"]["coordinates"][0][0]
    assert x == 76.123457
    assert y == 10.987654


def test_precision_none_keeps_full_float_output():
    from shapely.geometry import Polygon

    from geo.build.attributes import JoinResult, JoinedFeature
    from geo.build.emit import to_feature_collection

    poly = Polygon([(76.123456789012, 10.987654321098), (76.2, 10.9), (76.1, 11.0)])
    result = JoinResult(layer="wb_kerala", features=[JoinedFeature(geometry=poly, properties={})], unmatched=[])
    coll = to_feature_collection(result, {}, precision=None)
    assert coll["features"][0]["geometry"]["coordinates"][0][0][0] == 76.123456789012


def test_rounding_preserves_geometry_structure():
    """Rings, holes and multipart structure must survive untouched -- only the
    numbers change, never the nesting."""
    from shapely.geometry import MultiPolygon, Polygon

    from geo.build.emit import round_coordinates
    from shapely.geometry import mapping

    outer = [(0, 0), (0, 4), (4, 4), (4, 0)]
    hole = [(1, 1), (1, 2), (2, 2), (2, 1)]
    geom = MultiPolygon([Polygon(outer, [hole]), Polygon([(9, 9), (9, 10), (10, 10)])])
    before = mapping(geom)
    after = round_coordinates(before, 6)

    def shape_of(node):
        if isinstance(node, (list, tuple)):
            return [shape_of(n) for n in node]
        return None

    assert shape_of(before["coordinates"]) == shape_of(after["coordinates"])
    assert after["type"] == "MultiPolygon"


def test_2010_is_not_an_emittable_cycle():
    """2010's geometry belongs to the Delimitation Commission PDF track, which is
    out of scope. The OSM polygons are a Nov-2020 snapshot -- three delimitations
    later, across which Kerala went from 59 municipalities to 87 -- so emitting a
    2010 layer from them would assert a boundary set that never existed.
    """
    from geo.build.emit import EARLIER_CYCLE_FILENAMES

    assert "2010" not in EARLIER_CYCLE_FILENAMES
    assert set(EARLIER_CYCLE_FILENAMES) == {"2015", "2020"}
