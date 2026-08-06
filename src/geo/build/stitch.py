"""Reassemble whole ward and local-body polygons from per-tile MVT fragments.

A tile clips every feature that crosses it, so one ward or local body arrives as
several fragments spread across several cached files. This module reads those
fragments off disk, places each in real-world coordinates, groups them by the
feature's own identity, and unions the group back into the geometry that feature
actually has.

Two failure modes matter more than the happy path here, both learned the hard way
during planning (see the plan's "Source recon" section):

* **Gzip is intermittent, not absent.** Roughly one tile in 44 arrives gzipped with
  no reliable signal beyond the ``1f8b`` magic bytes. A decoder that skips the sniff
  hands compressed bytes to the MVT parser, which raises -- and if that raise is
  read as "empty tile", the tile silently disappears. One such tile held 3,258
  wards. So gzip is sniffed by magic, never by a header, and a decode failure is
  never caught and downgraded to absence.
* **Seams are near-exact, not exact.** ``geo.tiles`` establishes that a vertex on a
  shared tile edge can disagree between neighbours by about a nanometre -- nine
  orders of magnitude below the z14 quantisation floor of ~0.6 m -- because the
  affine's translation term is a floating-point addition and addition is not
  associative. That is far too small to need a snapping buffer, and reaching for
  one anyway (at metre scale) would blur real detail instead of fixing anything.
  ``unary_union`` alone should zip the fragments cleanly; this is verified by area
  comparison in the tests, not assumed.

Union happens in EPSG:3857, not EPSG:4326: the quantisation floor and the seam
tolerance above are both stated in metres, and metres are only meaningful in the
projected frame. The lon/lat conversion happens once, after the geometry is whole.
"""

from __future__ import annotations

import gzip
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import mapbox_vector_tile
from shapely.affinity import affine_transform
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform
from shapely.ops import unary_union
from shapely.validation import make_valid

from geo.config import Paths
from geo.tiles import mercator_to_wgs84, tile_affine

#: The first two bytes of every gzip stream, regardless of what any HTTP header
#: claims. See the module docstring -- trusting the header instead of the magic is
#: exactly the bug that dropped 3,258 wards during planning.
GZIP_MAGIC: Final = b"\x1f\x8b"

#: The tuple of feature properties that identifies "the same real-world feature"
#: within a layer, so its fragments can be found and unioned. Ported from the plan's
#: recon table (see "Source recon"). `wb_kerala`, `lb_kerala`, `kerala_bp` and
#: `kerala_dp` are as specified there; the KSMART site gave no field list for the two
#: tier-membership layers (`kerala_bp_with_lsgd`, `kerala_dp_with_block`), so their
#: keys below are a documented best guess -- nest the finer tier's identity inside
#: the coarser one's, since that is what "membership" means -- and should be
#: revisited against the real schema once Unit 3's fetch populates the cache.
IDENTITY_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "wb_kerala": ("OBJECTID",),
    "lb_kerala": ("lb_code",),
    "kerala_bp": ("District", "Block Panc", "Ward No"),
    "kerala_dp": ("District", "Localbody", "Ward No"),
    # Best-guess composite (see note above): a BP division carrying the GP ward
    # nested inside it.
    "kerala_bp_with_lsgd": ("District", "Block Panc", "Ward No", "Lsgd_Name", "Ward_No"),
    # Best-guess composite: a DP division carrying the Block Panchayat nested
    # inside it.
    "kerala_dp_with_block": ("District", "Localbody", "Ward No", "Block Panc"),
}

#: The field holding a district name, for the reconciliation helper. `lb_kerala`
#: names it differently from the other three layers (see the recon table).
DISTRICT_FIELD: Final[dict[str, str]] = {
    "wb_kerala": "District",
    "lb_kerala": "DistName",
    "kerala_bp": "District",
    "kerala_dp": "District",
    "kerala_bp_with_lsgd": "District",
    "kerala_dp_with_block": "District",
}

#: Statewide feature counts to reconcile against, from Unit 4's verification table.
#: `wb_kerala` is deliberately not the whole 23,573-ward universe -- it is the three
#: KSMART-covered tiers (GP 17,335 + Municipality 3,205 + Corporation 420); comparing
#: it to the full ward count would look like a shortfall when nothing is missing.
EXPECTED_STATEWIDE_COUNTS: Final[dict[str, int]] = {
    "wb_kerala": 20_960,
    "kerala_bp": 2_267,
    "kerala_dp": 346,
}

IdentityKey = tuple[Any, ...]


