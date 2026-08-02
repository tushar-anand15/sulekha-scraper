"""Compare party labels across two vocabularies, and apply a caller's front table.

The PDF report and the LSGD portal abbreviate parties differently --
``ML``/``IUML``, ``IND``/``INDEPENDENT`` -- and comparing the raw strings
would call the same party a mismatch. ``party_key`` closes that gap for
*comparison only*; a candidate row's ``party_name`` keeps whatever its own
source published, in that source's own spelling, always.

Which front a party belongs to (UDF / LDF / NDA / OTH) is not this module's
business to know. 2010's alliances are not 2015's -- RSP was LDF in 2010 and
UDF by 2015, KC(B) the other way round -- so a hardcoded mapping here would be
wrong for at least one cycle by construction. The front table is always a
parameter, sourced from a year's own published feed or its authored reference
table.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

_PARTY_EQUIV: Final[dict[str, str]] = {
    "ML": "IUML",
    "IND": "INDEPENDENT",
}
"""Abbreviation differences that plain punctuation-stripping cannot close.
``KC(M)`` and ``KCM`` already normalise to the same key without an entry here
-- only genuinely different abbreviations for the same party need one."""


def normalize(s: str) -> str:
    """Case- and punctuation-insensitive comparison key."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def party_key(party: str) -> str:
    """Canonical comparison key for a party label.

    Never used for output -- only to test whether two published spellings
    name the same party.
    """
    n = normalize(party)
    return _PARTY_EQUIV.get(n, n)


def parties_agree(a: str, b: str) -> bool:
    """Whether two published party labels name the same party."""
    ka, kb = party_key(a), party_key(b)
    return bool(ka) and ka == kb


@dataclass(frozen=True, slots=True)
class FrontEntry:
    """One row of a caller-supplied front table."""

    party_name: str
    party_group: str
    party_front: str
    evidence_tier: str = ""


@dataclass(frozen=True, slots=True)
class FrontResolution:
    """The front assigned to one candidate's party, and whether it was found."""

    party_name: str
    party_group: str
    party_front: str
    mapped: bool


def resolve_front(
    party_label: str, table: Mapping[str, FrontEntry], *, default_group: str = "OTH"
) -> FrontResolution:
    """Look up a party's front in a table the caller supplies.

    A party absent from the table maps to ``default_group`` and is reported
    as unmapped: ``mapped=False`` is the caller's cue to count and surface
    it. Defaulting quietly would misattribute a fringe party's wins to
    whichever group ``OTH`` happens to mean that year.
    """
    entry = table.get(party_label)
    if entry is not None:
        return FrontResolution(entry.party_name, entry.party_group, entry.party_front, True)
    return FrontResolution(party_label, default_group, default_group, False)
