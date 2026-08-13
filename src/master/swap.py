"""Build into scratch schemas, then swap them in.

The build used to write ``core.local_body`` before the gate ran, and dropped
``core.lb_sulekha_year`` on the way there. A build that failed its gate
therefore left the database holding a fresh spine and no crosswalk -- every
join from a meeting or a project to a body gone, on a site that is public. The
runbook claimed the build wrote nothing on failure; for the spine that was not
true.

Two ways to fix that were open: resolve everything in memory and write only
once every gate has passed, or build into scratch schemas and swap. This module
is the second, for two reasons.

*The gate is not the only thing that can fail.* Resolving in memory protects the
spine, but ``schema.sql`` still drops ``finance.project`` and spends twenty
minutes rebuilding 3.6M rows. A disk that fills in minute nineteen would take
the site down just as thoroughly as a failed gate, and no amount of in-memory
resolution helps. Staging covers both, because nothing live is touched until
every table exists and is full.

*A rebuild must not need a maintenance window.* The site is public. Wrapping the
build in one transaction would be atomic, but ``DROP TABLE`` takes an
ACCESS EXCLUSIVE lock the moment it runs, so every reader would block for the
length of the build. Renaming four schemas takes milliseconds, and Postgres
runs DDL transactionally, so the swap is one atomic step readers cannot observe
half of.

The cost is disk: the derived schemas exist twice for the length of a build,
about 3.7 GB on top of the 5.2 GB database. That is the price of not being down.

Only the derived schemas are staged. ``src_elections`` and ``src_geo`` are
reloaded verbatim from files on disk before the gate runs, so they are not
staged -- they carry no judgement, and a reload that fails raises rather than
half-writing. ``master build --skip-load`` leaves them alone entirely, which is
what to use when re-running a build that failed its gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from master.db import Database

#: The schemas this build owns outright. Every table in them is derived and is
#: rebuilt from the ``src_*`` schemas on every run, which is what makes swapping
#: the whole schema -- rather than migrating tables inside it -- the honest move.
DERIVED: Final = ("core", "finance", "meetings", "elections")

#: Where a build in progress writes. Dropped before every build and after every
#: swap, so a crashed run costs disk and nothing else.
STAGING_SUFFIX: Final = "_build"

#: What the live schema is renamed to during the swap, for the seconds between
#: the rename and the drop.
REPLACED_SUFFIX: Final = "_replaced"

#: ``\b`` is doing real work here: ``_`` is a word character, so ``src_elections.``
#: and ``src_geo.`` do not match, and neither does ``sakarma.``.
_QUALIFIED: Final = re.compile(r"\b(" + "|".join(DERIVED) + r")\.")


@dataclass(frozen=True, slots=True)
class Target:
    """Which set of schemas one run reads and writes.

    ``Target()`` is the live schemas, which is what a reader gets. The build
    uses ``Target(STAGING_SUFFIX)`` throughout and only renames into place once
    every gate has passed and every table is full.
    """

    suffix: str = ""

    def name(self, schema: str) -> str:
        return schema + self.suffix

    @property
    def core(self) -> str:
        return self.name("core")

    @property
    def finance(self) -> str:
        return self.name("finance")

    @property
    def meetings(self) -> str:
        return self.name("meetings")

    @property
    def elections(self) -> str:
        return self.name("elections")

    @property
    def schemas(self) -> tuple[str, ...]:
        return tuple(self.name(s) for s in DERIVED)

    @property
    def staging(self) -> bool:
        return bool(self.suffix)

    def sql(self, text: str) -> str:
        """Point every ``core.``/``finance.``/``meetings.``/``elections.`` at this target.

        ``schema.sql`` is written against the live names so it stays readable and
        so ``psql -f`` still does the obvious thing. This rewrites the schema
        qualifiers, and nothing else: the source schemas keep their names, and no
        string literal in that file contains one of these four words followed by
        a dot.
        """
        if not self.suffix:
            return text
        return _QUALIFIED.sub(lambda m: f"{m.group(1)}{self.suffix}.", text)


def create(db: Database, target: Target) -> None:
    """An empty scratch schema set, whatever a previous run left behind."""
    discard(db, target)
    for schema in target.schemas:
        db.execute(f"CREATE SCHEMA {schema};")


def discard(db: Database, target: Target) -> None:
    """Throw a half-built run away. Refuses to touch the live schemas."""
    if not target.staging:
        raise ValueError("discard() is for a staging target; it will not drop the live schemas")
    for schema in target.schemas:
        db.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE;")


def swap(db: Database, target: Target) -> None:
    """Rename the staged schemas into place, atomically, then drop the old ones.

    Renaming a schema carries its tables, indexes, sequences and functions with
    it, and the foreign key from ``lb_sulekha_year`` to ``local_body`` lives
    inside ``core``, so nothing has to be re-pointed. The derived tables are all
    ``CREATE TABLE AS``: none of them depends on another schema after it is
    built.

    The drop is deliberately outside the transaction. It is the slow part, it
    frees several gigabytes, and by then the site is already reading the new
    data -- so a failure there costs disk, not correctness.
    """
    if not target.staging:
        raise ValueError("swap() needs a staging target; the live schemas are the destination")

    for schema in DERIVED:
        db.execute(f"DROP SCHEMA IF EXISTS {schema}{REPLACED_SUFFIX} CASCADE;")

    with db.connection.transaction():
        for schema in DERIVED:
            if _exists(db, schema):
                db.execute(f"ALTER SCHEMA {schema} RENAME TO {schema}{REPLACED_SUFFIX};")
            db.execute(f"ALTER SCHEMA {target.name(schema)} RENAME TO {schema};")

    for schema in DERIVED:
        db.execute(f"DROP SCHEMA IF EXISTS {schema}{REPLACED_SUFFIX} CASCADE;")


def _exists(db: Database, schema: str) -> bool:
    return bool(db.scalar("SELECT 1 FROM pg_namespace WHERE nspname = %s", [schema]))