class StitchError(Exception):
    """A tile could not be turned into geometry.

    Always raised with the offending path in the message. The one thing this module
    must never do is treat a decode failure as an empty tile -- that reinterpretation
    is what silently punched a 3,258-ward hole in the map during planning.
    """


@dataclass(frozen=True, slots=True)
class AttributeMismatch:
    """Two fragments of the same feature disagree on a property.

    Fragments of one feature come from different tiles but describe the same
    real-world thing, so their shared attributes should be identical. A mismatch
    is not fatal on its own -- ArcGIS exports are not immune to a stray blank or a
    re-encoded string -- but it must be reported, never silently resolved by
    picking one side.
    """

    key: IdentityKey
    field: str
    first_value: Any
    other_value: Any
    path: Path


@dataclass(slots=True)
class StitchedFeature:
    """One reassembled feature: whole geometry plus the attributes it carries."""

    key: IdentityKey
    properties: dict[str, Any]
    geometry: BaseGeometry
    fragment_count: int


@dataclass(slots=True)
class StitchResult:
    """Everything a single layer's stitch produced."""

    layer: str
    features: dict[IdentityKey, StitchedFeature] = field(default_factory=dict)
    mismatches: list[AttributeMismatch] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """Per-layer, per-district feature counts, for comparison against the ward
    tier counts in Unit 6 -- this module only counts what it stitched; it does not
    know what the CSV expects."""

    layer: str
    total: int
    expected: int | None
    per_district: dict[str, int]

    @property
    def shortfall(self) -> int | None:
        """``expected - total``, or ``None`` if this layer has no stated expectation."""
        if self.expected is None:
            return None
        return self.expected - self.total


def _mercator_to_wgs84_xy(x: float, y: float, z: float | None = None) -> tuple[float, float]:
    """Adapter for ``shapely.ops.transform``, which calls with an optional z."""
    lon, lat = mercator_to_wgs84(x, y)
    return lon, lat


def read_tile_bytes(path: Path) -> bytes:
    """The raw bytes of one cached tile, decompressed if gzipped.

    Sniffs the ``1f8b`` magic rather than trusting any stored content-encoding --
    the cache is just bytes on disk, and the point of the sniff (per the module
    docstring) is to not need a header at all.
    """
    raw = path.read_bytes()
    if raw[:2] == GZIP_MAGIC:
        try:
            return gzip.decompress(raw)
        except OSError as exc:
            raise StitchError(f"{path}: gzip magic present but decompression failed") from exc
    return raw


def decode_tile(path: Path) -> dict[str, Any]:
    """Decode one cached tile into mapbox_vector_tile's per-layer dict.

    Any decode failure raises :class:`StitchError` naming ``path``. A skipped tile
    is a hole in the map, so there is no fallback here -- a caller that wants to
    tolerate a bad tile must decide that explicitly, not get it as this function's
    default behaviour.
    """
    raw = read_tile_bytes(path)
    try:
        return mapbox_vector_tile.decode(raw)
    except Exception as exc:  # noqa: BLE001 - re-raised with the path, not swallowed
        raise StitchError(f"{path}: could not decode as MVT") from exc


def iter_cached_tiles(paths: Paths, layer: str) -> Iterator[tuple[int, int, int, Path]]:
    """Every tile cached for one layer, as ``(z, x, y, path)``.

    Mirrors ``Paths.tile``'s own layout (``{layer}/{z}/{x}/{y}.mvt``) in reverse, so
    the fetcher's directory structure is the only place that layout is spelled out.
    """
    layer_root = paths.tiles / layer
    if not layer_root.is_dir():
        return
    for z_dir in sorted((p for p in layer_root.iterdir() if p.is_dir()), key=lambda p: int(p.name)):
        z = int(z_dir.name)
        for x_dir in sorted((p for p in z_dir.iterdir() if p.is_dir()), key=lambda p: int(p.name)):
            x = int(x_dir.name)
            for f in sorted(x_dir.glob("*.mvt"), key=lambda p: int(p.stem)):
                yield z, x, int(f.stem), f


def _identity_key(
    properties: Mapping[str, Any], id_fields: tuple[str, ...], path: Path
) -> IdentityKey:
    key = tuple(properties.get(f) for f in id_fields)
    if any(v is None for v in key):
        raise StitchError(
            f"{path}: feature is missing identity field(s) {id_fields} "
            f"(got properties {dict(properties)!r})"
        )
    return key


def _repair(geom: BaseGeometry) -> BaseGeometry:
    """Union output is not guaranteed valid -- repair rather than assume.

    ``make_valid`` is preferred; it is the shapely-2.0 GEOS-backed repair and keeps
    more of the original shape than a blunt ``buffer(0)``. It is tried first and
    ``buffer(0)`` is the fallback for the rare case ``make_valid`` itself cannot
    handle.
    """
    if geom.is_valid:
        return geom
    repaired = make_valid(geom)
    if repaired.is_valid:
        return repaired
    return repaired.buffer(0)


