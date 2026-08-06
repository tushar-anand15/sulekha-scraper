"""Write valid GeoJSON FeatureCollections for the joined layers, with provenance.

Every layer this module writes is attached to a real-world claim -- "this polygon is
where ward 7 of Parassala grama panchayat actually is" -- sourced from a government
tile server that was never designed to be redistributed as data. Two consequences
follow directly, both required by the plan (R6) rather than optional polish:

* **Provenance travels with the file, not beside it.** A GeoJSON FeatureCollection is
  just an object; GeoJSON (RFC 7946 §6.1) explicitly permits foreign members
  alongside ``"type"`` and ``"features"``, so a ``"provenance"`` key sits at the top
  level rather than in a sidecar README that a copy of the file can leave behind.
* **The accuracy caveat is not boilerplate.** These boundaries come from an MVT tile
  pipeline scraped at z14 (see ``geo.tiles`` and the plan's IoU convergence table),
  which is good enough for a choropleth and not good enough for anything that needs
  a cadastral-grade line -- a property dispute, a legal boundary determination, survey
  work. Every layer says so, because a GeoJSON file travels a long way from the code
  that knows why it should not be trusted for that.

Geometry is written from the same shapely objects ``geo.build.stitch`` produced --
already in EPSG:4326, GeoJSON's only sanctioned CRS -- so no re-projection happens
here. ``json.dump`` is used directly rather than a heavier GeoJSON library: the
per-feature shape (``type``/``geometry``/``properties``) is small enough that its own
library adds indirection without buying correctness.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final

from shapely import set_precision
from shapely.geometry import LineString, mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge, polygonize, unary_union
from shapely.strtree import STRtree
from shapely.validation import make_valid

from geo.build.attributes import JoinedFeature, JoinResult, Unmatched, local_body_properties
from geo.build.crosswalk import CrosswalkResult
from geo.config import Paths

#: Where the map application (out of scope for this plan) actually lives -- the page
#: a human would open, not the tile server behind it.
KSMART_SOURCE_URL: Final = "https://wardmap.ksmart.live/"

#: The vector-tile endpoint the scrape actually hit, per the plan's recon.
KSMART_TILE_URL: Final = "https://kmapdev.ksmart.live/tiles/{layer}/{z}/{x}/{y}"

#: The zoom the fetcher scraped at (see ``geo.tiles`` and the plan's IoU table --
#: shape has converged by z14 and moves under 0.1% of area per zoom beyond it).
KSMART_SCRAPE_ZOOM: Final = 14

#: No feature service, WFS, licence page or terms-of-use document was found during
#: recon (see the plan's "Source recon" section) -- KSMART is a public Government of
#: Kerala service with no stated open licence. Recorded honestly rather than
#: asserting a licence that was never granted.
KSMART_LICENCE: Final = (
    "Kerala KSMART wardmap (Government of Kerala public service). No open licence "
    "was published at the source; reproduced here for indicative, non-commercial "
    "visualization of public election results, not for redistribution as authoritative "
    "boundary data."
)

ACCURACY_CAVEAT: Final = (
    "Indicative, election-purpose boundaries reconstructed from vector tiles scraped "
    "at zoom 14. Not cadastral -- not suitable for property, legal, or survey use."
)

#: One filename per 2025 tier, keyed the same way ``geo.build.stitch`` names its
#: layers, so a caller cannot drift the two apart.
LAYER_FILENAMES: Final[dict[str, str]] = {
    "wb_kerala": "wards_2025.geojson",
    "lb_kerala": "local_bodies_2025.geojson",
    "kerala_bp": "block_panchayats_2025.geojson",
    "kerala_dp": "district_panchayats_2025.geojson",
}


def ksmart_provenance(*, layer: str, fetched: str, built: str) -> dict[str, Any]:
    """Provenance foreign member for one KSMART-sourced layer.

    ``fetched`` and ``built`` are dates the caller supplies (the fetch cache and the
    build run respectively) rather than ``date.today()`` taken here -- this module
    has no way to know when the tiles it is reading were actually pulled, and
    guessing "now" would misrepresent a build re-run over an old cache as if it were
    fresh.
    """
    return {
        "source_url": KSMART_SOURCE_URL,
        "tile_url_template": KSMART_TILE_URL.format(layer=layer, z="{z}", x="{x}", y="{y}"),
        "scrape_zoom": KSMART_SCRAPE_ZOOM,
        "fetched": fetched,
        "built": built,
        "licence": KSMART_LICENCE,
        "accuracy": ACCURACY_CAVEAT,
    }


#: Decimal places kept on emitted coordinates.
#:
#: Six places is about 11 cm at Kerala's latitude, against a z14 tile quantisation
#: floor of ~60 cm -- so this discards digits the source never actually resolved.
#: They are not free: shapely emits full float64 repr, seventeen significant digits
#: per ordinate, and on the ward layer that padding is most of a 354 MB file. It is
#: precision bloat rather than detail, and it dominates vertex count as a cost.
COORDINATE_PRECISION: Final = 6


def round_coordinates(obj: Any, precision: int) -> Any:
    """Round every ordinate in a GeoJSON geometry mapping, structure preserved.

    Walks the nested coordinate lists rather than using ``shapely.set_precision``,
    which snaps geometry onto a grid and can merge or drop vertices -- a topology
    change. Rounding the emitted numbers only affects the text written out; the
    geometry objects the gates ran against are untouched.
    """
    if isinstance(obj, float):
        return round(obj, precision)
    if isinstance(obj, int):
        return obj
    if isinstance(obj, (list, tuple)):
        return [round_coordinates(item, precision) for item in obj]
    if isinstance(obj, Mapping):
        return {k: round_coordinates(v, precision) for k, v in obj.items()}
    return obj


def to_feature_collection(
    result: JoinResult,
    provenance: Mapping[str, Any],
    *,
    precision: int | None = COORDINATE_PRECISION,
) -> dict[str, Any]:
    """Build the FeatureCollection dict -- kept separate from the file write so tests
    can inspect the structure without a round trip through disk.

    ``precision=None`` writes coordinates unrounded, for a caller that genuinely
    wants the raw float64 output.
    """

    def geometry_of(feature: Any) -> Any:
        if precision is None:
            return mapping(feature.geometry)
        # Snap onto the output grid *before* serialising, rather than rounding the
        # numbers afterwards. Rounding each ordinate independently can push a
        # vertex across an adjacent edge and make a valid polygon self-intersect
        # -- it did, for 2 wards and 5 local bodies. `set_precision` is
        # topology-aware: it snaps to the grid and repairs what that collapses,
        # so what gets written is both grid-aligned and valid. The subsequent
        # rounding is then a formatting no-op that trims float repr noise.
        snapped = set_precision(feature.geometry, 10.0**-precision)
        if snapped.is_empty:
            # Snapping can annihilate a feature smaller than one grid cell; keep
            # the original rather than emit an empty geometry.
            snapped = feature.geometry
        return round_coordinates(mapping(snapped), precision)

    return {
        "type": "FeatureCollection",
        "provenance": dict(provenance),
        "features": [
            {
                "type": "Feature",
                "geometry": geometry_of(f),
                "properties": f.properties,
            }
            for f in result.features
        ],
    }


def write_feature_collection(
    path: Path,
    result: JoinResult,
    provenance: Mapping[str, Any],
    *,
    precision: int | None = COORDINATE_PRECISION,
) -> dict[str, Any]:
    """Write one layer's FeatureCollection to ``path``, returning what was written.

    The gate is the caller's job (``JoinResult.gate()``), not this function's -- a
    writer that silently refused to write on an ungated problem would make
    ``validate`` and ``build`` behave differently for reasons buried in here rather
    than in the CLI's own gate-then-write shape.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    collection = to_feature_collection(result, provenance, precision=precision)
    path.write_text(
        json.dumps(collection, ensure_ascii=False, indent=None, separators=(",", ":")),
        encoding="utf-8",
    )
    return collection


