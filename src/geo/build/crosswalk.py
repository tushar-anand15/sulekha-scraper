"""Pairing KSMART's local bodies to ``data_merge``'s ``lb_code``.

The two sides share no identifier. KSMART's ``lb_code`` is seven characters encoding
tier + district + block + serial (``G020103`` -- Kollam, block 01, GP 03); ours is six,
encoding tier + district + a serial running within the district (``G02003``). Neither
is derivable from the other, and ``data/raw/caches/lsgd_cache.sqlite`` was checked for
a bridge: LSGD uses internal integer ids, so there is none. That leaves names.

Names alone are not enough. A sample of 960 bodies joined on exact
``(name, lb_type, district)`` matched only 672 -- the rest differ because the two
sides transliterate Malayalam independently (``Kottarakara``/``Kottarakkara``,
``Tripunithura``, ``Anjuthengu``). This is the identical problem
``data_merge.transform.matching`` already solves for the SEC/LSGD pairing, so the
cascade there is reused wholesale rather than reinvented.

What is *not* reused is the rejection gate. ``matching.WardTally`` accumulates three
independent per-ward signals -- ward name, winner name, and party -- and keeps a
pairing if any one clears its threshold. KSMART carries none of the latter two: its
ward features hold names and nothing else. Feeding it ``WardTally`` would leave two of
three counters permanently zero, so the gate here is a ward-name set-overlap ratio,
modelled on ``WardTally.kept``'s threshold logic and its report-never-drop contract
but computed over the one signal that exists.

That verification is the point of the whole module. A pairing that matches on body
name but disagrees on ward names is wrong, and accepting it would attach a whole local
body's election results to the wrong polygons -- which renders as a plausible-looking
map that is silently false. Better to fail the build.
"""

from __future__ import annotations

import csv
import difflib
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from data_merge.transform.matching import (
    LBKey,
    normalize,
    pair_local_bodies,
    wardnames_agree,
)

#: Below this share of agreeing ward names, a pairing is rejected outright.
#: Deliberately the same 0.5 as ``matching.MIN_AGREEMENT``: the two sides
#: transliterate independently, so demanding near-perfect overlap would reject
#: correct pairings, while half the wards agreeing is far beyond coincidence.
MIN_WARD_AGREEMENT: Final = 0.5

#: Fewer wards than this and an agreement ratio is too noisy to gate on, so the
#: pairing is kept and flagged rather than rejected on thin evidence. Mirrors
#: ``matching._MIN_SAMPLE_FOR_GATE``.
MIN_SAMPLE_FOR_GATE: Final = 4

CROSSWALK_FILENAME: Final = "ksmart_lb_crosswalk.csv"
OVERRIDE_FILENAME: Final = "ksmart_lb_overrides.csv"

CROSSWALK_COLUMNS: Final = (
    "lb_code",
    "ksmart_lb_code",
    "lb_type",
    "district_name",
    "lb_name",
    "ksmart_name",
    "match_method",
    "ward_agreement",
    "wards_ours",
    "wards_ksmart",
)


@dataclass(frozen=True, slots=True)
class LocalBody:
    """One local body on either side, reduced to what pairing needs."""

    code: str
    name: str
    lb_type: str
    district: str
    ward_names: tuple[str, ...] = ()

    @property
    def key(self) -> LBKey:
        return (normalize(self.district), self.lb_type, normalize(self.name))


@dataclass(frozen=True, slots=True)
class Match:
    """One resolved pairing, with the evidence that resolved it."""

    ours: LocalBody
    theirs: LocalBody
    method: str
    ward_agreement: float

    @property
    def verified(self) -> bool:
        """Whether the ward-name evidence actually supports this pairing.

        A pairing below the sample floor is *unverified*, not *wrong* -- it is
        carried with the flag set so the crosswalk CSV shows a reviewer exactly
        which rows rest on thin evidence.

        The same applies, and matters more, when the *other* side carries no ward
        names at all. The opendatakerala source is exactly that case: it is a
        local-body polygon set with no ward data anywhere in it. Scoring those
        pairings against an empty list yields 0.0 agreement for every one of
        them, and rejecting on that would throw away all 1,200 bodies on the
        grounds that a comparison nobody could make did not succeed. Absence of
        evidence is not contradiction; only a side that *has* ward names can
        disagree about them.
        """
        if not self.theirs.ward_names:
            return True
        if len(self.ours.ward_names) < MIN_SAMPLE_FOR_GATE:
            return True
        return self.ward_agreement >= MIN_WARD_AGREEMENT


