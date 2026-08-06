"""Dissolve opendatakerala's Grama Panchayat polygons into Block/District Panchayats.

The offline half of Unit 7. The opendatakerala release has only one admin
tier -- 1,034 ``admin_level=8`` polygons (Grama Panchayats, Municipalities,
Corporations) -- but every Grama Panchayat carries its parent ``Block_QID`` and
``DP_QID``. Dissolving GPs on those keys reconstructs the two tiers the source
never draws directly: 152 Block Panchayats and 14 District Panchayats. That
reaches all 1,200 Kerala local bodies from one source, without a second one.

Municipalities and Corporations are excluded from both dissolves on purpose:
they sit outside the two-tier rural panchayat hierarchy this module
reconstructs and are already atomic local bodies in their own right. Folding
one into a Block or District Panchayat polygon would not just be conceptually
wrong -- it would silently inflate that polygon's area, and nothing about the
dissolve's own output would look wrong enough to notice. It would only surface
much later, as a puzzling area mismatch against an independent source.

This module must never import an HTTP client, at any depth --
``tests/geo/test_no_network.py`` AST-walks every file under ``geo/build`` and
fails the suite if one shows up, however nested. The cached release GeoJSON
that :mod:`geo.fetch.opendatakerala` downloads is the only input; this module
only ever reads it from disk.

Licence: the input polygons are OpenStreetMap data redistributed under the
Open Database License (ODbL) by opendatakerala. Layers derived from this
module's output inherit ODbL's attribution and share-alike obligations -- see
``docs/geo_runbook.md``.

Scope note: this module stops at dissolved geometry keyed by the source's own
identifiers (``Block_QID`` / ``DP_QID`` / ``LSGI_Code``). Crosswalking those to
our own ``lb_code`` is Unit 5's machinery, applied separately.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.validation import make_valid

#: The only tier value a Grama Panchayat carries in ``local_auth``. Only these
#: features are eligible to feed either dissolve.
GRAM_PANCHAYAT: Final = "gram_panchayat"

#: Tiers that must never enter a Block/District Panchayat dissolve. See the
#: module docstring for why folding these in would be silently wrong rather
#: than loudly wrong.
EXCLUDED_TIERS: Final = frozenset({"municipality", "municipal_corporation"})

#: The property holding a GP's own identifier, used only for reporting.
CODE_FIELD: Final = "LSGI_Code"


@dataclass(frozen=True, slots=True)
class MissingCode:
    """A Grama Panchayat that could not be dissolved: its group key was null.

    Reported rather than silently dropped. A GP missing ``Block_QID`` or
    ``DP_QID`` would otherwise just vanish from that tier -- one fewer feature
    in the output, with nothing in the dissolve itself to flag that a body is
    now missing from the map.
    """

    lsgi_code: str
    name: str | None
    group_field: str


@dataclass(frozen=True, slots=True)
class DissolvedBody:
    """One dissolved Block Panchayat or District Panchayat.

    ``member_codes`` carries the source ``LSGI_Code`` of every Grama Panchayat
    folded into this polygon -- both for provenance and so a test can verify a
    dissolved body's area against the sum of exactly its own members.
    """

    qid: str
    name: str | None
    district: str | None
    geometry: BaseGeometry
    member_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DissolveResult:
    bodies: tuple[DissolvedBody, ...]
    missing: tuple[MissingCode, ...]


def load_features(path: str | Path) -> list[dict[str, Any]]:
    """Read a GeoJSON FeatureCollection's ``features`` list.

    Kept dumb on purpose: no filtering, no validation beyond "is this even a
    FeatureCollection" -- that belongs in :func:`dissolve`, where it can be
    tested against small fixtures instead of the real 6 MB file.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    features = data.get("features")
    if not isinstance(features, list):
        raise ValueError(f"{path}: not a GeoJSON FeatureCollection (no 'features' list)")
    return features


def _eligible_gram_panchayats(
    features: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Grama Panchayat features only -- Municipalities/Corporations dropped here.

    This is the single choke point both dissolves route through, so the
    exclusion cannot be forgotten by one call site and not the other.
    """
    eligible = []
    for feature in features:
        props = feature.get("properties", {})
        tier = props.get("local_auth")
        if tier in EXCLUDED_TIERS:
            continue
        if tier != GRAM_PANCHAYAT:
            # Anything that is neither a GP nor an explicitly excluded tier is
            # unexpected upstream data -- skip it rather than guess, since this
            # dissolve only ever composes GPs.
            continue
        eligible.append(feature)
    return eligible


def dissolve(
    features: list[dict[str, Any]],
    *,
    group_field: str,
    name_field: str,
    district_field: str = "District",
) -> DissolveResult:
    """Dissolve Grama Panchayat polygons grouped by ``group_field``.

    Used with ``group_field="Block_QID"`` for the 152 Block Panchayats and
    ``group_field="DP_QID"`` for the 14 District Panchayats -- the two calls
    that matter are :func:`dissolve_block_panchayats` and
    :func:`dissolve_district_panchayats` below; this is the shared machinery
    behind both so the exclusion and null-reporting logic exist once.

    Geometry is repaired after the union rather than before: ``unary_union``
    can produce a technically-invalid result (e.g. a self-touching ring) even
    when every input polygon was valid, and repairing the inputs first would
    not guarantee the output stays valid anyway. ``make_valid`` is tried first
    since it preserves more of the original shape than ``buffer(0)``; the
    latter is a fallback for whatever ``make_valid`` itself cannot fix.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    missing: list[MissingCode] = []

    for feature in _eligible_gram_panchayats(features):
        props = feature["properties"]
        key = props.get(group_field)
        if not key:
            missing.append(
                MissingCode(
                    lsgi_code=props.get(CODE_FIELD),
                    name=props.get("name"),
                    group_field=group_field,
                )
            )
            continue
        groups.setdefault(key, []).append(feature)

    bodies = []
    for key, members in sorted(groups.items()):
        geometries = [shape(member["geometry"]) for member in members]
        dissolved = unary_union(geometries)
        if not dissolved.is_valid:
            dissolved = make_valid(dissolved)
            if not dissolved.is_valid:
                dissolved = dissolved.buffer(0)
        first_props = members[0]["properties"]
        bodies.append(
            DissolvedBody(
                qid=key,
                name=first_props.get(name_field),
                district=first_props.get(district_field),
                geometry=dissolved,
                member_codes=tuple(m["properties"].get(CODE_FIELD) for m in members),
            )
        )

    return DissolveResult(bodies=tuple(bodies), missing=tuple(missing))


def dissolve_block_panchayats(features: list[dict[str, Any]]) -> DissolveResult:
    """Dissolve Grama Panchayats on ``Block_QID`` -- expect 152 features statewide."""
    return dissolve(features, group_field="Block_QID", name_field="BlockName")


def dissolve_district_panchayats(features: list[dict[str, Any]]) -> DissolveResult:
    """Dissolve Grama Panchayats on ``DP_QID`` -- expect 14 features statewide."""
    return dissolve(features, group_field="DP_QID", name_field="DP_Name")