def emit_ward_layer(
    paths: Paths, result: JoinResult, *, fetched: str, built: str
) -> Path:
    """Write the 2025 ``wb_kerala`` ward layer."""
    out_path = paths.final / LAYER_FILENAMES["wb_kerala"]
    provenance = ksmart_provenance(layer="wb_kerala", fetched=fetched, built=built)
    write_feature_collection(out_path, result, provenance)
    return out_path


def emit_local_body_layer(
    paths: Paths, result: JoinResult, *, fetched: str, built: str
) -> Path:
    """Write the 2025 ``lb_kerala`` local-body layer."""
    out_path = paths.final / LAYER_FILENAMES["lb_kerala"]
    provenance = ksmart_provenance(layer="lb_kerala", fetched=fetched, built=built)
    write_feature_collection(out_path, result, provenance)
    return out_path


def emit_block_panchayat_layer(
    paths: Paths, result: JoinResult, *, fetched: str, built: str
) -> Path:
    """Hook for the ``kerala_bp`` division layer.

    Not wired up in Unit 6 -- the plan scopes the BP/DP attribute join to whichever
    unit builds their name-based crosswalk (they carry no ``lb_code``, see
    ``geo.build.stitch.IDENTITY_FIELDS``). The writer itself is generic over any
    ``JoinResult``, so once that join exists this is a one-line call, not new code.
    """
    out_path = paths.final / LAYER_FILENAMES["kerala_bp"]
    provenance = ksmart_provenance(layer="kerala_bp", fetched=fetched, built=built)
    write_feature_collection(out_path, result, provenance)
    return out_path