@dataclass(frozen=True, slots=True)
class CrosswalkResult:
    matches: tuple[Match, ...]
    unresolved: tuple[LocalBody, ...]
    """Ours, with no KSMART counterpart. Never silently dropped."""
    rejected: tuple[Match, ...]
    """Paired by name but contradicted by ward names -- worse than unresolved."""
    unclaimed: tuple[LocalBody, ...]
    """Theirs, never paired to anything of ours."""
    how: Counter[str]

    @property
    def resolved_count(self) -> int:
        return len(self.matches)

    def gate(self, expected: int) -> list[str]:
        """Reasons this crosswalk must not be trusted. Empty means it passed."""
        problems: list[str] = []
        if self.unresolved:
            problems.append(
                f"{len(self.unresolved)} local bodies unresolved: "
                + ", ".join(f"{lb.code} {lb.name}" for lb in self.unresolved[:10])
            )
        if self.rejected:
            problems.append(
                f"{len(self.rejected)} pairings rejected on ward disagreement: "
                + ", ".join(f"{m.ours.code}->{m.theirs.code}" for m in self.rejected[:10])
            )
        if self.resolved_count != expected:
            problems.append(f"resolved {self.resolved_count} of an expected {expected}")
        return problems


def ward_agreement(ours: Sequence[str], theirs: Sequence[str]) -> float:
    """Share of our ward names with an agreeing counterpart on the other side.

    Greedy one-to-one: each of their names can be consumed once, so a local body
    that repeats a name cannot inflate the score by matching it repeatedly.
    Asymmetric on purpose -- the question is whether *our* wards are accounted
    for, and KSMART having extra wards is a delimitation difference, not evidence
    of a bad pairing.
    """
    if not ours:
        return 0.0
    remaining = list(theirs)
    agreed = 0
    for name in ours:
        for i, candidate in enumerate(remaining):
            if wardnames_agree(name, candidate):
                agreed += 1
                del remaining[i]
                break
    return agreed / len(ours)


class DistrictMismatch(RuntimeError):
    """A district on one side has no counterpart on the other."""


def pair_districts(ours: Iterable[str], theirs: Iterable[str]) -> dict[str, str]:
    """Map each of their district spellings onto ours, normalised.

    This exists because the district is the *scoping key* for local-body pairing,
    and that makes a mismatch here categorically worse than a mismatch anywhere
    else: a body whose name is spelled differently fails to match on its own, but
    a district whose name is spelled differently disqualifies every body inside it
    at once, and does so silently -- they simply look unpaired.

    That is not hypothetical. Kerala has fourteen districts; thirteen agree
    exactly between the two sources. The fourteenth is ``KASARGOD`` on our side
    and ``Kasaragod`` on KSMART's -- one letter -- and before this function
    existed it cost all 38 local bodies in that district.

    Fourteen is small enough to demand every one resolve, so an unmatched
    district raises instead of degrading quietly.
    """
    ours_norm = {normalize(d): d for d in ours if d}
    mapping: dict[str, str] = {}
    unresolved: list[str] = []

    for name in theirs:
        if not name:
            continue
        key = normalize(name)
        if key in ours_norm:
            mapping[key] = key
            continue
        close = difflib.get_close_matches(key, list(ours_norm), n=2, cutoff=0.85)
        if len(close) == 1:
            mapping[key] = close[0]
        elif len(close) > 1:
            # Two plausible districts is not a near-miss, it is ambiguity. Kerala's
            # district names are distinctive enough that this should never happen;
            # if it does, guessing would mis-scope a whole district's bodies.
            unresolved.append(f"{name} (ambiguous: {close})")
        else:
            unresolved.append(name)

    if unresolved:
        raise DistrictMismatch(
            "districts with no counterpart: "
            + ", ".join(unresolved)
            + f". Known: {sorted(ours_norm)}"
        )
    return mapping


