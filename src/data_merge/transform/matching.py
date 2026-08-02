"""Verify that a name- or number-based pairing between two portals is sane.

The SEC and LSGD portals share no identifiers: the SEC uses ward codes
(``G01043006``), LSGD uses its own internal ids and plain ward numbers. So
local bodies are paired by name within (district, lb_type), and every pairing
is then verified independently -- by comparing the ward name and the winner's
name, two fields both portals record without copying from each other.

Matching drifts because the two sides transliterate Malayalam independently
(``ATHIYANOOR``/``ATHIYANNOOR``, ``Kochi``/``Cochin``), so nothing here trusts
a single comparison. ``names_agree`` and ``wardnames_agree`` are ported
behaviourally unchanged from ``merge_lsgd.py`` -- the exact-then-loosening
cascade below, and the rejection gate that follows it, are what makes a wrong
pairing show up as disagreement instead of silently wrong data.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

# Only OCCUPATIONAL prefixes are stripped when comparing names. Never KUMARI
# or KUMAR -- those are ordinary parts of Malayali names (NIRMALAKUMARI,
# SUNITHAKUMARI), and stripping them breaks the very comparisons they appear
# in.
RE_HONORIFIC: Final = re.compile(r"\b(ADV|ADVT|DR|PROF|MR|MRS|MS)\b\.?", re.I)
RE_WARD_WORD: Final = re.compile(r"WARD", re.I)

_CLOSE_RATIO: Final = 0.85
_MIN_LONG_TOKEN: Final = 5
_MIN_PREFIX: Final = 8
"""A shorter shared prefix is coincidence rather than the same person: Kerala
names repeat their opening syllables often."""


def normalize(s: str) -> str:
    """Case- and punctuation-insensitive comparison key."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def has_latin(s: str) -> bool:
    """False for Malayalam text -- 2020 publishes candidate names that way on
    the SEC side, which is exactly when a name-based check becomes unusable
    and callers must fall back to a script-independent signal instead."""
    return any("a" <= ch.lower() <= "z" for ch in (s or ""))


def _name_variants(s: str) -> list[str]:
    """The portals space compound names differently, so compare space-free,
    and a parenthesised alias -- ``THOMAS SCARIA (P.T.SCARIA)`` -- may be the
    exact form the other portal uses on its own."""
    upper = (s or "").upper()
    forms = [upper]
    alias = re.search(r"\((.*?)\)", upper)
    if alias:
        forms.append(alias.group(1))
    forms.append(re.sub(r"\(.*?\)", "", upper))
    return [re.sub(r"[^A-Z]", "", RE_HONORIFIC.sub(" ", f)) for f in forms]


def _long_tokens(s: str) -> list[str]:
    cleaned = RE_HONORIFIC.sub(" ", (s or "").upper())
    return [t for t in re.sub(r"[^A-Z ]", " ", cleaned).split() if len(t) >= _MIN_LONG_TOKEN]


def names_agree(a: str, b: str) -> str:
    """Do these two strings name the same person?

    Both sides already refer to the same ward, so the only question is
    whether the pairing that put them together is sane. Returns the rule
    that matched, or ``""`` for no match.
    """
    for x in _name_variants(a):
        for y in _name_variants(b):
            if not x or not y:
                continue
            if x == y:
                return "exact"
            if sorted(x) == sorted(y):
                return "anagram"
            if difflib.SequenceMatcher(None, x, y).ratio() >= _CLOSE_RATIO:
                return "close"
            # One portal records a fuller name than the other: LSGD's
            # "SALIKUTTY JOSEPH" against the SEC's "Sali Kutty". Space-free
            # these become SALIKUTTYJOSEPH and SALIKUTTY -- a clean prefix, but
            # only 0.75 by ratio, because the surname is pure unmatched length.
            # A prefix is far stronger evidence than that ratio suggests, so it
            # is admitted separately, with a length floor: short prefixes
            # ("RAJAN" inside "RAJANBABU") are common enough to be coincidence.
            if len(x) >= _MIN_PREFIX and len(y) >= _MIN_PREFIX:
                if x.startswith(y) or y.startswith(x):
                    return "prefix"
    for ta in _long_tokens(a):
        for tb in _long_tokens(b):
            if difflib.SequenceMatcher(None, ta, tb).ratio() >= _CLOSE_RATIO:
                return "token"
    return ""