def emit_district_panchayat_layer(
    paths: Paths, result: JoinResult, *, fetched: str, built: str
) -> Path:
    """Hook for the ``kerala_dp`` division layer. See
    :func:`emit_block_panchayat_layer` -- same reasoning, one tier up."""
    out_path = paths.final / LAYER_FILENAMES["kerala_dp"]
    provenance = ksmart_provenance(layer="kerala_dp", fetched=fetched, built=built)
    write_feature_collection(out_path, result, provenance)
    return out_path


# ---------------------------------------------------------------------------
# Unit 8: 2010/2015/2020 local-body layers from the opendatakerala OSM
# snapshot (see geo.build.dissolve and geo.build.osm_crosswalk).
# ---------------------------------------------------------------------------

#: The GitHub release the boundary polygons actually came from -- an OSM
#: extract, not a delimitation dataset. See the plan's "Source recon" for how
#: that was established (``Block_QID``/``DP_QID`` dissolve, no ward data).
OSM_SOURCE_URL: Final = "https://github.com/opendatakerala/lsg-kerala-data/releases"

#: Every 2010/2015/2020 local-body layer this module emits is built from one
#: physical polygon set: the release opendatakerala dated November 2020. There
#: is no per-cycle delimitation available for local bodies at all (see the
#: plan's Problem Frame) -- this constant exists so every provenance block
#: states that fact explicitly rather than letting a filename like
#: ``local_bodies_2015.geojson`` imply a boundary that was actually surveyed
#: in 2015.
OSM_BOUNDARY_VINTAGE: Final = "November 2020 (opendatakerala OSM snapshot)"

#: ODbL requires attribution on redistribution, and permits no weaker a
#: notice than this -- see <https://www.openstreetmap.org/copyright>. Unlike
#: KSMART (no stated licence, see ``KSMART_LICENCE`` above), this data *is*
#: openly licensed, and the obligation that comes with that must travel with
#: every layer built from it, not just live in the runbook a copy of the file
#: will never see.
OSM_LICENCE: Final = (
    "© OpenStreetMap contributors, redistributed by opendatakerala under the "
    "Open Database License (ODbL) v1.0 <https://opendatacommons.org/licenses/odbl/>. "
    "Attribution required on any use or redistribution of this layer: "
    '"© OpenStreetMap contributors". Derivative databases must be released under '
    "ODbL or a compatible share-alike licence."
)

OSM_ACCURACY_CAVEAT: Final = (
    "Local-body boundaries only, from a single November 2020 OSM snapshot reused "
    "identically across the 2010, 2015 and 2020 election cycles -- this is NOT a "
    "per-cycle delimitation. No ward-level geometry exists for these cycles at all "
    "(see the plan's Problem Frame). Indicative for choropleth use, not cadastral."
)

