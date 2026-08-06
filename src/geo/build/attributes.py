"""Join election attributes onto stitched geometry.

Two joins happen here, both keyed through the Unit 5 crosswalk rather than directly:

* **Local bodies.** ``lb_kerala``'s identity key *is* KSMART's ``lb_code``, so a
  stitched feature's key looks up straight into
  ``{ksmart_lb_code: our_lb_code}`` (built once from ``CrosswalkResult.matches``) and
  from there into ``local_bodies_<year>.csv`` keyed on our ``lb_code``.
* **Wards.** ``wb_kerala``'s identity key is the KSMART-internal ``OBJECTID``, which
  means nothing on our side. The feature carries KSMART's own ``lb_code`` and
  ``Ward_No`` as ordinary properties, so the ward join is two hops: KSMART lb_code ->
  our lb_code (the same map as above) -> ``(lb_code, ward_no)`` into
  ``wards_<year>.csv``. Ward numbers are normalised to their integer text (``"01"``
  and ``1`` must compare equal) because neither side's formatting is a contract.

Both directions of a missing join are tracked and neither is dropped silently:
a geometry with no CSV row (the local body never resolved, or this exact ward
number never appeared in the CSV), and a CSV row with no geometry (KSMART is simply
missing that ward or that local body's polygon). The plan is explicit that either one
is a hole in the map and must fail the gate, not vanish into an aggregate count -- so
both are collected as :class:`Unmatched` records, one per district, and
:meth:`JoinResult.gate` turns them into build-failing reasons.

Two edge cases in the CSV rows themselves need explicit handling, not incidental
correctness:

* **Uncontested wards have no runner-up.** ``runnerup_votes`` is blank, and
  ``winner_votes - 0`` is a *wrong number*, not a missing one -- it invents a margin
  that never happened. :func:`compute_margin` returns ``None`` for a blank
  runner-up, which serializes to JSON ``null``, distinguishable from a real
  zero-vote margin (which does happen in small wards).
* **A blank Malayalam name must serialize as ``null``, not ``""``.** An empty string
  is a value a frontend has to special-case; ``null`` is a value it can fall back on
  for free. :func:`clean_optional_text` makes that translation the one place it
  happens, rather than leaving every call site to remember it.
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from shapely.geometry.base import BaseGeometry

from geo.build.crosswalk import CrosswalkResult, LocalBody
from geo.build.stitch import DISTRICT_FIELD, StitchResult
from geo.config import Paths

#: Properties KSMART's ``wb_kerala`` carries per the plan's recon table -- the KSMART
#: local body code and the ward number, both needed to hop across the crosswalk to
#: our own keys. Not identity fields (``OBJECTID`` is), just ordinary attributes.
WB_LB_CODE_PROPERTY: Final = "lb_code"
WB_WARD_NO_PROPERTY: Final = "Ward_No"


class JoinError(Exception):
    """Attribute data could not be loaded or joined at all -- distinct from a gated
    per-row mismatch, which is reported rather than raised."""


@dataclass(frozen=True, slots=True)
class Unmatched:
    """One side of a join with nothing on the other side.

    ``kind`` is ``"geometry"`` (a stitched polygon with no CSV row) or ``"row"`` (a
    CSV row with no stitched polygon) -- both are holes in the map, reported by
    district so a build failure names where to look rather than only how many.
    """

    kind: str
    district: str
    identity: str
    reason: str


@dataclass(slots=True)
class JoinedFeature:
    """One output feature: whole geometry plus the properties a choropleth needs."""

    geometry: BaseGeometry
    properties: dict[str, Any]


@dataclass(slots=True)
class JoinResult:
    """Everything one layer's attribute join produced."""

    layer: str
    features: list[JoinedFeature] = field(default_factory=list)
    unmatched: list[Unmatched] = field(default_factory=list)

    @property
    def unmatched_geometries(self) -> list[Unmatched]:
        return [u for u in self.unmatched if u.kind == "geometry"]

    @property
    def unmatched_rows(self) -> list[Unmatched]:
        return [u for u in self.unmatched if u.kind == "row"]

    def gate(self) -> list[str]:
        """Reasons this join must not be trusted. Empty means it passed.

        Mirrors ``CrosswalkResult.gate``'s shape: a build-facing summary that names
        the districts at fault rather than only a count, since "23,573 wards, some
        missing" is not actionable and "3 unmatched in Idukki" is.
        """
        problems: list[str] = []
        geoms = self.unmatched_geometries
        rows = self.unmatched_rows
        if geoms:
            by_district = Counter(u.district for u in geoms)
            problems.append(
                f"{len(geoms)} stitched {self.layer} feature(s) had no matching CSV "
                f"row, by district: {dict(by_district)}"
            )
        if rows:
            by_district = Counter(u.district for u in rows)
            problems.append(
                f"{len(rows)} CSV row(s) for {self.layer} had no matching geometry, "
                f"by district: {dict(by_district)}"
            )
        return problems


