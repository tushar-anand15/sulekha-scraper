"""Reducing two portals' spellings of one place to a comparable key.

Two details do most of the work in the crosswalk, and both are here rather than
inline because they are the difference between a body matching exactly and a
body resting on a fuzzy score a reviewer has to audit.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

#: Malayalam chillu letters have two encodings: an atomic character (ർ) and the
#: older consonant + virama + ZWJ sequence (ര്‍). The two portals disagree, and
#: stripping ZWJ leaves consonant + virama, so fold the atomic form to match.
#: Folding these moved 10 bodies from ``similarity`` to ``exact``.
CHILLU: Final[dict[str, str]] = {
    "ൺ": "ണ്",  # ൺ -> ണ്
    "ൻ": "ന്",  # ൻ -> ന്
    "ർ": "ര്",  # ർ -> ര്
    "ൽ": "ല്",  # ൽ -> ല്
    "ൾ": "ള്",  # ൾ -> ള്
    "ൿ": "ക്",  # ൿ -> ക്
}

#: The zero-width joiner and non-joiner, which carry no meaning once the chillu
#: forms have been folded but do break string equality.
_ZERO_WIDTH: Final = "‌‍"

#: Sulekha writes the tier into the body's English name ("Kudayathoor Grama
#: Panchayat"); the elections build keeps them in separate columns. Comparing
#: the two without stripping this matches almost nothing.
TIER_SUFFIX: Final = re.compile(
    r"\s*(Grama Panchayat|Block Panchayat|District Panchayat"
    r"|Municipal Corporation|Municipality|Corporation)\s*$",
    re.IGNORECASE,
)


def nm_ml(text: str | None) -> str:
    """A Malayalam name reduced to a comparable key."""
    folded = unicodedata.normalize("NFC", text or "")
    folded = "".join(CHILLU.get(ch, ch) for ch in folded)
    return "".join(ch for ch in folded if ch not in _ZERO_WIDTH and not ch.isspace())


def nm_en(text: str | None) -> str:
    """A Latin-script name reduced to a comparable key."""
    folded = unicodedata.normalize("NFC", text or "").lower()
    return "".join(ch for ch in folded if ch.isalnum())


def strip_tier(name: str | None) -> str:
    """Drop a trailing tier word, so Sulekha's names can be compared to ours."""
    return TIER_SUFFIX.sub("", name or "")


def nm_en_body(name: str | None) -> str:
    """The key a Sulekha body name is matched on: tier stripped, then folded."""
    return nm_en(strip_tier(name))