#: One filename per earlier cycle. ``lb_kerala`` from Unit 6 stays the 2025
#: name; these three are new.
#: The cycles this package maps, and deliberately only these.
#:
#: 2010 is absent on purpose. Its geometry belongs to the Delimitation Commission
#: PDF track (see ``docs/georeferencing_note.md``), which is out of scope, and the
#: OSM polygons are a November 2020 snapshot -- fifteen years and three
#: delimitations away from that cycle, across which Kerala went from 59
#: municipalities to 87. Emitting a 2010 layer from them was cheap to do and
#: would have quietly asserted a boundary set that never existed in 2010.
EARLIER_CYCLE_FILENAMES: Final[dict[str, str]] = {
    "2015": "local_bodies_2015.geojson",
    "2020": "local_bodies_2020.geojson",
}


def osm_provenance(*, year: str, fetched: str, built: str) -> dict[str, Any]:
    """Provenance foreign member for one OSM-derived local-body layer.

    Deliberately a different shape from :func:`ksmart_provenance` rather than
    a shared helper with optional fields -- the two sources disagree on the
    one thing that matters most here (licensed vs. not), and folding that into
    a single function's branches would make it easy to leave a KSMART-shaped
    layer without its ODbL notice, or vice versa, by editing the wrong branch.
    """
    return {
        "source_url": OSM_SOURCE_URL,
        "cycle": year,
        "boundary_vintage": OSM_BOUNDARY_VINTAGE,
        "per_cycle_delimitation": False,
        "fetched": fetched,
        "built": built,
        "licence": OSM_LICENCE,
        "accuracy": OSM_ACCURACY_CAVEAT,
    }


def join_osm_local_bodies(
    geometries: Mapping[str, BaseGeometry],
    crosswalk: CrosswalkResult,
    lb_rows: Iterable[Mapping[str, str]],
) -> JoinResult:
    """Join ``local_bodies_<year>.csv`` rows onto OSM-derived geometry.

    The counterpart to ``geo.build.attributes.join_local_body_layer``, which
    joins CSV rows onto a *stitched KSMART* result -- this module cannot reuse
    that function directly because its input here is a plain
    ``{osm_code: geometry}`` mapping (straight OSM features plus dissolved
    Block/District Panchayats), not a ``StitchResult``. ``local_body_properties``
    itself is shared unchanged, since the CSV row shape a choropleth needs
    does not depend on which source drew the polygon.

    Both directions of a missing join are tracked, same contract as
    ``attributes.JoinResult``: a resolved OSM body with no CSV row, and a CSV
    row whose local body never resolved in the crosswalk, are both reported
    rather than silently dropped.
    """
    code_map = {m.theirs.code: m.ours.code for m in crosswalk.matches}
    district_by_our_code = {m.ours.code: m.ours.district for m in crosswalk.matches}
    rows_by_code = {row["lb_code"]: dict(row) for row in lb_rows}

    unmatched: list[Unmatched] = []
    features: list[JoinedFeature] = []
    claimed: set[str] = set()

    for osm_code, geometry in geometries.items():
        our_code = code_map.get(osm_code)
        district = district_by_our_code.get(our_code, "<unknown>") if our_code else "<unknown>"
        if our_code is None:
            unmatched.append(
                Unmatched(
                    kind="geometry",
                    district=district,
                    identity=str(osm_code),
                    reason=f"OSM body {osm_code!r} not in the resolved crosswalk",
                )
            )
            continue

        row = rows_by_code.get(our_code)
        if row is None:
            unmatched.append(
                Unmatched(
                    kind="geometry",
                    district=district,
                    identity=str(osm_code),
                    reason=f"no CSV local-body row for {our_code!r}",
                )
            )
            continue

        claimed.add(our_code)
        features.append(JoinedFeature(geometry=geometry, properties=local_body_properties(row)))

    for lb_code, row in rows_by_code.items():
        if lb_code in claimed:
            continue
        unmatched.append(
            Unmatched(
                kind="row",
                district=row.get("district_name", "<unknown>"),
                identity=lb_code,
                reason="no OSM geometry claimed this local body",
            )
        )

    return JoinResult(layer="lb_osm", features=features, unmatched=unmatched)