_RULE_RANK: Final[dict[str, int]] = {
    "exact": 5,
    "anagram": 4,
    "prefix": 3,
    "close": 2,
    "token": 1,
    "": 0,
}


def name_score(a: str, b: str) -> tuple[int, float]:
    """How strongly two names agree, as ``(rule rank, best ratio)``.

    :func:`names_agree` answers "do these agree at all?", which is the right
    question when verifying an already-made pairing. Choosing *between*
    candidates needs a comparable strength instead: in ward G09035009 the
    elected member is "MUHAMMED SHAREEF" and two tied candidates are "Mohammed
    Asharaf" and "Mohammed Shareef" -- both agree loosely, and taking whichever
    is encountered first hands the ward to the wrong person.
    """
    best = 0.0
    for x in _name_variants(a):
        for y in _name_variants(b):
            if x and y:
                best = max(best, difflib.SequenceMatcher(None, x, y).ratio())
    return _RULE_RANK[names_agree(a, b)], best


def wardnames_agree(a: str, b: str) -> bool:
    """Ward names are English on both portals in every year, so this works
    even when candidate names are not comparable.

    Also used, unmodified, to compare local-body names one step of the
    pairing cascade below -- a fuzzy string match is a fuzzy string match
    regardless of which field it is applied to.
    """
    # SEC suffixes some ward names with "WARD"; LSGD does not. Strip it before
    # comparing, or every ward in those local bodies looks wrong.
    x = normalize(RE_WARD_WORD.sub(" ", (a or "").upper()))
    y = normalize(RE_WARD_WORD.sub(" ", (b or "").upper()))
    if not x or not y:
        return False
    if x == y:
        return True
    return difflib.SequenceMatcher(None, x, y).ratio() >= _CLOSE_RATIO


# ---------------------------------------------------------------------------
# Local-body pairing cascade
# ---------------------------------------------------------------------------

_ONE_PER_DISTRICT_TYPES: Final[frozenset[str]] = frozenset({"District Panchayat", "Corporation"})
_DIFFLIB_CUTOFF: Final = 0.72
_DIFFLIB_MARGIN: Final = 0.08
"""Below this margin, two fuzzy candidates are too close to trust a pick.
Picking one arbitrarily would silently mispair a local body, and every
reservation and role beneath it would land on the wrong wards -- so this
case yields no pairing instead."""

LBKey = tuple[str, str, str]
"""(district, lb_type, name_norm) -- a local body on the side being matched."""


@dataclass(frozen=True, slots=True)
class Pairing:
    """Result of the local-body pairing cascade."""

    matched: dict[LBKey, str]
    """Target key -> the normalised name it paired to in ``pool``."""

    how: Counter[str]
    """Which cascade step resolved each pairing, for the merge report."""

    unpaired: tuple[LBKey, ...]
    """Never silently dropped -- callers must report these."""