def load_csv_rows(path: Path) -> list[dict[str, str]]:
    """Read one ``data/final`` CSV as plain string dicts.

    ``utf-8-sig`` because ``data_merge`` writes these with a BOM; a plain ``utf-8``
    open leaves a ``\\ufeff`` glued onto the first column's header, and every lookup
    on that column then fails for a reason that looks like a missing key rather than
    an encoding mismatch.
    """
    if not path.exists():
        raise JoinError(f"expected input CSV not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def wards_csv_path(paths: Paths, year: str) -> Path:
    return paths.elections / year / f"wards_{year}.csv"


def local_bodies_csv_path(paths: Paths, year: str) -> Path:
    return paths.elections / year / f"local_bodies_{year}.csv"


def load_wards(paths: Paths, year: str) -> list[dict[str, str]]:
    return load_csv_rows(wards_csv_path(paths, year))


def load_local_bodies(paths: Paths, year: str) -> list[dict[str, str]]:
    return load_csv_rows(local_bodies_csv_path(paths, year))


def local_bodies_for_crosswalk(
    lb_rows: Iterable[Mapping[str, str]], ward_rows: Iterable[Mapping[str, str]]
) -> list[LocalBody]:
    """Turn our CSV rows into the ``LocalBody`` shape ``geo.build.crosswalk`` pairs.

    Ward names come from ``wards_<year>.csv``, grouped by ``lb_code`` -- the crosswalk
    needs them as the independent field that verifies a name pairing, per
    ``crosswalk``'s module docstring.
    """
    ward_names: dict[str, list[str]] = defaultdict(list)
    for row in ward_rows:
        lb_code = row.get("lb_code", "")
        name = (row.get("ward_name") or "").strip()
        if lb_code and name:
            ward_names[lb_code].append(name)

    bodies: list[LocalBody] = []
    for row in lb_rows:
        code = row["lb_code"]
        bodies.append(
            LocalBody(
                code=code,
                name=row["lb_name"],
                lb_type=row["lb_type"],
                district=row["district_name"],
                ward_names=tuple(ward_names.get(code, ())),
            )
        )
    return bodies


def _ksmart_to_ours(crosswalk: CrosswalkResult) -> dict[str, str]:
    """``{ksmart_lb_code: our_lb_code}`` from resolved matches only.

    Deliberately not consulting ``unresolved``/``rejected`` -- an unresolved or
    rejected local body has no trustworthy code on either side, so any geometry or
    row that only reaches this dict through one of those local bodies correctly
    falls through as unmatched.
    """
    return {m.theirs.code: m.ours.code for m in crosswalk.matches}


def normalize_ward_no(value: Any) -> str:
    """Canonical text form of a ward number, so ``"01"``, ``1`` and ``" 1 "`` compare
    equal regardless of which side's formatting produced them."""
    text = str(value).strip()
    try:
        return str(int(text))
    except ValueError:
        return text


def compute_margin(winner_votes: str, runnerup_votes: str) -> int | None:
    """Winner minus runner-up, or ``None`` for an uncontested ward.

    A blank ``runnerup_votes`` means there was no runner-up, not a runner-up with
    zero votes -- treating it as zero would report a fabricated margin for exactly
    the wards where "margin" is meaningless. See the module docstring.
    """
    runnerup_text = (runnerup_votes or "").strip()
    if not runnerup_text:
        return None
    winner_text = (winner_votes or "").strip()
    if not winner_text:
        return None
    return int(winner_text) - int(runnerup_text)


def clean_optional_text(value: str | None) -> str | None:
    """Blank becomes ``None`` (JSON ``null``); anything else passes through stripped.

    A frontend can branch on ``null`` for free; an empty string is a value it has to
    remember to special-case. See the module docstring.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _int_or_none(value: str | None) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _district_of(properties: Mapping[str, Any], layer: str) -> str:
    field_name = DISTRICT_FIELD.get(layer, "District")
    value = properties.get(field_name)
    return str(value) if value is not None else "<unknown>"


def ward_properties(row: Mapping[str, str]) -> dict[str, Any]:
    """The properties one ward feature carries, per the plan's Unit 6 approach."""
    return {
        "ward_code": row.get("ward_code"),
        "lb_code": row.get("lb_code"),
        "lb_name": row.get("lb_name"),
        "lb_type": row.get("lb_type"),
        "district_name": row.get("district_name"),
        "ward_no": row.get("ward_no"),
        "ward_name": clean_optional_text(row.get("ward_name")),
        "ward_name_mal": clean_optional_text(row.get("ward_name_mal")),
        "reservation": clean_optional_text(row.get("reservation")),
        "n_candidates": _int_or_none(row.get("n_candidates")),
        "valid_votes": _int_or_none(row.get("valid_votes")),
        "invalid_votes": _int_or_none(row.get("invalid_votes")),
        "winner_name": clean_optional_text(row.get("winner_name")),
        "winner_party": clean_optional_text(row.get("winner_party")),
        "winner_party_group": clean_optional_text(row.get("winner_party_group")),
        "winner_votes": _int_or_none(row.get("winner_votes")),
        "runnerup_name": clean_optional_text(row.get("runnerup_name")),
        "runnerup_votes": _int_or_none(row.get("runnerup_votes")),
        "margin": compute_margin(row.get("winner_votes", ""), row.get("runnerup_votes", "")),
        "lb_ruling_front": clean_optional_text(row.get("lb_ruling_front")),
        "lb_control_type": clean_optional_text(row.get("lb_control_type")),
    }


def local_body_properties(row: Mapping[str, str]) -> dict[str, Any]:
    """The properties one local-body feature carries, per the plan's Unit 6 approach."""
    return {
        "lb_code": row.get("lb_code"),
        "lb_name": row.get("lb_name"),
        "lb_name_mal": clean_optional_text(row.get("lb_name_mal")),
        "lb_type": row.get("lb_type"),
        "district_name": row.get("district_name"),
        "total_wards": _int_or_none(row.get("total_wards")),
        "lb_seats_udf": _int_or_none(row.get("lb_seats_udf")),
        "lb_seats_ldf": _int_or_none(row.get("lb_seats_ldf")),
        "lb_seats_nda": _int_or_none(row.get("lb_seats_nda")),
        "lb_seats_oth": _int_or_none(row.get("lb_seats_oth")),
        "lb_majority_threshold": _int_or_none(row.get("lb_majority_threshold")),
        "lb_largest_front": clean_optional_text(row.get("lb_largest_front")),
        "lb_largest_front_seats": _int_or_none(row.get("lb_largest_front_seats")),
        "lb_ruling_front": clean_optional_text(row.get("lb_ruling_front")),
        "lb_control_type": clean_optional_text(row.get("lb_control_type")),
        "lb_head_role": clean_optional_text(row.get("lb_head_role")),
        "lb_head_name": clean_optional_text(row.get("lb_head_name")),
        "lb_head_party_group": clean_optional_text(row.get("lb_head_party_group")),
        "lb_head_cross_front": clean_optional_text(row.get("lb_head_cross_front")),
    }


def join_ward_layer(
    stitch: StitchResult, crosswalk: CrosswalkResult, ward_rows: Iterable[Mapping[str, str]]
) -> JoinResult:
    """Join ``wards_<year>.csv`` rows onto a stitched ``wb_kerala`` result."""
    code_map = _ksmart_to_ours(crosswalk)
    rows_by_key: dict[tuple[str, str], dict[str, str]] = {}
    for row in ward_rows:
        key = (row.get("lb_code", ""), normalize_ward_no(row.get("ward_no", "")))
        rows_by_key[key] = dict(row)

    unmatched: list[Unmatched] = []
    features: list[JoinedFeature] = []
    claimed: set[tuple[str, str]] = set()

    for feature in stitch.features.values():
        district = _district_of(feature.properties, stitch.layer)
        ksmart_lb_code = feature.properties.get(WB_LB_CODE_PROPERTY)
        our_lb_code = code_map.get(ksmart_lb_code) if ksmart_lb_code is not None else None
        if our_lb_code is None:
            unmatched.append(
                Unmatched(
                    kind="geometry",
                    district=district,
                    identity=str(feature.key),
                    reason=f"local body {ksmart_lb_code!r} not in the resolved crosswalk",
                )
            )
            continue

        ward_no = normalize_ward_no(feature.properties.get(WB_WARD_NO_PROPERTY, ""))
        row_key = (our_lb_code, ward_no)
        row = rows_by_key.get(row_key)
        if row is None:
            unmatched.append(
                Unmatched(
                    kind="geometry",
                    district=district,
                    identity=str(feature.key),
                    reason=f"no CSV ward row for {row_key}",
                )
            )
            continue

        claimed.add(row_key)
        features.append(JoinedFeature(geometry=feature.geometry, properties=ward_properties(row)))

    for row_key, row in rows_by_key.items():
        if row_key in claimed:
            continue
        unmatched.append(
            Unmatched(
                kind="row",
                district=row.get("district_name", "<unknown>"),
                identity=str(row_key),
                reason="no stitched geometry claimed this ward",
            )
        )

    return JoinResult(layer=stitch.layer, features=features, unmatched=unmatched)


def join_local_body_layer(
    stitch: StitchResult, crosswalk: CrosswalkResult, lb_rows: Iterable[Mapping[str, str]]
) -> JoinResult:
    """Join ``local_bodies_<year>.csv`` rows onto a stitched ``lb_kerala`` result."""
    code_map = _ksmart_to_ours(crosswalk)
    rows_by_code = {row["lb_code"]: dict(row) for row in lb_rows}

    unmatched: list[Unmatched] = []
    features: list[JoinedFeature] = []
    claimed: set[str] = set()

    for feature in stitch.features.values():
        district = _district_of(feature.properties, stitch.layer)
        (ksmart_lb_code,) = feature.key
        our_lb_code = code_map.get(ksmart_lb_code)
        if our_lb_code is None:
            unmatched.append(
                Unmatched(
                    kind="geometry",
                    district=district,
                    identity=str(feature.key),
                    reason=f"local body {ksmart_lb_code!r} not in the resolved crosswalk",
                )
            )
            continue

        row = rows_by_code.get(our_lb_code)
        if row is None:
            unmatched.append(
                Unmatched(
                    kind="geometry",
                    district=district,
                    identity=str(feature.key),
                    reason=f"no CSV local-body row for {our_lb_code!r}",
                )
            )
            continue

        claimed.add(our_lb_code)
        features.append(
            JoinedFeature(geometry=feature.geometry, properties=local_body_properties(row))
        )

    for lb_code, row in rows_by_code.items():
        if lb_code in claimed:
            continue
        unmatched.append(
            Unmatched(
                kind="row",
                district=row.get("district_name", "<unknown>"),
                identity=lb_code,
                reason="no stitched geometry claimed this local body",
            )
        )

    return JoinResult(layer=stitch.layer, features=features, unmatched=unmatched)
