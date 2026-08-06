"""Body- and division-level crosswalk for the 2025 Block/District Panchayat tiers.

``kerala_bp`` and ``kerala_dp`` are *division* layers -- one stitched feature per
Block Panchayat division or District Panchayat division, not one per local body.
Neither carries a body-level feature at all, and neither carries our ``lb_code``
(see ``stitch.IDENTITY_FIELDS``). That is two joins short of what
``geo.build.attributes`` does for wards and local bodies, which is why this module
exists rather than reusing ``join_ward_layer``/``join_local_body_layer`` unmodified:

1. **A body-level crosswalk that does not exist yet.** ``geo.build.crosswalk``
   pairs *bodies*; it has nothing to pair kerala_bp/kerala_dp's division rows to
   until they are first grouped back up into bodies (:func:`theirs_bodies_from_stitch`).
   Once grouped, pairing itself is unchanged -- ``build_crosswalk`` is reused
   wholesale, including its ward-name (here: division-name) verification gate,
   because a Block Panchayat name and a Grama Panchayat name transliterate through
   the same Malayalam-to-English inconsistency and need the same fuzzy cascade.
   District Panchayats need less of that: there is exactly one per district on
   both sides, so ``data_merge.transform.matching``'s existing
   ``_ONE_PER_DISTRICT_TYPES`` rule (already covers ``"District Panchayat"``)
   resolves all 14 on scope alone -- this module adds no DP-specific fuzzy
   matching on top of that, deliberately, because a coincidental name match
   would be a weaker signal than the one-per-district rule already gives.

2. **A ward-number join onto that pairing, once it exists.** This is a second
   hop past the body-level crosswalk, analogous to how ``join_ward_layer`` hops
   KSMART local-body code -> our ``lb_code`` -> ``(lb_code, ward_no)``, except
   there is no KSMART code to hop through here -- the body-level ``Match`` from
   step 1 stands in for it.

**The naming trap.** In ``kerala_bp``, ``Localbody`` is the *Block Panchayat*'s
name and ``Block Panc`` is the *division*'s name inside it -- backwards from what
both field names suggest. In ``kerala_dp``, ``Localbody`` is the *District
Panchayat*'s name (same string as the district itself) and ``Ward Name`` is the
division. Both layers were verified against the real cached tiles before this
module was written (see the caller's runbook), not re-derived here. Every
function below reads the body's name from ``Localbody`` and nothing else --
:data:`BODY_NAME_FIELD` exists so that fact is stated in exactly one place, and
:data:`TierConfig.division_name_field` carries the tier-specific field that must
never be read as the body's name.

**Division-level verification without a second independent signal of the same
shape.** ``crosswalk.py`` verifies a *body* pairing by comparing ward-name sets
between two otherwise-unrelated fields. There is no such second field for a
*division* pairing -- but there is a second, structurally unrelated KSMART table:
``kerala_bp_with_lsgd`` (every GP ward nested inside the BP division that
contains it) and ``kerala_dp_with_block`` (every BP division nested inside the
DP division that contains it). Both re-state the coarser division's own
``(District, Localbody, "Ward No")`` identity as a side effect of listing what
sits inside it. If that triple also appears in the membership layer, the
division pairing has independent corroboration; if it does not, per
``crosswalk.Match.verified``'s doctrine, that is *not* evidence of a wrong
pairing -- KSMART's own recon notes the membership layers' field list was a
documented best guess pending real data (see ``stitch.py``), so a division
missing from it is carried and flagged ``division_verified=False``, never
rejected.

**KSMART's kerala_bp is short 15 of our 2,267 BP divisions.** Verified against
the real cached tiles: ``stitch_layer(paths, "kerala_bp")`` yields 2,252
features, and KSMART's coverage gap sits inside bodies it otherwise knows fully
(all 152 Block Panchayats resolve at the body level). This is a hole in
KSMART's source data, not a bug in this join -- see :func:`division_gate`, which
lets a caller tolerate that specific, counted shortfall rather than failing the
whole build on a gap that will not close.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from geo.build.attributes import (
    JoinedFeature,
    JoinResult,
    Unmatched,
    load_local_bodies,
    load_wards,
    local_bodies_for_crosswalk,
    normalize_ward_no,
    ward_properties,
)
from geo.build.crosswalk import CrosswalkResult, LocalBody, build_crosswalk
from geo.build.emit import emit_block_panchayat_layer, emit_district_panchayat_layer
from geo.build.stitch import StitchResult, stitch_layer
from geo.config import Paths

#: Where a division-tier feature (``kerala_bp``/``kerala_dp``/either membership
#: layer) carries the *body*'s own district and name -- see the module docstring's
#: naming-trap warning. ``Localbody`` is right for all four layers; nothing in
#: this module should ever read a body's name from anywhere else.
BODY_DISTRICT_FIELD: Final = "District"
BODY_NAME_FIELD: Final = "Localbody"

#: Where a division-tier feature carries its own division number. Shared across
#: all four layers -- in the two membership layers this is the *coarser* tier's
#: division number (see the module docstring), which is exactly what makes them
#: usable as independent evidence for a coarser-tier division pairing.
DIVISION_NO_FIELD: Final = "Ward No"


@dataclass(frozen=True, slots=True)
class TierConfig:
    """Everything that differs between the BP and DP tiers, gathered in one
    place so the join logic below is written once and parametrised, not
    duplicated per tier with the one different field name silently drifting."""

    lb_type: str
    """Our ``lb_type`` string, as it appears in ``local_bodies_2025.csv`` and
    ``wards_2025.csv``."""
    stitch_layer: str
    """The ``geo.build.stitch`` layer holding this tier's division geometry."""
    membership_layer: str
    """The ``geo.build.stitch`` layer giving independent per-division evidence
    (see the module docstring)."""
    division_name_field: str
    """The KSMART property holding *this tier's* division name -- ``"Block
    Panc"`` for BP, ``"Ward Name"`` for DP. Never the body's name; see
    :data:`BODY_NAME_FIELD`."""
    layer_name: str
    """Matches a key in ``geo.build.emit.LAYER_FILENAMES``."""


