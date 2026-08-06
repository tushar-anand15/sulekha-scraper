"""Pairing opendatakerala's OSM local bodies to ``data_merge``'s ``lb_code``.

Unit 7 turned the 1,034 OSM ``admin_level=8`` polygons into all 1,200 Kerala
local bodies: 1,033 usable Grama Panchayats/Municipalities/Corporations read
directly (one ``LSGI_Code`` collides -- see :data:`AMBIGUOUS_OSM_CODES`, and
one is malformed -- see the override file), plus 152 Block Panchayats and 14
District Panchayats dissolved out of the GPs by :mod:`geo.build.dissolve`.
This module resolves all of them to our six-character ``lb_code``.

The KSMART crosswalk (:mod:`geo.build.crosswalk`) had to solve this with names
alone, because KSMART's seven-character code has no derivable relationship to
ours. OSM's ``LSGI_Code`` is different: measured against
``local_bodies_2020.csv``, **1,031 of 1,033** direct bodies (99.8%) carry an
``LSGI_Code`` that is *literally* our ``lb_code``, unmodified. That is worth
exploiting before falling back to the name cascade, so
:func:`exact_code_matches` finds every such pairing first, and only the
residual goes through :func:`geo.build.crosswalk.build_crosswalk`'s
name-and-ward-verification machinery -- reused wholesale via the ``overrides``
parameter it already exposes, since a forced code pairing and a hand-reviewed
override are the same shape: "trust this pairing without going through the
name cascade."

Two OSM data-quality quirks surfaced by measuring this, both fixed here rather
than in :mod:`geo.build.dissolve` (which stays a pure GP-dissolve, uninterested
in crosswalk district labels):

* **Kasaragod is spelled "Kasaragod" in the OSM release and "KASARGOD" in
  ours.** A single-letter spelling difference is invisible to
  ``normalize()`` --- it strips punctuation and case, not vowels --- so every
  Kasaragod body would silently fail the ``(district, lb_type)`` pool lookup
  without :data:`DISTRICT_ALIASES`.
* **A dissolved Block/District Panchayat's ``district`` is read from its
  *first* member Grama Panchayat's own ``District`` property** (see
  ``DissolvedBody`` in :mod:`geo.build.dissolve`), and that property is wrong
  on a handful of GPs -- e.g. one of Devikulam Block Panchayat's member GPs is
  tagged ``District: Ernakulam`` even though the block itself, and every other
  member, is in Idukki. :func:`majority_district` re-derives the district as
  the most common value across *all* members rather than trusting the first,
  which self-heals every case measured except where every member happens to
  share the same wrong tag (none observed).

Even after both fixes, four bodies out of 1,199 need a hand override because
their OSM name diverges from ours by more than the fuzzy cascade's threshold
tolerates (``Chovvannoor``/``Chowannur``, ``Areekkode``/``Areacode``,
``Thoonerry``/``Tuneri``, and one Grama Panchayat whose OSM ``name`` field
carries its own English-suffixed alias baked in, colliding with a malformed
``LSGI_Code``). See ``data/reference/geo/osm_lb_overrides.csv``. With those
four hand-reviewed, this module resolves all 1,199 rows in
``local_bodies_2020.csv``.

This module must never import an HTTP client at any depth --
``tests/geo/test_no_network.py`` walks every file under ``geo/build``.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Final

from data_merge.transform.matching import normalize
from geo.build.crosswalk import CrosswalkResult, LocalBody, build_crosswalk
from geo.build.dissolve import DissolvedBody

#: OSM ``local_auth`` values that map onto our own ``lb_type`` strings. Only
#: these three tiers are read directly from the release; Block and District
#: Panchayats are dissolved first (see :mod:`geo.build.dissolve``) and enter
#: this module as :class:`~geo.build.dissolve.DissolvedBody` instead.
TIER_MAP: Final[dict[str, str]] = {
    "gram_panchayat": "Grama Panchayat",
    "municipality": "Municipality",
    "municipal_corporation": "Corporation",
}

#: Spelling divergences between the OSM release's ``District`` property and
#: ``data_merge``'s ``district_name`` column, keyed by the *normalized* OSM
#: spelling. Measured, not assumed: this is the only district-name mismatch
#: found across all 1,199 bodies. A body whose district cannot be resolved to
#: ours falls out of the pairing pool entirely and never reaches the ward- or
#: name-level checks, so this fix has to happen before pooling, not after.
DISTRICT_ALIASES: Final[dict[str, str]] = {
    "KASARAGOD": "KASARGOD",
}

#: Strips the tier suffix a dissolved body's name inherits from its source
#: field (``BlockName`` / ``DP_Name``), e.g. ``"Manjeswaram Block Panchayat"``
#: -> ``"Manjeswaram"``. Case-insensitive and tolerant of the alternate
#: ``"Panchayath"`` spelling the release uses for a handful of blocks
#: (``"Thoonerry Block Panchayath"``) -- both forms were observed, and a
#: suffix strip that only matched one would leave the other's name cluttered
#: with eleven characters of noise the fuzzy cascade cannot see past.
_SUFFIX_RE: Final = re.compile(r"\s+(Block|District)\s+Panchayath?\s*$", re.IGNORECASE)

OVERRIDE_FILENAME: Final = "osm_lb_overrides.csv"
CROSSWALK_FILENAME: Final = "osm_lb_crosswalk.csv"

CROSSWALK_COLUMNS: Final = (
    "lb_code",
    "osm_code",
    "lb_type",
    "district_name",
    "lb_name",
    "osm_name",
    "match_method",
    "ward_agreement",
)


def strip_tier_suffix(name: str) -> str:
    """``"Manjeswaram Block Panchayat"`` -> ``"Manjeswaram"``.

    Only Block/District Panchayat names carry this suffix -- it comes from
    ``BlockName``/``DP_Name``, which the source reuses as a human label, not
    an identifier. Grama Panchayat, Municipality and Corporation names are
    passed through unchanged (the regex only matches a trailing "Block
    Panchayat[h]" or "District Panchayat[h]", never any other suffix).
    """
    return _SUFFIX_RE.sub("", name).strip()


def resolve_district(district: str | None) -> str:
    """Apply :data:`DISTRICT_ALIASES` to one OSM ``District`` value."""
    if not district:
        return ""
    key = normalize(district)
    return DISTRICT_ALIASES.get(key, district)


def district_by_lsgi_code(features: Sequence[Mapping[str, object]]) -> dict[str, str]:
    """``{LSGI_Code: District}`` straight from the raw release features.

    The only reason this exists separately from :mod:`geo.build.dissolve` is
    :func:`majority_district`, which needs every member GP's *own* district
    tag, not just the one the dissolve happened to keep.
    """
    out: dict[str, str] = {}
    for feature in features:
        props = feature.get("properties", {})  # type: ignore[assignment]
        code = props.get("LSGI_Code")  # type: ignore[union-attr]
        district = props.get("District")  # type: ignore[union-attr]
        if code and district:
            out[str(code)] = str(district)
    return out


def majority_district(body: DissolvedBody, district_by_code: Mapping[str, str]) -> str:
    """The most common ``District`` tag among a dissolved body's own members.

    ``DissolvedBody.district`` (see :mod:`geo.build.dissolve``) is read from
    just the first member's properties, and that property is occasionally
    wrong on one member while every other member (and the block's actual
    location) agrees on the real district. A majority vote across *all*
    members self-heals exactly that case. Falls back to the dissolve's own
    ``district`` if none of the member codes are known (should not happen in
    practice, since every member's code comes from the same feature set this
    function is built from).
    """
    districts = [district_by_code[code] for code in body.member_codes if code in district_by_code]
    if not districts:
        return body.district or ""
    return Counter(districts).most_common(1)[0][0]


def local_bodies_from_osm_features(
    features: Sequence[Mapping[str, object]],
) -> list[LocalBody]:
    """The 1,033 usable direct bodies (Grama Panchayat/Municipality/Corporation).

    Skips whatever ``local_auth`` value is not in :data:`TIER_MAP` --
    ``admin_level=8`` features the release does not classify as one of the
    three tiers this crosswalk expects, none observed in the current release
    but not assumed absent either.
    """
    bodies: list[LocalBody] = []
    for feature in features:
        props = feature.get("properties", {})  # type: ignore[assignment]
        tier = TIER_MAP.get(str(props.get("local_auth", "")))  # type: ignore[union-attr]
        if tier is None:
            continue
        code = props.get("LSGI_Code")  # type: ignore[union-attr]
        name = props.get("name")  # type: ignore[union-attr]
        district = props.get("District")  # type: ignore[union-attr]
        if not code or not name:
            continue
        bodies.append(
            LocalBody(
                code=str(code),
                name=str(name),
                lb_type=tier,
                district=resolve_district(str(district) if district else None),
            )
        )
    return bodies


def local_bodies_from_dissolved(
    bodies: Iterable[DissolvedBody],
    *,
    lb_type: str,
    district_by_code: Mapping[str, str],
) -> list[LocalBody]:
    """The 152 Block Panchayats or 14 District Panchayats, ready to crosswalk.

    ``lb_type`` is supplied by the caller (``"Block Panchayat"`` or
    ``"District Panchayat"``) rather than inferred, since a
    :class:`~geo.build.dissolve.DissolvedBody` does not itself know which
    dissolve produced it.
    """
    out: list[LocalBody] = []
    for body in bodies:
        if body.name is None:
            continue
        district = majority_district(body, district_by_code)
        out.append(
            LocalBody(
                code=body.qid,
                name=strip_tier_suffix(body.name),
                lb_type=lb_type,
                district=resolve_district(district),
            )
        )
    return out


def exact_code_matches(
    ours: Iterable[LocalBody], theirs: Iterable[LocalBody]
) -> dict[str, str]:
    """``{our lb_code: their code}`` wherever the OSM code literally equals ours.

    Only kept when the OSM-side code is *unique* among ``theirs`` -- the
    release carries one duplicated ``LSGI_Code`` (``G08064``, shared by a
    Kannur municipality and an unrelated Thrissur Grama Panchayat), and an
    ambiguous code is worse than no code at all: silently picking either
    candidate has a real chance of being wrong. An ambiguous code falls
    through to the name cascade instead, where district scoping disambiguates
    it correctly.

    This dict is fed to :func:`geo.build.crosswalk.build_crosswalk` as its
    ``overrides`` argument -- an auto-detected exact code match and a
    hand-reviewed override are the same shape to that function, "trust this
    pairing, skip the name cascade" -- so nothing new needs to be built to
    apply it.
    """
    counts = Counter(lb.code for lb in theirs)
    by_code = {lb.code: lb for lb in theirs if counts[lb.code] == 1}
    our_codes = {lb.code for lb in ours}
    return {code: code for code in our_codes if code in by_code}


def load_overrides(path: Path) -> dict[str, str]:
    """Hand-maintained ``lb_code -> osm_code`` pairings, consulted before the
    name cascade -- see :data:`OVERRIDE_FILENAME` and the module docstring for
    the four rows this file currently carries and why each one is there.
    """
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return {
            row["lb_code"].strip(): row["osm_code"].strip()
            for row in csv.DictReader(fh)
            if row.get("lb_code") and row.get("osm_code")
        }


def build_osm_crosswalk(
    ours: Iterable[LocalBody],
    theirs: Iterable[LocalBody],
    *,
    overrides: Mapping[str, str] | None = None,
) -> CrosswalkResult:
    """Resolve every one of ours to an OSM body.

    Auto-detected exact-code matches (:func:`exact_code_matches`) are applied
    first; the hand-maintained override file wins over those where the two
    disagree, since a human reviewing a specific residual is stronger
    evidence than an automatic code match that turned out to need one.
    Everything neither covers goes through
    :func:`geo.build.crosswalk.build_crosswalk`'s name-and-ward cascade
    unchanged.
    """
    ours = list(ours)
    theirs = list(theirs)
    merged = dict(exact_code_matches(ours, theirs))
    merged.update(overrides or {})
    return build_crosswalk(ours, theirs, overrides=merged)


def write_crosswalk(result: CrosswalkResult, path: Path) -> None:
    """Emit the reviewable OSM crosswalk CSV -- see
    :func:`geo.build.crosswalk.write_crosswalk`, the pattern this mirrors,
    with OSM-flavoured column names so a reviewer never confuses this file's
    ``osm_code`` for a KSMART one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(result.matches, key=lambda m: m.ours.code)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CROSSWALK_COLUMNS)
        writer.writeheader()
        for m in rows:
            writer.writerow(
                {
                    "lb_code": m.ours.code,
                    "osm_code": m.theirs.code,
                    "lb_type": m.ours.lb_type,
                    "district_name": m.ours.district,
                    "lb_name": m.ours.name,
                    "osm_name": m.theirs.name,
                    "match_method": m.method,
                    "ward_agreement": f"{m.ward_agreement:.3f}",
                }
            )
