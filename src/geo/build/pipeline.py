"""The end-to-end build: cached tiles and release files in, map layers out.

This is the module the ``geo validate`` and ``geo build`` verbs drive, and the only
place that knows the *order* of the pipeline. Everything it calls -- stitch,
crosswalk, join, emit -- is independently testable and knows nothing about the others.

The two verbs share this code deliberately. ``validate`` runs every stage and every
gate and then throws the result away; ``build`` runs the identical thing and writes.
If they were separate code paths, a build could pass a check that a validate never
ran, which is the failure the split exists to prevent.

Cycle scope is 2015 onwards. See ``emit.EARLIER_CYCLE_FILENAMES`` for why 2010 is not
here: the OSM polygons post-date it by three delimitations.
"""

from __future__ import annotations

import collections
import csv
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from shapely.geometry import shape

from geo.build import emit as E
from geo.build import tier_crosswalk as T
from geo.build.attributes import (
    JoinResult,
    join_local_body_layer,
    join_ward_layer,
    load_local_bodies,
    load_wards,
    local_bodies_for_crosswalk,
)
from geo.build.crosswalk import (
    CROSSWALK_FILENAME,
    OVERRIDE_FILENAME,
    CrosswalkResult,
    LocalBody,
    build_crosswalk,
    load_overrides,
    write_crosswalk,
)
from geo.build.dissolve import (
    dissolve_block_panchayats,
    dissolve_district_panchayats,
    load_features,
)
from geo.build.osm_crosswalk import OVERRIDE_FILENAME as OSM_OVERRIDE_FILENAME
from geo.build.osm_crosswalk import (
    build_osm_crosswalk,
    district_by_lsgi_code,
    local_bodies_from_dissolved,
    local_bodies_from_osm_features,
)
from geo.build.osm_crosswalk import load_overrides as load_osm_overrides
from geo.build.stitch import stitch_layer
from geo.config import Paths

#: Hand-reviewed Block/District Panchayat body pairings, consulted before the name
#: cascade. Those tiers carry no code on either side, so a transliteration gap wide
#: enough to defeat fuzzy matching can only be closed by a human deciding once.
TIER_OVERRIDE_FILENAME: Final = "tier_body_overrides.csv"


def load_tier_overrides(path: Path) -> dict[str, str]:
    """``our lb_code -> KSMART body key``. Missing file is not an error."""
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return {
            row["lb_code"].strip(): row["ksmart_key"].strip()
            for row in csv.DictReader(fh)
            if row.get("lb_code") and row.get("ksmart_key")
        }


#: Defects in KSMART's own data, as ``layer -> (extra geometry, missing geometry)``.
#: Every number here was counted against the real cache, not estimated, and a build
#: fails the moment a count grows -- which is the point of writing them down rather
#: than loosening the gate.
#:
#: ``kerala_bp`` publishes 2,252 of our 2,267 Block Panchayat divisions; the 15 absent
#: ones are a gap in their source that a re-fetch will not close. ``kerala_dp``
#: publishes all 346 District Panchayat divisions but gives two Palakkad divisions the
#: same ``Ward No 30``; one claims the result row and the other cannot, so that single
#: duplicate surfaces once in each direction.
KNOWN_TIER_GAPS: dict[str, tuple[int, int]] = {
    "kerala_bp": (0, 15),
    "kerala_dp": (1, 1),
}

#: The cycles this pipeline builds, newest first.
CYCLES: tuple[str, ...] = ("2025", "2020", "2015")

#: The tiers KSMART's ``wb_kerala``/``lb_kerala`` cover. Block and District
#: Panchayats are *not* absent from the data -- they live in their own layers, and
#: feeding their rows into these joins would report all 2,613 of them as unmatched.
KSMART_TIERS: frozenset[str] = frozenset({"Grama Panchayat", "Municipality", "Corporation"})


@dataclass
class BuildReport:
    """What one run produced, and every reason it should not be trusted."""

    written: list[Path] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems

    def note(self, label: str, gate: Sequence[str]) -> None:
        self.problems.extend(f"{label}: {p}" for p in gate)


def ksmart_local_bodies(paths: Paths, year: str) -> tuple[CrosswalkResult, list[LocalBody]]:
    """Pair our local bodies to KSMART's, using ward names as the evidence.

    Ward names come from the stitched ``wb_kerala`` layer rather than from anything
    KSMART states about its own bodies, because that is what makes the check
    independent: a pairing that agrees on the body's name but not on its wards is
    wrong, and only an independently-sourced field can say so.
    """
    wb = stitch_layer(paths, "wb_kerala")
    lb = stitch_layer(paths, "lb_kerala")

    names: dict[str, list[str]] = collections.defaultdict(list)
    for feature in wb.features.values():
        code = feature.properties.get("lb_code")
        name = feature.properties.get("Ward Eng") or feature.properties.get("Ward_Name")
        if code and name:
            names[code].append(name)

    theirs = [
        LocalBody(
            code=props["lb_code"],
            name=props.get("name") or "",
            lb_type=props.get("Lsgd_Type") or "",
            district=props.get("DistName") or "",
            ward_names=tuple(names.get(props["lb_code"], ())),
        )
        for feature in lb.features.values()
        for props in [feature.properties]
        if props.get("lb_code")
    ]
    ours = [
        b
        for b in local_bodies_for_crosswalk(load_local_bodies(paths, year), load_wards(paths, year))
        if b.lb_type in KSMART_TIERS
    ]
    crosswalk = build_crosswalk(
        ours, theirs, overrides=load_overrides(paths.reference / OVERRIDE_FILENAME)
    )
    return crosswalk, ours