BLOCK_PANCHAYAT: Final = TierConfig(
    lb_type="Block Panchayat",
    stitch_layer="kerala_bp",
    membership_layer="kerala_bp_with_lsgd",
    division_name_field="Block Panc",
    layer_name="kerala_bp",
)

DISTRICT_PANCHAYAT: Final = TierConfig(
    lb_type="District Panchayat",
    stitch_layer="kerala_dp",
    membership_layer="kerala_dp_with_block",
    division_name_field="Ward Name",
    layer_name="kerala_dp",
)

#: Body counts to gate the crosswalk against -- 152 Block Panchayats, 14 District
#: Panchayats, per ``local_bodies_2025.csv``.
EXPECTED_BODY_COUNTS: Final[dict[str, int]] = {
    "Block Panchayat": 152,
    "District Panchayat": 14,
}


# ---------------------------------------------------------------------------
# Body-level crosswalk
# ---------------------------------------------------------------------------


def local_bodies_for_tier(
    lb_rows: Iterable[Mapping[str, str]], ward_rows: Iterable[Mapping[str, str]], lb_type: str
) -> list[LocalBody]:
    """Our side of the body-level crosswalk: the ``lb_type`` rows only.

    Thin wrapper around ``attributes.local_bodies_for_crosswalk`` -- that
    function already builds a ``LocalBody`` per row with its ward (here:
    division) names attached, grouped by ``lb_code``; restricting both inputs
    to one ``lb_type`` first is all a BP/DP body list needs on top of it.
    """
    lbs = [r for r in lb_rows if r.get("lb_type") == lb_type]
    wards = [r for r in ward_rows if r.get("lb_type") == lb_type]
    return local_bodies_for_crosswalk(lbs, wards)


def theirs_bodies_from_stitch(stitch: StitchResult, config: TierConfig) -> list[LocalBody]:
    """KSMART's side of the body-level crosswalk, reconstructed from a division
    layer that has no body-level feature of its own.

    Every stitched division row repeats its body's ``(District, Localbody)`` --
    grouping by that pair and collecting each group's division names (read from
    ``config.division_name_field``, *not* ``BODY_NAME_FIELD`` -- the naming trap
    the module docstring warns about) reconstructs exactly the ``LocalBody``
    shape ``build_crosswalk`` verifies pairings against. The synthetic
    ``code`` is never compared to anything outside this module -- it exists only
    so ``build_crosswalk``'s internals have something unique to key on.
    """
    grouped: dict[tuple[str, str], set[str]] = defaultdict(set)
    for feature in stitch.features.values():
        district = feature.properties.get(BODY_DISTRICT_FIELD)
        name = feature.properties.get(BODY_NAME_FIELD)
        if district is None or name is None:
            continue
        key = (str(district), str(name))
        division_name = feature.properties.get(config.division_name_field)
        grouped.setdefault(key, set())
        if division_name is not None:
            grouped[key].add(str(division_name))

    return [
        LocalBody(
            code=f"{district}::{name}",
            name=name,
            lb_type=config.lb_type,
            district=district,
            ward_names=tuple(sorted(divisions)),
        )
        for (district, name), divisions in grouped.items()
    ]