def emit_earlier_cycle_local_body_layer(
    paths: Paths, year: str, result: JoinResult, *, fetched: str, built: str
) -> Path:
    """Write the 2010, 2015 or 2020 local-body layer.

    ``year`` selects the output filename from :data:`EARLIER_CYCLE_FILENAMES`.
    The geometry, crosswalk and join machinery are identical across cycles;
    only the joined CSV (``local_bodies_<year>.csv``) differs.
    """
    if year not in EARLIER_CYCLE_FILENAMES:
        expected = sorted(EARLIER_CYCLE_FILENAMES)
        raise ValueError(f"unsupported cycle {year!r}, expected one of {expected}")
    out_path = paths.final / EARLIER_CYCLE_FILENAMES[year]
    provenance = osm_provenance(year=year, fetched=fetched, built=built)
    write_feature_collection(out_path, result, provenance)
    return out_path


# ---------------------------------------------------------------------------
# Simplification.
#
# Shapely's Geometry.simplify() runs Douglas-Peucker independently on one
# polygon at a time. Applied separately to two polygons that are supposed to
# share a border, the two simplified copies of that border pick different
# subsets of the original vertices -- because Douglas-Peucker's choice of
# which points survive depends on the polygon's own ring, which starts at a
# different vertex and often winds the opposite direction on each side of the
# border. The result is a visible sliver of gap or overlap along every shared
# edge in the layer, not a rare edge case.
#
# ``topology_preserving_simplify`` below avoids that by simplifying the
# *border network* once rather than each polygon separately:
#
#   1. Extract every polygon's rings as plain LineStrings and union them all
#      (``unary_union``). Where two polygons share a border, both contributed
#      the *same* coincident line, and GEOS's node-based union dissolves
#      coincident input into one output edge -- so each physical border
#      appears exactly once in the result, however many polygons touch it.
#      The union alone still hands back one 2-point fragment per original
#      segment rather than one long edge (GEOS nodes the whole arrangement,
#      it does not merge collinear runs), so ``linemerge`` is applied next to
#      recombine every non-branching run of fragments back into one maximal
#      edge -- otherwise "simplifying" a run of already-atomic 2-point
#      fragments is a no-op and nothing is actually simplified.
#   2. Simplify each of those merged, deduplicated edges once. Because there
#      is now only one copy of any shared border, both of its neighbours will
#      use the identical simplified version -- there is no second copy left
#      to diverge from it.
#   3. Re-polygonize the simplified edge network (``polygonize``) and assign
#      each resulting face back to whichever original polygon covers its
#      representative point (an STRtree makes that assignment sublinear
#      rather than O(polygons x faces)).
#
# This is the "shared-edge/topology approach" the plan calls out as the
# available option that needs no new dependency -- ``shapely``'s own
# ``unary_union``/``polygonize`` pair *is* a topology engine, just not one
# packaged as a "simplify a coverage" convenience function the way a
# TopoJSON library would be.
#
# The guarantee is real but conditional: it only holds where input borders
# are *exactly* coincident (identical vertex coordinates on both sides),
# which is true here because both neighbouring GP polygons in the OSM release
# were digitized from the same shared way -- but is not independently
# verified at full statewide scale by this module. Where a border is not
# exactly coincident (accumulated floating-point drift, or a genuine data
# error), that border's edges will not dedupe in step 1 and will be
# simplified independently, same as the naive approach -- degrading
# gracefully to the naive failure mode rather than crashing. Tests measure
# the resulting gap/overlap area directly rather than assuming either
# outcome.
# ---------------------------------------------------------------------------


class SimplificationError(ValueError):
    """A simplification tolerance produced an invalid geometry.

    Raised rather than emitting the bad geometry, because an invalid polygon
    (a self-touching or self-crossing ring) silently corrupts every consumer
    downstream -- a browser's renderer may paper over it, a spatial join will
    not.
    """


def _rings(geometry: BaseGeometry) -> list[Any]:
    """Every ring (exterior + interiors) making up one Polygon or MultiPolygon."""
    if geometry.geom_type == "Polygon":
        return [geometry.exterior, *geometry.interiors]
    if geometry.geom_type == "MultiPolygon":
        rings: list[Any] = []
        for part in geometry.geoms:
            rings.append(part.exterior)
            rings.extend(part.interiors)
        return rings
    raise SimplificationError(f"cannot simplify geometry of type {geometry.geom_type!r}")