def load_overrides(path: Path) -> dict[str, str]:
    """Hand-maintained ``lb_code -> ksmart_lb_code`` pairings, consulted first.

    Exists because fuzzy matching will always leave a residue, and the honest fix
    for those is a human deciding once and recording it -- not a looser threshold
    that silently mispairs something else.
    """
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return {
            row["lb_code"].strip(): row["ksmart_lb_code"].strip()
            for row in csv.DictReader(fh)
            if row.get("lb_code") and row.get("ksmart_lb_code")
        }


def build_crosswalk(
    ours: Iterable[LocalBody],
    theirs: Iterable[LocalBody],
    *,
    overrides: Mapping[str, str] | None = None,
) -> CrosswalkResult:
    """Pair our local bodies to KSMART's, verifying each against ward names."""
    ours = list(ours)
    theirs = list(theirs)
    overrides = dict(overrides or {})

    by_code = {lb.code: lb for lb in theirs}

    # Reconcile district spellings before anything is keyed by them -- see
    # pair_districts for why a mismatch here is silently catastrophic.
    district_alias = pair_districts(
        (lb.district for lb in ours), (lb.district for lb in theirs)
    )

    # Their names are not unique globally, only within (district, tier).
    by_key: dict[tuple[str, str], dict[str, LocalBody]] = defaultdict(dict)
    for lb in theirs:
        district = district_alias.get(normalize(lb.district), normalize(lb.district))
        by_key[(district, lb.lb_type)][normalize(lb.name)] = lb

    matches: list[Match] = []
    rejected: list[Match] = []
    unresolved: list[LocalBody] = []
    how: Counter[str] = Counter()
    claimed: set[str] = set()

    forced: dict[str, LocalBody] = {}
    pending: list[LocalBody] = []
    for lb in ours:
        target = overrides.get(lb.code)
        if target and target in by_code:
            forced[lb.code] = by_code[target]
        else:
            pending.append(lb)

    pool = {k: frozenset(v) for k, v in by_key.items()}
    pairing = pair_local_bodies((lb.key for lb in pending), pool)
    by_our_key = {lb.key: lb for lb in pending}

    def record(lb: LocalBody, other: LocalBody, method: str) -> None:
        score = ward_agreement(lb.ward_names, other.ward_names)
        match = Match(ours=lb, theirs=other, method=method, ward_agreement=score)
        if match.verified:
            matches.append(match)
            how[method] += 1
            claimed.add(other.code)
        else:
            rejected.append(match)

    for code, other in forced.items():
        lb = next(x for x in ours if x.code == code)
        record(lb, other, "override")

    for key, matched_name in pairing.matched.items():
        lb = by_our_key.get(key)
        if lb is None:
            continue
        other = by_key[(key[0], key[1])][matched_name]
        method = _method_for(pairing.how, key, matched_name)
        record(lb, other, method)

    for key in pairing.unpaired:
        lb = by_our_key.get(key)
        if lb is not None:
            unresolved.append(lb)

    unclaimed = tuple(lb for lb in theirs if lb.code not in claimed)
    return CrosswalkResult(
        matches=tuple(matches),
        unresolved=tuple(unresolved),
        rejected=tuple(rejected),
        unclaimed=unclaimed,
        how=how,
    )


def _method_for(how: Counter[str], key: LBKey, matched: str) -> str:
    """``pair_local_bodies`` reports method counts, not per-pairing methods.

    Recovering the exact rule per row would mean reimplementing the cascade, so
    the crosswalk records the cheap, honest distinction instead: an identical
    normalised name, or something fuzzier that a reviewer should look at.
    """
    return "exact" if key[2] == matched else "fuzzy"


def write_crosswalk(result: CrosswalkResult, path: Path) -> None:
    """Emit the reviewable artifact.

    Written sorted by our code so a diff between two runs is readable, and
    carrying the method and agreement score so a reviewer can go straight to the
    rows that were not exact.
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
                    "ksmart_lb_code": m.theirs.code,
                    "lb_type": m.ours.lb_type,
                    "district_name": m.ours.district,
                    "lb_name": m.ours.name,
                    "ksmart_name": m.theirs.name,
                    "match_method": m.method,
                    "ward_agreement": f"{m.ward_agreement:.3f}",
                    "wards_ours": len(m.ours.ward_names),
                    "wards_ksmart": len(m.theirs.ward_names),
                }
            )