def pair_local_bodies(
    targets: Iterable[LBKey], pool: Mapping[tuple[str, str], frozenset[str]]
) -> Pairing:
    """Pair each target local body to one in ``pool``, most specific rule first.

    1. Exact match on the normalised name.
    2. One-per-district: District Panchayats and Corporations are unique
       within (district, lb_type), and LSGD names them for the district while
       the SEC report truncates its district column -- a name match is
       unnecessary here and would often fail anyway.
    3. Fuzzy name agreement (``wardnames_agree``), accepted only when exactly
       one pool candidate agrees -- two or more agreeing is ambiguity, and
       falls through to the next step.
    4. ``difflib.get_close_matches`` with a uniqueness margin between the top
       two candidates.

    Every pairing that survives this cascade is still provisional -- the
    rejection gate below verifies it ward by ward before it is trusted.
    """
    matched: dict[LBKey, str] = {}
    how: Counter[str] = Counter()
    unpaired: list[LBKey] = []

    for district, lb_type, name in targets:
        candidates = pool.get((district, lb_type), frozenset())
        key: LBKey = (district, lb_type, name)

        if name in candidates:
            matched[key] = name
            how["exact"] += 1
            continue

        if lb_type in _ONE_PER_DISTRICT_TYPES and len(candidates) == 1:
            matched[key] = next(iter(candidates))
            how["one_per_district"] += 1
            continue

        agreeing = [c for c in candidates if wardnames_agree(c, name)]
        if len(agreeing) == 1:
            matched[key] = agreeing[0]
            how["fuzzy_name"] += 1
            continue

        close = difflib.get_close_matches(name, sorted(candidates), n=2, cutoff=_DIFFLIB_CUTOFF)
        if len(close) == 1:
            matched[key] = close[0]
            how["difflib"] += 1
            continue
        if len(close) > 1:
            top = difflib.SequenceMatcher(None, name, close[0]).ratio()
            second = difflib.SequenceMatcher(None, name, close[1]).ratio()
            if top - second >= _DIFFLIB_MARGIN:
                matched[key] = close[0]
                how["difflib"] += 1
                continue

        unpaired.append(key)

    return Pairing(matched=matched, how=how, unpaired=tuple(unpaired))


# ---------------------------------------------------------------------------
# Rejection gate
# ---------------------------------------------------------------------------

MIN_AGREEMENT: Final = 0.5
"""A pairing is kept if at least half its wards agree on ward name or on the
winner's name. Names alone reject correct pairings -- SEC suffixes "WARD",
writes "TEMPLE" for LSGD's "KSHETHRAM", and in 2020 renders some ward names in
Malayalam against LSGD's English -- so either signal clearing the bar on its
own is enough."""

MIN_PARTY_AGREEMENT: Final = 0.8
"""Party is a weak identifier alone -- a handful of fronts contest every
ward -- so accepting a pairing on party agreement needs a much higher bar."""

_MIN_SAMPLE_FOR_GATE: Final = 4
"""Below this many wards, an agreement rate is too noisy to gate on, so the
pairing is kept by default rather than rejected on thin evidence."""


@dataclass(frozen=True, slots=True)
class WardTally:
    """Per-local-body agreement counts, accumulated ward by ward."""

    total: int = 0
    ward_agrees: int = 0
    name_agrees: int = 0
    party_agrees: int = 0

    def add(self, *, ward: bool, name: bool, party: bool) -> WardTally:
        return WardTally(
            total=self.total + 1,
            ward_agrees=self.ward_agrees + int(ward),
            name_agrees=self.name_agrees + int(name),
            party_agrees=self.party_agrees + int(party),
        )

    @property
    def kept(self) -> bool:
        """Whether this pairing clears the rejection gate.

        A local body whose wards agree on nothing -- ward name, winner name,
        or party -- is a wrong pairing rather than a noisy right one.
        Carrying its wards forward would stamp the wrong reservation and role
        onto every one of them, which is worse than carrying no data at all.
        """
        if self.total < _MIN_SAMPLE_FOR_GATE:
            return True
        return (
            self.ward_agrees / self.total >= MIN_AGREEMENT
            or self.name_agrees / self.total >= MIN_AGREEMENT
            or self.party_agrees / self.total >= MIN_PARTY_AGREEMENT
        )


@dataclass(frozen=True, slots=True)
class GateResult:
    """Which pairings survived, and -- reported, never dropped -- which did not."""

    kept: frozenset[str]
    rejected: dict[str, WardTally]


def apply_gate(tallies: Mapping[str, WardTally]) -> GateResult:
    """Apply the rejection gate to every local body's accumulated tally."""
    kept = frozenset(code for code, tally in tallies.items() if tally.kept)
    rejected = {code: tally for code, tally in tallies.items() if not tally.kept}
    return GateResult(kept=kept, rejected=rejected)
