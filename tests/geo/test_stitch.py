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
            {"District": "Kollam", "Block Panc": "A", "Ward No": 1},
        ),
        _feature(
            "POLYGON ((200 200, 200 300, 300 300, 300 200, 200 200))",
            {"District": "Kollam", "Block Panc": "A", "Ward No": 2},
        ),
        _feature(
            "POLYGON ((500 500, 500 600, 600 600, 600 500, 500 500))",
            {"District": "Idukki", "Block Panc": "B", "Ward No": 1},
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
