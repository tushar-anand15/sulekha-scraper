"""Stitching per-tile MVT fragments back into whole polygons.

Every tile here is synthetic, built with ``mapbox_vector_tile.encode`` and written
to a temporary cache laid out exactly like ``Paths.tile`` produces
(``{layer}/{z}/{x}/{y}.mvt``), so the test suite proves the stitcher against known
geometry rather than the live KSMART server. See the plan's "Source recon" section
for why gzip-sniffing and seam tolerance are the two things this module cannot get
wrong silently.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import mapbox_vector_tile
import pytest
from shapely.affinity import affine_transform
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from geo.build.stitch import (
    EXPECTED_STATEWIDE_COUNTS,
    StitchError,
    decode_tile,
    iter_cached_tiles,
    reconcile,
    stitch_layer,
)
from geo.config import Paths
from geo.tiles import tile_affine

Z = 14
# An arbitrary but fixed base tile index, away from the antimeridian/poles.
BASE_X, BASE_Y = 11693, 7802


def _write_tile(
    paths: Paths,
    layer: str,
    z: int,
    x: int,
    y: int,
    features: list[dict],
    *,
    gzipped: bool = False,
) -> Path:
    """Encode ``features`` as one MVT layer and write it to the cache path."""
    body = mapbox_vector_tile.encode(
        [{"name": layer, "features": features}],
        default_options={"extents": 4096},
    )
    if gzipped:
        body = gzip.compress(body)
    path = paths.tile(layer, z, x, y)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _feature(geometry_wkt: str, properties: dict) -> dict:
    return {"geometry": geometry_wkt, "properties": properties}


def _expected_geom_3857(local_wkt: str, x: int, y: int, z: int) -> object:
    """Independently transform a local-coordinate WKT geometry into EPSG:3857,
    the same way the stitcher should, so tests do not just check the stitcher
    against itself."""
    from shapely import wkt as shapely_wkt

    a, b, d, e, xoff, yoff = tile_affine(x, y, z)
    return affine_transform(shapely_wkt.loads(local_wkt), (a, b, d, e, xoff, yoff))


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path)


def test_two_adjacent_tiles_union_into_one_polygon_no_hole(paths: Paths) -> None:
    """Half a square in the west tile, half in the east tile, sharing the seam."""
    layer = "wb_kerala"
    west = _feature(
        "POLYGON ((2048 0, 4096 0, 4096 4096, 2048 4096, 2048 0))",
        {"OBJECTID": 1, "District": "Kollam"},
    )
    east = _feature(
        "POLYGON ((0 0, 2048 0, 2048 4096, 0 4096, 0 0))",
        {"OBJECTID": 1, "District": "Kollam"},
    )
    _write_tile(paths, layer, Z, BASE_X, BASE_Y, [west])
    _write_tile(paths, layer, Z, BASE_X + 1, BASE_Y, [east])

    result = stitch_layer(paths, layer)

    assert list(result.features) == [(1,)]
    feature = result.features[(1,)]
    assert feature.fragment_count == 2

    expected_west = _expected_geom_3857(
        "POLYGON ((2048 0, 4096 0, 4096 4096, 2048 4096, 2048 0))", BASE_X, BASE_Y, Z
    )
    expected_east = _expected_geom_3857(
        "POLYGON ((0 0, 2048 0, 2048 4096, 0 4096, 0 0))", BASE_X + 1, BASE_Y, Z
    )
    expected = unary_union([expected_west, expected_east])

    # Reproject the stitched (WGS84) geometry back to a comparable metric by
    # comparing to the WGS84 area of the independently-built union instead of
    # trusting the stitcher's own conversion for the comparison.
    from geo.tiles import mercator_to_wgs84
    from shapely.ops import transform as shapely_transform

    expected_wgs84 = shapely_transform(
        lambda x, y, z=None: mercator_to_wgs84(x, y), expected
    )

    assert feature.geometry.area == pytest.approx(expected_wgs84.area, rel=1e-9)
    assert feature.geometry.geom_type == "Polygon"
    assert len(feature.geometry.interiors) == 0


def test_identity_grouping_keeps_two_wards_in_one_tile_separate(paths: Paths) -> None:
    layer = "wb_kerala"
    ward_a = _feature(
        "POLYGON ((0 0, 0 1000, 1000 1000, 1000 0, 0 0))", {"OBJECTID": 1, "District": "Kollam"}
    )
    ward_b = _feature(
        "POLYGON ((2000 2000, 2000 3000, 3000 3000, 3000 2000, 2000 2000))",
        {"OBJECTID": 2, "District": "Kollam"},
    )
    _write_tile(paths, layer, Z, BASE_X, BASE_Y, [ward_a, ward_b])

    result = stitch_layer(paths, layer)

    assert set(result.features) == {(1,), (2,)}
    assert result.features[(1,)].fragment_count == 1
    assert result.features[(2,)].fragment_count == 1
    assert not result.features[(1,)].geometry.equals(result.features[(2,)].geometry)


def test_feature_spanning_four_tiles_at_a_corner_has_no_seam_hole(paths: Paths) -> None:
    """Four full tiles, same identity, meeting at the shared 2x2 corner point."""
    layer = "lb_kerala"
    full_tile = "POLYGON ((0 0, 4096 0, 4096 4096, 0 4096, 0 0))"
    corners = [
        (BASE_X, BASE_Y),
        (BASE_X + 1, BASE_Y),
        (BASE_X, BASE_Y + 1),
        (BASE_X + 1, BASE_Y + 1),
    ]
    for x, y in corners:
        _write_tile(
            paths,
            layer,
            Z,
            x,
            y,
            [_feature(full_tile, {"lb_code": "G020103"})],
        )

    result = stitch_layer(paths, layer)

    assert list(result.features) == [("G020103",)]
    feature = result.features[("G020103",)]
    assert feature.fragment_count == 4
    assert feature.geometry.geom_type == "Polygon"
    assert len(feature.geometry.interiors) == 0, "seam left a hole at the tile corner"

    expected = unary_union(
        [_expected_geom_3857(full_tile, x, y, Z) for x, y in corners]
    )
    from geo.tiles import mercator_to_wgs84
    from shapely.ops import transform as shapely_transform

    expected_wgs84 = shapely_transform(lambda x, y, z=None: mercator_to_wgs84(x, y), expected)
    assert feature.geometry.area == pytest.approx(expected_wgs84.area, rel=1e-9)


def test_genuine_multipart_feature_stays_a_multipolygon(paths: Paths) -> None:
    """An island GP: two disjoint rings, one feature, must not silently merge or split."""
    layer = "wb_kerala"
    island = _feature(
        "MULTIPOLYGON ("
        "((0 0, 0 200, 200 200, 200 0, 0 0)), "
        "((3000 3000, 3000 3200, 3200 3200, 3200 3000, 3000 3000))"
        ")",
        {"OBJECTID": 7, "District": "Ernakulam"},
    )
    _write_tile(paths, layer, Z, BASE_X, BASE_Y, [island])

    result = stitch_layer(paths, layer)

    feature = result.features[(7,)]
    assert feature.geometry.geom_type == "MultiPolygon"
    assert len(feature.geometry.geoms) == 2


def test_undecodable_tile_raises_naming_the_path(paths: Paths) -> None:
    layer = "wb_kerala"
    path = paths.tile(layer, Z, BASE_X, BASE_Y)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a vector tile, just garbage bytes padded out further")

    with pytest.raises(StitchError) as excinfo:
        stitch_layer(paths, layer)
    assert str(path) in str(excinfo.value)

    with pytest.raises(StitchError) as excinfo2:
        decode_tile(path)
    assert str(path) in str(excinfo2.value)


def test_gzipped_cached_tile_decodes_the_same_as_uncompressed(paths: Paths) -> None:
    layer = "wb_kerala"
    feature = _feature(
        "POLYGON ((0 0, 0 1000, 1000 1000, 1000 0, 0 0))", {"OBJECTID": 1, "District": "Idukki"}
    )
    plain_path = _write_tile(paths, layer, Z, BASE_X, BASE_Y, [feature])
    plain_decoded = decode_tile(plain_path)

    gz_paths = Paths(paths.root.parent / "gz_root")
    gz_path = _write_tile(gz_paths, layer, Z, BASE_X, BASE_Y, [feature], gzipped=True)
    assert gz_path.read_bytes()[:2] == b"\x1f\x8b"
    gz_decoded = decode_tile(gz_path)

    assert gz_decoded[layer]["features"] == plain_decoded[layer]["features"]


def test_gzip_sniffed_by_magic_even_without_a_gz_suffix_hint(paths: Paths) -> None:
    """The cache stores everything as ``.mvt`` regardless of compression (see
    ``Paths.tile``), so the only signal available is the magic bytes themselves."""
    layer = "wb_kerala"
    feature = _feature("POLYGON ((0 0, 0 10, 10 10, 10 0, 0 0))", {"OBJECTID": 9, "District": "Wayanad"})
    path = _write_tile(paths, layer, Z, BASE_X, BASE_Y, [feature], gzipped=True)
    decoded = decode_tile(path)
    assert decoded[layer]["features"][0]["properties"]["OBJECTID"] == 9


def test_attribute_mismatch_across_fragments_is_reported_not_silently_dropped(
    paths: Paths,
) -> None:
    layer = "wb_kerala"
    west = _feature(
        "POLYGON ((2048 0, 4096 0, 4096 4096, 2048 4096, 2048 0))",
        {"OBJECTID": 1, "District": "Kollam"},
    )
    east = _feature(
        "POLYGON ((0 0, 2048 0, 2048 4096, 0 4096, 0 0))",
        {"OBJECTID": 1, "District": "KOLLAM"},  # disagrees with the west fragment
    )
    _write_tile(paths, layer, Z, BASE_X, BASE_Y, [west])
    _write_tile(paths, layer, Z, BASE_X + 1, BASE_Y, [east])

    result = stitch_layer(paths, layer)

    # The feature still stitches -- a mismatch is reported, not fatal -- but the
    # disagreement shows up rather than being silently resolved.
    assert (1,) in result.features
    assert len(result.mismatches) == 1
    mismatch = result.mismatches[0]
    assert mismatch.key == (1,)
    assert mismatch.field == "District"
    assert {mismatch.first_value, mismatch.other_value} == {"Kollam", "KOLLAM"}


def test_iter_cached_tiles_walks_the_layer_directory(paths: Paths) -> None:
    layer = "kerala_bp"
    a = _write_tile(paths, layer, Z, BASE_X, BASE_Y, [_feature("POLYGON ((0 0, 0 1, 1 1, 1 0, 0 0))", {})])
    b = _write_tile(paths, layer, Z, BASE_X + 1, BASE_Y, [_feature("POLYGON ((0 0, 0 1, 1 1, 1 0, 0 0))", {})])

    found = sorted(iter_cached_tiles(paths, layer))
    assert found == sorted([(Z, BASE_X, BASE_Y, a), (Z, BASE_X + 1, BASE_Y, b)])


def test_iter_cached_tiles_empty_for_an_unfetched_layer(paths: Paths) -> None:
    assert list(iter_cached_tiles(paths, "kerala_dp")) == []


def test_reconciliation_reports_counts_per_district(paths: Paths) -> None:
    layer = "kerala_bp"
    features = [
        _feature(
            "POLYGON ((0 0, 0 100, 100 100, 100 0, 0 0))",
            {"District": "Kollam", "Localbody": "Parassala", "Block Panc": "A", "Ward No": 1},
        ),
        _feature(
            "POLYGON ((200 200, 200 300, 300 300, 300 200, 200 200))",
            {"District": "Kollam", "Localbody": "Parassala", "Block Panc": "A", "Ward No": 2},
        ),
        _feature(
            "POLYGON ((500 500, 500 600, 600 600, 600 500, 500 500))",
            {"District": "Idukki", "Localbody": "Adimaly", "Block Panc": "B", "Ward No": 1},
        ),
    ]
    _write_tile(paths, layer, Z, BASE_X, BASE_Y, features)

    result = stitch_layer(paths, layer)
    report = reconcile(result)

    assert report.layer == layer
    assert report.total == 3
    assert report.per_district == {"Kollam": 2, "Idukki": 1}
    assert report.expected == EXPECTED_STATEWIDE_COUNTS[layer]
    assert report.shortfall == EXPECTED_STATEWIDE_COUNTS[layer] - 3


def test_missing_identity_field_raises_naming_the_path(paths: Paths) -> None:
    layer = "kerala_dp"
    feature = _feature(
        "POLYGON ((0 0, 0 10, 10 10, 10 0, 0 0))", {"District": "Kollam"}
    )  # missing Localbody and Ward No
    path = _write_tile(paths, layer, Z, BASE_X, BASE_Y, [feature])

    with pytest.raises(StitchError) as excinfo:
        stitch_layer(paths, layer)
    assert str(path) in str(excinfo.value)


def test_geometry_is_valid_in_the_frame_callers_actually_receive(tmp_path):
    """Repair must survive reprojection, not merely precede it.

    The mercator inverse is nonlinear in y, so a polygon repaired in EPSG:3857
    can come out of the transform self-intersecting. Repairing only before the
    transform therefore ships invalid geometry while looking correct in the
    projected frame -- which is exactly what happened on the real statewide
    cache: 40 of 21,002 wards, every one of them valid until reprojected.
    """
    from shapely.geometry import Polygon
    from shapely.ops import transform as shapely_transform

    from geo.build.stitch import _mercator_to_wgs84_xy, _repair

    # A sliver whose vertices are near-collinear at high latitude -- the shape
    # most sensitive to the nonlinearity.
    sliver = Polygon(
        [
            (8_300_000.0, 1_430_000.0),
            (8_300_100.0, 1_430_000.05),
            (8_300_200.0, 1_430_000.0),
            (8_300_100.0, 1_429_999.95),
        ]
    )
    reprojected = shapely_transform(_mercator_to_wgs84_xy, sliver)
    assert _repair(reprojected).is_valid


def test_repair_is_idempotent_on_valid_input():
    """Repair must not reshape geometry that was already fine."""
    from shapely.geometry import box

    from geo.build.stitch import _repair

    good = box(0, 0, 1, 1)
    assert _repair(good) is good


def test_repair_returns_areal_geometry_only():
    """make_valid salvages everything, including dangling edges.

    A polygon pinched at a point repairs to polygons *plus* a LineString, handed
    back as a GeometryCollection. A ward is an area: emitting a mixed collection
    breaks consumers and the simplification pass outright. 40 of 21,002 wards hit
    this on the real statewide build.
    """
    from shapely.geometry import Polygon

    from geo.build.stitch import _repair

    bowtie = Polygon([(0, 0), (2, 2), (2, 0), (0, 2)])
    assert not bowtie.is_valid
    repaired = _repair(bowtie)
    assert repaired.geom_type in ("Polygon", "MultiPolygon")
    assert repaired.is_valid


def test_repair_keeps_all_the_area_it_salvages():
    """Dropping the non-areal parts must not drop actual extent."""
    from shapely.geometry import Polygon

    from geo.build.stitch import _repair

    bowtie = Polygon([(0, 0), (2, 2), (2, 0), (0, 2)])
    repaired = _repair(bowtie)
    # The bowtie's two lobes are 1.0 each.
    assert repaired.area == pytest.approx(2.0)


def test_only_the_deepest_cached_zoom_is_used(paths: Paths) -> None:
    """Mixing zooms inflates every feature to its coarsest rendering.

    The cache holds every level the descent walked. Each carries its own
    generalised version of the same feature, so unioning across levels grows each
    polygon until it swallows its neighbours -- on the real statewide build that
    turned 0.244 sq deg of Ernakulam ward area into 1.621, with adjacent wards
    overlapping 56.8% instead of tiling.
    """
    coarse = _feature("POLYGON ((0 0, 0 4000, 4000 4000, 4000 0, 0 0))", {"OBJECTID": 1})
    fine = _feature("POLYGON ((0 0, 0 100, 100 100, 100 0, 0 0))", {"OBJECTID": 1})
    _write_tile(paths, "wb_kerala", Z - 1, BASE_X // 2, BASE_Y // 2, [coarse])
    _write_tile(paths, "wb_kerala", Z, BASE_X, BASE_Y, [fine])

    result = stitch_layer(paths, "wb_kerala")
    (feature,) = result.features.values()
    # The z-1 tile covers vastly more ground; if it were included the area would
    # be orders of magnitude larger.
    assert feature.fragment_count == 1


def test_fragments_are_clipped_to_their_own_tile(paths: Paths) -> None:
    """MVT clips to the tile *plus a margin*, so a fragment spills into the
    neighbouring tile's area and overlaps whatever genuinely lives there.

    Asserted via area: a fragment drawn well past the tile edge must contribute
    only the part inside its own tile.
    """
    from geo.tiles import EXTENT

    overspill = _feature(
        f"POLYGON ((0 0, 0 {EXTENT * 2}, {EXTENT * 2} {EXTENT * 2}, {EXTENT * 2} 0, 0 0))",
        {"OBJECTID": 7},
    )
    _write_tile(paths, "wb_kerala", Z, BASE_X, BASE_Y, [overspill])

    result = stitch_layer(paths, "wb_kerala")
    (feature,) = result.features.values()

    from shapely.geometry import box

    from geo.tiles import tile_bounds_3857

    tile = box(*tile_bounds_3857(BASE_X, BASE_Y, Z))
    # Reproject the tile box the same way the stitcher reprojects geometry.
    from shapely.ops import transform as shapely_transform

    from geo.build.stitch import _mercator_to_wgs84_xy

    tile_wgs84 = shapely_transform(_mercator_to_wgs84_xy, tile)
    assert feature.geometry.area <= tile_wgs84.area * 1.0001