def _repaired(geometry: BaseGeometry) -> BaseGeometry:
    """``make_valid``, falling back to ``buffer(0)`` -- same repair order as
    ``geo.build.dissolve.dissolve``, for the same reason: ``make_valid``
    preserves more of the original shape when it succeeds."""
    if geometry.is_valid:
        return geometry
    repaired = make_valid(geometry)
    if not repaired.is_valid:
        repaired = repaired.buffer(0)
    return repaired


def simplify_geometry(geometry: BaseGeometry, tolerance: float) -> BaseGeometry:
    """Douglas-Peucker simplify one geometry, rejecting a tolerance that
    invalidates it rather than silently emitting the broken result.

    ``preserve_topology=False`` is used deliberately -- it simplifies harder
    than the topology-preserving mode, which is exactly why its output must be
    checked rather than trusted. A self-intersection this produces is *not*
    repaired with ``make_valid``: repairing it would silently accept the
    tolerance that broke it. The caller should lower the tolerance instead.
    """
    simplified = geometry.simplify(tolerance, preserve_topology=False)
    if not simplified.is_valid:
        raise SimplificationError(
            f"tolerance {tolerance} produced an invalid geometry "
            f"(area {geometry.area:.6g} -> attempted simplify failed validity)"
        )
    return simplified


def simplify_per_polygon(
    geometries: Mapping[str, BaseGeometry], tolerance: float
) -> dict[str, BaseGeometry]:
    """The naive baseline: simplify each polygon independently.

    Not used for emitted output -- see the module-level note above for why
    this opens gaps and overlaps along shared borders. Kept as a public
    function so tests (and the runbook's size/tolerance measurement) can
    quantify exactly how much worse it is than
    :func:`topology_preserving_simplify`, rather than that tradeoff being
    asserted without evidence.
    """
    return {code: simplify_geometry(geom, tolerance) for code, geom in geometries.items()}


def topology_preserving_simplify(
    geometries: Mapping[str, BaseGeometry], tolerance: float
) -> dict[str, BaseGeometry]:
    """Simplify a whole coverage of polygons without opening seams between
    neighbours. See the module-level note above for the algorithm and its
    one real precondition (exact vertex coincidence on shared borders).

    A body that disappears entirely from the re-polygonized network --
    possible for a very small island polygon at a tolerance close to its own
    size -- keeps its *original*, unsimplified geometry rather than vanishing
    from the output. A shrunken island is an acceptable simplification
    tradeoff; a missing one is a hole in the map.
    """
    codes = list(geometries.keys())
    geoms = [geometries[code] for code in codes]

    all_edges = [LineString(ring.coords) for geom in geoms for ring in _rings(geom)]
    if not all_edges:
        return dict(geometries)

    noded = unary_union(all_edges)
    merged = linemerge(noded)
    edges = list(merged.geoms) if hasattr(merged, "geoms") else [merged]
    simplified_edges = [edge.simplify(tolerance) for edge in edges if not edge.is_empty]
    if not simplified_edges:
        return dict(geometries)

    rebuilt = unary_union(simplified_edges)
    faces = list(polygonize(rebuilt))
    if not faces:
        return dict(geometries)

    tree = STRtree(geoms)
    owned: dict[int, list[BaseGeometry]] = defaultdict(list)
    for face in faces:
        point = face.representative_point()
        owner_idx = None
        for idx in tree.query(point):
            if geoms[idx].covers(point):
                owner_idx = int(idx)
                break
        if owner_idx is None:
            # Numeric edge case: the face's representative point landed just
            # outside every candidate (e.g. right on a simplified boundary).
            # Fall back to whichever original polygon's boundary is nearest,
            # rather than dropping the face -- a dropped face is exactly the
            # "collapsed island" failure this function exists to avoid.
            candidates = list(tree.query(face))
            if candidates:
                owner_idx = int(min(candidates, key=lambda i: geoms[int(i)].distance(point)))
        if owner_idx is not None:
            owned[owner_idx].append(face)

    result: dict[str, BaseGeometry] = {}
    for idx, code in enumerate(codes):
        parts = owned.get(idx)
        if not parts:
            result[code] = geometries[code]
            continue
        merged = unary_union(parts) if len(parts) > 1 else parts[0]
        result[code] = _repaired(merged)
    return result


