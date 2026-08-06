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
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from shapely.geometry import mapping

from geo.build.attributes import JoinResult
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


def to_feature_collection(
    result: JoinResult, provenance: Mapping[str, Any]
) -> dict[str, Any]:
    """Build the FeatureCollection dict -- kept separate from the file write so tests
    can inspect the structure without a round trip through disk."""
    return {
        "type": "FeatureCollection",
        "provenance": dict(provenance),
        "features": [
            {
                "type": "Feature",
                "geometry": mapping(f.geometry),
                "properties": f.properties,
            }
            for f in result.features
        ],
    }


def write_feature_collection(
    path: Path, result: JoinResult, provenance: Mapping[str, Any]
) -> dict[str, Any]:
    """Write one layer's FeatureCollection to ``path``, returning what was written.

    The gate is the caller's job (``JoinResult.gate()``), not this function's -- a
    writer that silently refused to write on an ungated problem would make
    ``validate`` and ``build`` behave differently for reasons buried in here rather
    than in the CLI's own gate-then-write shape.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    collection = to_feature_collection(result, provenance)
    path.write_text(json.dumps(collection, ensure_ascii=False, indent=None), encoding="utf-8")
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