def build_2025(paths: Paths, report: BuildReport, *, fetched: str, built: str, write: bool) -> None:
    """The KSMART half: ward and local-body layers for the current delimitation."""
    crosswalk, ours = ksmart_local_bodies(paths, "2025")
    report.counts["2025_bodies_resolved"] = crosswalk.resolved_count
    report.note("2025 local-body crosswalk", crosswalk.gate(len(ours)))

    wb = stitch_layer(paths, "wb_kerala")
    lb = stitch_layer(paths, "lb_kerala")
    ward_rows = [r for r in load_wards(paths, "2025") if r["lb_type"] in KSMART_TIERS]
    lb_rows = [r for r in load_local_bodies(paths, "2025") if r["lb_type"] in KSMART_TIERS]

    wards = join_ward_layer(wb, crosswalk, ward_rows)
    bodies = join_local_body_layer(lb, crosswalk, lb_rows)
    report.counts["2025_wards"] = len(wards.features)
    report.counts["2025_local_bodies"] = len(bodies.features)

    if write:
        write_crosswalk(crosswalk, paths.reference / CROSSWALK_FILENAME)
        report.written.append(E.emit_ward_layer(paths, wards, fetched=fetched, built=built))
        report.written.append(E.emit_local_body_layer(paths, bodies, fetched=fetched, built=built))


def build_tiers(paths: Paths, report: BuildReport, *, fetched: str, built: str, write: bool) -> None:
    """Block and District Panchayat divisions -- 2025 only.

    These layers carry no ``lb_code``, so they pair on names and are verified against
    the tier-membership layers. KSMART's ``kerala_bp`` is genuinely missing divisions
    our results have, so the gate tolerates a *counted* shortfall rather than a
    blanket pass -- an uncounted allowance would hide a regression behind a known gap.
    """
    overrides = load_tier_overrides(paths.reference / TIER_OVERRIDE_FILENAME)
    for config in (T.BLOCK_PANCHAYAT, T.DISTRICT_PANCHAYAT):
        try:
            crosswalk, joined = T.build_and_emit_tier_layer(
                paths, config, fetched=fetched, built=built, overrides=overrides
            )
        except Exception as exc:  # a missing layer must not take the whole build down
            report.problems.append(f"{config.lb_type}: {exc}")
            continue
        report.counts[f"2025_{config.lb_type.replace(' ', '_').lower()}_divisions"] = len(
            joined.features
        )
        extra, missing = KNOWN_TIER_GAPS.get(config.stitch_layer, (0, 0))
        report.note(
            config.lb_type,
            T.division_gate(
                joined,
                tolerate_missing_geometry=missing,
                tolerate_extra_geometry=extra,
            ),
        )


def build_earlier(paths: Paths, report: BuildReport, *, fetched: str, built: str, write: bool) -> None:
    """The OSM half: local-body layers for 2015 and 2020.

    Both cycles share one physical polygon set -- the November 2020 snapshot -- and
    the provenance on every emitted layer says so. There is no per-cycle local-body
    delimitation available at all, and a filename like ``local_bodies_2015.geojson``
    would otherwise imply a boundary that was surveyed in 2015.
    """
    features = load_features(paths.releases / "kerala_lsg_data.geojson")
    blocks = dissolve_block_panchayats(features)
    districts = dissolve_district_panchayats(features)
    report.counts["osm_block_panchayats"] = len(blocks.bodies)
    report.counts["osm_district_panchayats"] = len(districts.bodies)
    for label, result in (("block", blocks), ("district", districts)):
        if result.missing:
            report.problems.append(
                f"{label} dissolve: {len(result.missing)} gram panchayats have no group code"
            )

    by_code = district_by_lsgi_code(features)
    theirs = (
        local_bodies_from_osm_features(features)
        + local_bodies_from_dissolved(
            blocks.bodies, lb_type="Block Panchayat", district_by_code=by_code
        )
        + local_bodies_from_dissolved(
            districts.bodies, lb_type="District Panchayat", district_by_code=by_code
        )
    )
    geometries = {
        str(f["properties"]["LSGI_Code"]): shape(f["geometry"])
        for f in features
        if f["properties"].get("LSGI_Code")
    }
    for body in list(blocks.bodies) + list(districts.bodies):
        geometries[body.qid] = body.geometry

    hand = load_osm_overrides(paths.reference / OSM_OVERRIDE_FILENAME)
    for year in ("2015", "2020"):
        rows = load_local_bodies(paths, year)
        ours = local_bodies_for_crosswalk(rows, load_wards(paths, year))
        crosswalk = build_osm_crosswalk(ours, theirs, overrides=hand)
        report.counts[f"{year}_bodies_resolved"] = crosswalk.resolved_count
        report.note(f"{year} local-body crosswalk", crosswalk.gate(len(ours)))

        joined = E.join_osm_local_bodies(geometries, crosswalk, rows)
        report.counts[f"{year}_local_bodies"] = len(joined.features)
        report.note(f"{year} join", joined.gate())
        if write:
            report.written.append(
                E.emit_earlier_cycle_local_body_layer(
                    paths, year, joined, fetched=fetched, built=built
                )
            )


def run(
    paths: Paths, *, fetched: str, built: str, write: bool = True, tiers: bool = True
) -> BuildReport:
    """Build every layer, gating each stage. ``write=False`` is the ``validate`` verb."""
    report = BuildReport()
    build_2025(paths, report, fetched=fetched, built=built, write=write)
    if tiers and write:
        # The tier layers emit as they build; there is no in-memory-only mode for
        # them yet, so validate skips rather than silently writing.
        build_tiers(paths, report, fetched=fetched, built=built, write=write)
    build_earlier(paths, report, fetched=fetched, built=built, write=write)
    return report