def build_tier_body_crosswalk(
    lb_rows: Iterable[Mapping[str, str]],
    ward_rows: Iterable[Mapping[str, str]],
    stitch: StitchResult,
    config: TierConfig,
    *,
    overrides: Mapping[str, str] | None = None,
) -> CrosswalkResult:
    """Pair our BP or DP local bodies to KSMART's, reusing ``build_crosswalk``
    wholesale -- see the module docstring for why DP needs no fuzzy matching on
    top of it (the one-per-district rule already resolves all 14) while BP does
    (152 bodies, real transliteration variance)."""
    ours = local_bodies_for_tier(lb_rows, ward_rows, config.lb_type)
    theirs = theirs_bodies_from_stitch(stitch, config)
    return build_crosswalk(ours, theirs, overrides=overrides)


# ---------------------------------------------------------------------------
# Division-level join
# ---------------------------------------------------------------------------


def _membership_division_keys(membership_stitch: StitchResult) -> set[tuple[str, str, str]]:
    """``(district, body name, division number)`` triples independently observed
    in a tier-membership layer -- the corroborating signal ``join_division_layer``
    checks each division pairing against. See the module docstring's
    verification section for why this, and not a ward-name comparison, is the
    available independent evidence at division granularity."""
    keys: set[tuple[str, str, str]] = set()
    for feature in membership_stitch.features.values():
        district = feature.properties.get(BODY_DISTRICT_FIELD)
        name = feature.properties.get(BODY_NAME_FIELD)
        division_no = feature.properties.get(DIVISION_NO_FIELD)
        if district is None or name is None or division_no is None:
            continue
        keys.add((str(district), str(name), normalize_ward_no(division_no)))
    return keys


def join_division_layer(
    stitch: StitchResult,
    membership_stitch: StitchResult | None,
    crosswalk: CrosswalkResult,
    division_rows: Iterable[Mapping[str, str]],
    config: TierConfig,
) -> JoinResult:
    """Join ``wards_2025.csv``'s BP/DP division rows onto stitched division
    geometry, two hops just like ``attributes.join_ward_layer``: KSMART
    ``(District, Localbody)`` -> our ``lb_code`` (via the body-level crosswalk
    resolved above) -> ``(lb_code, ward_no)`` -> the CSV row.

    ``membership_stitch`` may be ``None`` (that cache not fetched, or a test with
    no need for it) -- every division still joins, only every
    ``division_verified`` property comes back ``False``, per
    ``Match.verified``'s absence-of-evidence-is-not-contradiction rule: a
    division is never rejected for lack of evidence, only flagged.

    A KSMART division's own ``Ward No`` is not always unique within a body: two
    ``kerala_dp`` divisions in Palakkad's District Panchayat both carry ``Ward
    No`` 30 (``Chalavara`` and ``Sreekrishnapuram`` -- see
    ``stitch.IDENTITY_FIELDS``'s note on why ``kerala_dp`` is keyed on name as
    well as number for stitching). Our own CSV has exactly one row per
    ``(lb_code, ward_no)``, so at most one KSMART division can claim it; whichever
    is encountered second is reported as an unmatched geometry naming the
    collision explicitly, rather than silently duplicating the first division's
    election result onto a second, different polygon.
    """
    theirs_to_ours = {(m.theirs.district, m.theirs.name): m.ours for m in crosswalk.matches}
    rows_by_key: dict[tuple[str, str], dict[str, str]] = {}
    for row in division_rows:
        key = (row.get("lb_code", ""), normalize_ward_no(row.get("ward_no", "")))
        rows_by_key[key] = dict(row)

    verified_keys = (
        _membership_division_keys(membership_stitch) if membership_stitch is not None else set()
    )

    unmatched: list[Unmatched] = []
    features: list[JoinedFeature] = []
    claimed: set[tuple[str, str]] = set()

    for feature in stitch.features.values():
        district = feature.properties.get(BODY_DISTRICT_FIELD)
        name = feature.properties.get(BODY_NAME_FIELD)
        ours = (
            theirs_to_ours.get((str(district), str(name)))
            if district is not None and name is not None
            else None
        )
        if ours is None:
            unmatched.append(
                Unmatched(
                    kind="geometry",
                    district=str(district) if district is not None else "<unknown>",
                    identity=str(feature.key),
                    reason=f"KSMART body {name!r} in {district!r} not in the resolved crosswalk",
                )
            )
            continue

        ward_no = normalize_ward_no(feature.properties.get(DIVISION_NO_FIELD, ""))
        row_key = (ours.code, ward_no)

        if row_key in claimed:
            unmatched.append(
                Unmatched(
                    kind="geometry",
                    district=ours.district,
                    identity=str(feature.key),
                    reason=(
                        f"{row_key} already claimed by another KSMART division "
                        "sharing this ward number"
                    ),
                )
            )
            continue

        row = rows_by_key.get(row_key)
        if row is None:
            unmatched.append(
                Unmatched(
                    kind="geometry",
                    district=ours.district,
                    identity=str(feature.key),
                    reason=f"no CSV division row for {row_key}",
                )
            )
            continue

        claimed.add(row_key)
        properties = ward_properties(row)
        properties["division_verified"] = (str(district), str(name), ward_no) in verified_keys
        features.append(JoinedFeature(geometry=feature.geometry, properties=properties))

    for row_key, row in rows_by_key.items():
        if row_key in claimed:
            continue
        unmatched.append(
            Unmatched(
                kind="row",
                district=row.get("district_name", "<unknown>"),
                identity=str(row_key),
                reason="no stitched division geometry claimed this ward",
            )
        )

    return JoinResult(layer=config.layer_name, features=features, unmatched=unmatched)


