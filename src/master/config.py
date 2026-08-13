"""Where the master build reads from and writes to.

Mirrors ``geo.config`` and ``data_merge.config``: one dataclass resolved once,
no module-level state, every path derived from a single repo root so the build
runs the same from any working directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# src/master/config.py -> src/master -> src -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_DATABASE_URL = "postgresql://sambandh:sambandh@localhost:55432/sambandh"


@dataclass(frozen=True, slots=True)
class Paths:
    """Every location the master build touches."""

    root: Path
    final: Path
    reference: Path
    out: Path

    @property
    def geo_layers(self) -> Path:
        return self.final / "geo"

    @property
    def geo_reference(self) -> Path:
        return self.reference / "geo"

    @property
    def overrides(self) -> Path:
        return self.reference / "master" / "crosswalk_overrides.csv"

    @property
    def sec_registry_cache(self) -> Path:
        """``data_merge``'s 2020 scrape cache, read here for one thing only.

        The SEC's district dropdowns name every body it recognises, including
        bodies that returned no result. That is the only place Mattannur
        Municipality appears, so the cache is an input to the spine and not just
        an artefact of the elections build.
        """
        return self.root / "data" / "raw" / "caches" / "raw_cache_2020.sqlite"

    def cycle(self, year: int) -> Path:
        return self.final / str(year)


def resolve_paths(root: Path | None = None) -> Paths:
    base = (root or REPO_ROOT).resolve()
    return Paths(
        root=base,
        final=base / "data" / "final",
        reference=base / "data" / "reference",
        out=base / "data" / "final" / "master",
    )


def database_url() -> str:
    """The master database. Overridable so tests can point at a scratch one."""
    return os.environ.get("MASTER_DATABASE_URL", DEFAULT_DATABASE_URL)


# Election cycles, and the financial years Sulekha covers.
CYCLES = (2010, 2015, 2020, 2025)
FIRST_FINANCIAL_YEAR = 2012
LAST_FINANCIAL_YEAR = 2025

# Sakarma's own tier ids, which the portal never spells out in English.
LB_TYPES = {
    1: "District Panchayat",
    2: "Block Panchayat",
    3: "Municipality",
    4: "Corporation",
    5: "Grama Panchayat",
}

# Kerala's fourteen districts, in the ordinal the SEC's codes carry -- characters
# two and three of every lb_code, so G02046 is district 2, Kollam. Spelled as the
# elections build spells them, because ``core.local_body.district_name`` must read
# the same whichever side of the build wrote the row.
#
# The elections data is the authority while it has a row to offer. This table is
# what answers for a district whose only body in the SEC's registry returned no
# result: there is then no elections row to copy a name from, and
# ``district_name`` is NOT NULL.
DISTRICTS = {
    1: "THIRUVANANTHAPURAM",
    2: "KOLLAM",
    3: "PATHANAMTHITTA",
    4: "ALAPPUZHA",
    5: "KOTTAYAM",
    6: "IDUKKI",
    7: "ERNAKULAM",
    8: "THRISSUR",
    9: "PALAKKAD",
    10: "MALAPPURAM",
    11: "KOZHIKODE",
    12: "WAYANAD",
    13: "KANNUR",
    14: "KASARGOD",
}

TIER_BY_PREFIX = {
    "G": "Grama Panchayat",
    "B": "Block Panchayat",
    "D": "District Panchayat",
    "M": "Municipality",
    "C": "Corporation",
}