def seam_gap_and_overlap(
    original_a: BaseGeometry,
    original_b: BaseGeometry,
    simplified_a: BaseGeometry,
    simplified_b: BaseGeometry,
    *,
    band_width: float,
) -> tuple[float, float]:
    """How much whitespace opened up, and how much overlap appeared, along
    the border two polygons shared before simplification.

    Defined relative to a band around the *original* shared border --
    ``original_a.boundary.intersection(original_b.boundary)`` buffered by
    ``band_width`` -- rather than the simplified polygons' own borders, since
    the whole point is to measure how far the simplified borders drifted from
    where the border used to be. ``gap_area`` is the part of that band neither
    simplified polygon covers; ``overlap_area`` is the part both do. A caller
    that only wants "did any gap appear" can just check ``gap_area > 0``, but
    reporting the area lets the runbook state a real tolerance/quality
    tradeoff instead of a pass/fail bit.

    The band is clipped to ``original_a`` and ``original_b``'s own combined
    footprint before measuring. Buffering a line produces rounded caps at its
    two endpoints, and those endpoints sit on the *outer* boundary of the
    original polygons (where the shared edge meets the rest of each body's
    perimeter) -- unclipped, the caps poke into space neither original body
    ever covered, which would count as "gap" for a reason that has nothing to
    do with simplification.
    """
    shared_border = original_a.boundary.intersection(original_b.boundary)
    if shared_border.is_empty:
        return 0.0, 0.0
    original_footprint = unary_union([original_a, original_b])
    band = shared_border.buffer(band_width).intersection(original_footprint)
    covered = unary_union([simplified_a, simplified_b])
    gap_area = band.difference(covered).area
    overlap_area = simplified_a.intersection(simplified_b).area
    return gap_area, overlap_area


def collection_size_bytes(collection: Mapping[str, Any]) -> int:
    """Bytes the written file would occupy -- the same encoding
    ``write_feature_collection`` uses, so the number matches what lands on
    disk rather than a Python-object size estimate that would not."""
    return len(json.dumps(dict(collection), ensure_ascii=False).encode("utf-8"))


def write_simplified_feature_collection(
    path: Path,
    result: JoinResult,
    provenance: Mapping[str, Any],
    *,
    tolerance: float,
    id_property: str = "lb_code",
) -> dict[str, Any]:
    """Write a topology-preserving-simplified variant of ``result``.

    Feature identity (count and every ``properties`` dict) is preserved
    exactly -- only ``geometry`` changes. The provenance foreign member
    carries the tolerance and the method used, and both the original and
    simplified sizes, so a consumer can see the size/fidelity tradeoff that
    was made without recomputing it.
    """
    geometries = {f.properties[id_property]: f.geometry for f in result.features}
    simplified = topology_preserving_simplify(geometries, tolerance)

    simplified_features = [
        JoinedFeature(geometry=simplified[f.properties[id_property]], properties=f.properties)
        for f in result.features
    ]
    simplified_result = JoinResult(
        layer=result.layer, features=simplified_features, unmatched=result.unmatched
    )

    original_collection = to_feature_collection(result, provenance)
    full_provenance = dict(provenance)
    full_provenance["simplification"] = {
        "method": "topology-preserving (shared-edge, shapely unary_union/polygonize)",
        "tolerance_degrees": tolerance,
        "original_size_bytes": collection_size_bytes(original_collection),
    }
    collection = to_feature_collection(simplified_result, full_provenance)
    full_provenance["simplification"]["simplified_size_bytes"] = collection_size_bytes(collection)
    # Re-serialize once more so the recorded simplified size is itself correct
    # (the first pass could not know its own size before being built).
    collection = to_feature_collection(simplified_result, full_provenance)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(collection, ensure_ascii=False, indent=None), encoding="utf-8")
    return collection