def division_gate(
    result: JoinResult,
    *,
    tolerate_missing_geometry: int = 0,
    tolerate_extra_geometry: int = 0,
) -> list[str]:
    """Like ``JoinResult.gate``, but a caller may tolerate a known, *counted*
    number of mismatches in either direction rather than failing the whole build.

    Both tolerances default to zero and both must be justified by something
    actually verified, never rounded up to a comfortable number. A counted
    allowance still fails when the count grows; a blanket one hides the next
    regression behind the last known defect.

    Two are known today, and they are different in kind:

    * ``kerala_bp`` is short 15 of our 2,267 BP divisions -- a real gap in
      KSMART's source that will not close on a re-fetch.
    * ``kerala_dp`` carries one *duplicate*: two Palakkad divisions share
      ``Ward No 30``. One claims the row and the other cannot, so the same defect
      shows up once in each direction.
    """
    problems: list[str] = []
    geoms = result.unmatched_geometries
    rows = result.unmatched_rows
    if len(geoms) > tolerate_extra_geometry:
        by_district = Counter(u.district for u in geoms)
        problems.append(
            f"{len(geoms)} stitched {result.layer} feature(s) had no matching CSV "
            f"row (tolerating {tolerate_extra_geometry}), by district: {dict(by_district)}"
        )
    if len(rows) > tolerate_missing_geometry:
        by_district = Counter(u.district for u in rows)
        problems.append(
            f"{len(rows)} CSV row(s) for {result.layer} had no matching geometry "
            f"(tolerating {tolerate_missing_geometry} known KSMART gap(s)), by "
            f"district: {dict(by_district)}"
        )
    return problems


# ---------------------------------------------------------------------------
# End-to-end per tier: stitch -> pair -> join -> emit
# ---------------------------------------------------------------------------

_EMIT_FUNCTIONS: Final = {
    "kerala_bp": emit_block_panchayat_layer,
    "kerala_dp": emit_district_panchayat_layer,
}


def build_and_emit_tier_layer(
    paths: Paths,
    config: TierConfig,
    *,
    year: str = "2025",
    fetched: str,
    built: str,
    overrides: Mapping[str, str] | None = None,
) -> tuple[CrosswalkResult, JoinResult]:
    """Run one tier (BP or DP) end to end: stitch its division and membership
    layers from the cache, pair bodies, join divisions, and emit the GeoJSON via
    the existing ``geo.build.emit`` hook for that layer.

    Returns the body crosswalk and the division join so a caller can gate on
    both (``crosswalk.gate(expected=...)`` and :func:`division_gate`) before
    trusting what was written -- the write itself does not gate, matching
    ``emit.write_feature_collection``'s documented contract that gating is the
    caller's job.
    """
    lb_rows = load_local_bodies(paths, year)
    ward_rows = load_wards(paths, year)

    stitch = stitch_layer(paths, config.stitch_layer)
    membership = stitch_layer(paths, config.membership_layer)

    crosswalk = build_tier_body_crosswalk(lb_rows, ward_rows, stitch, config, overrides=overrides)
    division_rows = [r for r in ward_rows if r.get("lb_type") == config.lb_type]
    result = join_division_layer(stitch, membership, crosswalk, division_rows, config)

    emit_fn = _EMIT_FUNCTIONS[config.layer_name]
    emit_fn(paths, result, fetched=fetched, built=built)

    return crosswalk, result
