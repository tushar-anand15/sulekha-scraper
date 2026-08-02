"""Measure a sex column's orientation, then resolve gender from ranked sources.

Two separate jobs, deliberately kept apart:

**Orientation measurement** (``measure_orientation``). The SEC's RTI candidate
report carries a Sex column for every candidate, winners and losers alike, but
the 2020 edition of that report has the column INVERTED at source -- an
identical extraction agrees with published gender at 98.5% in 2015, 100% in
2025, and 0.18% in 2020. That is a property of one document -- the parser and
layout are unaffected -- so nothing here may hardcode "2020 is inverted" as a
constant: the next cycle to ship a broken column would sail straight through a
per-year switch. Orientation is measured instead, against the one fact no
source controls -- only women may contest a women-reserved ward -- and
``unclear`` is a real outcome that the caller must act on, never silently
promoted to ``aligned``.

**Precedence resolution** (``resolve_gender``). Once a column's orientation is
known, or where the caller already has clean M/F/T values from several
sources, this combines them: the reserved-ward rule binds every candidate in
the ward, not merely the winner, and outranks a typed field the data is known
to get wrong in both directions (an honorific of ``Shri`` recorded for a woman,
an LSGD gender field of ``Male`` recorded for one). Below that, sources that
agree beat a single source, and a single source beats nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Final

WOMEN_RESERVED: Final[frozenset[str]] = frozenset({"Woman", "SC Woman", "ST Woman"})

_MIN_SAMPLE: Final = 200
"""Below this many observations, an orientation verdict is not meaningful --
the 50-row edge case in the tests exists precisely to lock this floor."""
_ALIGNED_MIN: Final = 0.90
_INVERTED_MAX: Final = 0.10
"""The bands sit away from 100%/0% because real data carries source-side
typos -- 2015 measures 98.1% aligned, 2010 99.8% -- not because a column
under this bar is ambiguous."""


class Verdict(Enum):
    ALIGNED = "aligned"
    INVERTED = "inverted"
    UNCLEAR = "unclear"


@dataclass(frozen=True, slots=True)
class Orientation:
    """Result of measuring a sex column against the women-reserved-ward rule."""

    verdict: Verdict
    share_female: float
    n: int


def measure_orientation(sexes_in_reserved_wards: Iterable[str]) -> Orientation:
    """Measure a sex column's orientation.

    ``sexes_in_reserved_wards`` is the column's raw M/F value for every
    candidate the caller has already joined to a women-reserved ward -- every
    candidate there, because the reservation binds all of them, which is what
    makes the sample large enough to be decisive. A verdict of ``unclear``
    below the sample floor is deliberate: it must not be reachable by
    widening the aligned/inverted bands, only by more data.
    """
    values = [v for v in sexes_in_reserved_wards if v in ("M", "F")]
    n = len(values)
    female = sum(1 for v in values if v == "F")
    share = female / n if n else 0.0

    if n < _MIN_SAMPLE:
        verdict = Verdict.UNCLEAR
    elif share >= _ALIGNED_MIN:
        verdict = Verdict.ALIGNED
    elif share <= _INVERTED_MAX:
        verdict = Verdict.INVERTED
    else:
        verdict = Verdict.UNCLEAR

    return Orientation(verdict=verdict, share_female=share, n=n)


def oriented_sex(raw_sex: str, orientation: Orientation) -> str:
    """Apply a measured orientation to one raw sex value.

    An ``unclear`` orientation returns ``""``: a blank is more useful here
    than a coin-flip, and a caller that silently picked "aligned" whenever
    unsure would reintroduce the exact defect this module exists to catch.

    ``T`` passes through untouched whatever the orientation. Inversion swaps
    the M and F columns; it says nothing about a third value, and an inferred
    rule must never overwrite a self declaration. Dropping it here -- as
    returning "" for anything outside M/F did -- erased the single
    transgender candidate in the 2025 report before the precedence rule that
    exists to protect them ever saw the value, and the women-reserved-ward rule
    then recorded them as F.
    """
    if raw_sex == "T":
        return "T"
    if raw_sex not in ("M", "F"):
        return ""
    if orientation.verdict is Verdict.ALIGNED:
        return raw_sex
    if orientation.verdict is Verdict.INVERTED:
        return "F" if raw_sex == "M" else "M"
    return ""


# ---------------------------------------------------------------------------
# Precedence resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GenderSource:
    """One candidate's value from a single source.

    Callers pass sources ordered strongest to weakest, with the honorific
    last -- the resolver has no notion of which source to trust more, only
    how to combine values once they arrive ranked.
    """

    name: str
    value: str
    """"M", "F", "T", or "" for not available."""


@dataclass(frozen=True, slots=True)
class GenderResolution:
    gender: str
    source: str


def resolve_gender(*, reserved: bool, sources: Sequence[GenderSource]) -> GenderResolution:
    """Resolve one candidate's gender from ranked sources.

    Precedence, strongest first:

    1. A self-declared ``"T"`` outranks everything else. Overriding it with
       an inferred rule would erase the one case where a candidate stated
       their own gender directly.
    2. The reserved-ward rule: only women may contest a women-reserved ward,
       and that binds EVERY candidate in the ward, not only the winner. A
       source that disagrees is flagged (``conflict_reserved``) but does not
       win -- the law is a stronger constraint than a typed field this
       dataset already knows contains errors in both directions.
    3. Two or more sources that agree (``both_agree``).
    4. A single available source, named for its own origin (``pdf``,
       ``sec_sex``, ``lsgd``, ``honorific``, ...).
    5. Nothing available: unresolved.

    Where ranked sources disagree outside a reserved ward, the strongest one
    wins and the row is flagged ``conflict_<name>_used`` rather than silently
    resolved -- the disagreement itself is the interesting fact.
    """
    for source in sources:
        if source.value == "T":
            return GenderResolution("T", source.name)

    non_empty = [s for s in sources if s.value in ("M", "F")]

    if reserved:
        if any(s.value == "F" for s in non_empty):
            return GenderResolution("F", "reserved_ward")
        if any(s.value == "M" for s in non_empty):
            return GenderResolution("F", "conflict_reserved")
        return GenderResolution("F", "reserved_ward")

    if not non_empty:
        return GenderResolution("", "")

    distinct = {s.value for s in non_empty}
    if len(distinct) == 1:
        if len(non_empty) > 1:
            return GenderResolution(non_empty[0].value, "both_agree")
        return GenderResolution(non_empty[0].value, non_empty[0].name)

    strongest = non_empty[0]
    return GenderResolution(strongest.value, f"conflict_{strongest.name}_used")