def _union_fragments(geoms: list[BaseGeometry]) -> BaseGeometry:
    """Union a group of same-feature EPSG:3857 fragments into one geometry.

    A single fragment is returned unchanged rather than passed through
    ``unary_union`` -- not an optimisation, but avoiding a no-op GEOS call for the
    common case of a feature that landed wholly inside one tile.
    """
    merged = geoms[0] if len(geoms) == 1 else unary_union(geoms)
    return _repair(merged)


def stitch_layer(
    paths: Paths,
    layer: str,
    *,
    tiles: Iterable[tuple[int, int, int, Path]] | None = None,
    identity_fields: tuple[str, ...] | None = None,
) -> StitchResult:
    """Reassemble every feature of one layer from its cached tile fragments.

    ``tiles`` defaults to everything :func:`iter_cached_tiles` finds under
    ``paths.tiles/{layer}``; tests pass an explicit list so they can exercise a
    handful of synthetic tiles without touching the real cache layout.
    """
    id_fields = identity_fields if identity_fields is not None else IDENTITY_FIELDS.get(layer)
    if id_fields is None:
        raise StitchError(f"no identity fields registered for layer {layer!r}")

    tile_iter = iter_cached_tiles(paths, layer) if tiles is None else tiles

    fragments: dict[IdentityKey, list[BaseGeometry]] = defaultdict(list)
    first_seen_properties: dict[IdentityKey, dict[str, Any]] = {}
    first_seen_path: dict[IdentityKey, Path] = {}
    mismatches: list[AttributeMismatch] = []

    for z, x, y, path in tile_iter:
        decoded = decode_tile(path)
        layer_data = decoded.get(layer)
        if not layer_data:
            continue
        a, b, d, e, xoff, yoff = tile_affine(x, y, z)
        for feat in layer_data["features"]:
            props: dict[str, Any] = dict(feat["properties"])
            key = _identity_key(props, id_fields, path)
            geom_local = shape(feat["geometry"])
            geom_3857 = affine_transform(geom_local, (a, b, d, e, xoff, yoff))
            fragments[key].append(geom_3857)

            prior = first_seen_properties.get(key)
            if prior is None:
                first_seen_properties[key] = props
                first_seen_path[key] = path
            else:
                for field_name, value in props.items():
                    if field_name not in prior:
                        continue
                    if prior[field_name] != value:
                        mismatches.append(
                            AttributeMismatch(
                                key=key,
                                field=field_name,
                                first_value=prior[field_name],
                                other_value=value,
                                path=path,
                            )
                        )

    features: dict[IdentityKey, StitchedFeature] = {}
    for key, geoms in fragments.items():
        merged_3857 = _union_fragments(geoms)
        # Repaired again *after* reprojection, not only before it. The mercator
        # inverse is nonlinear in y, so it can move near-collinear vertices past
        # each other and reintroduce a self-intersection into a polygon that was
        # valid in the projected frame. Measured on the real statewide cache: of
        # 48 heavily-fragmented northern coastal wards, none were invalid in 3857
        # and three were invalid once reprojected. EPSG:4326 is what callers get,
        # so EPSG:4326 is what has to be valid.
        merged_wgs84 = _repair(shapely_transform(_mercator_to_wgs84_xy, merged_3857))
        features[key] = StitchedFeature(
            key=key,
            properties=first_seen_properties[key],
            geometry=merged_wgs84,
            fragment_count=len(geoms),
        )

    return StitchResult(layer=layer, features=features, mismatches=mismatches)


def reconcile(result: StitchResult) -> ReconciliationReport:
    """Per-district feature counts for one stitched layer.

    This is the completeness check Unit 4 needs and Unit 6 will compare against the
    ward-tier counts: it reports what was actually stitched, per district, so a
    pruned quadtree branch or a dropped tile shows up as a specific district falling
    short rather than a single statewide number that could hide where the gap is.
    """
    district_field = DISTRICT_FIELD.get(result.layer, "District")
    per_district: Counter[str] = Counter()
    for feature in result.features.values():
        district = feature.properties.get(district_field)
        per_district[str(district) if district is not None else "<unknown>"] += 1
    return ReconciliationReport(
        layer=result.layer,
        total=len(result.features),
        expected=EXPECTED_STATEWIDE_COUNTS.get(result.layer),
        per_district=dict(per_district),
    )
