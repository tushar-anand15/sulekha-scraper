"""The gate, and the report that says what the gate could not decide.

Two kinds of statement live here, and the difference between them is the whole
point of the file.

A **gate** is a fact the build refuses to be wrong about. Every Sakarma body
resolves to an ``lb_key``; every Sulekha year-row resolves; every project row
and every meeting row reaches a body; no two bodies share an ``lb_code``; every
body has a district and a tier. A gate that fails stops the build before a
single match is written, because a half-resolved crosswalk is worse than none:
the pages still render, they just show somebody else's spending, or none at all
under a name that has plenty.

**Coverage** is everything the gate cannot decide because it is a property of
the sources rather than of this build. Geometry covers 1,033 of 1,238 bodies
because KSMART publishes 1,033. One body has no election result because the SEC
never published one. Meetings begin in 2016 because Sakarma begins in 2016.
None of that is a failure, and none of it may be silently omitted either: a
report that leaves out what it could not check reads exactly like a report where
the check passed. The site has to state these numbers, so the build states them
first, from the same data.

Match method sits between the two. 186 Sakarma bodies and 4,372 Sulekha
year-rows rest on a similarity score rather than an exact name, and every one of
those is a body a reviewer could in principle re-check. The count belongs in
front of them on every run, not in a commit message from the day it was
measured.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from master import __version__
from master.config import DATASET_NAME, SOURCE_DUMPS, Paths
from master.crosswalk import CrosswalkResult, is_in_elections
from master.db import Database
from master.swap import Target

_WIDTH: Final = 62
_CHUNK: Final = 1 << 20

#: How many offending rows a failing gate names before it stops listing them.
#: Enough to recognise the pattern, not so many that the failure scrolls away.
NAMED: Final = 10


class Status:
    PASS = "PASS"
    FAIL = "FAIL"
    INFO = "INFO"


# ----------------------------------------------------------------- statements


@dataclass(frozen=True, slots=True)
class Gate:
    """One thing the build must be right about, with the count behind it.

    ``checked`` is what the gate looked at and ``failed`` is what it disagreed
    with, so a passing gate still carries evidence. "0 failed" over 443,235
    meetings says something a bare "PASS" does not.
    """

    name: str
    checked: int
    failed: int
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def render(self) -> str:
        status = Status.PASS if self.ok else Status.FAIL
        head = f"  [{status}] {self.name}"
        counts = f"{self.checked:,} checked, {self.failed:,} failed"
        tail = f"{counts} -- {self.detail}" if self.detail and not self.ok else counts
        return f"{head.ljust(_WIDTH)} {tail}"


@dataclass(frozen=True, slots=True)
class Line:
    """A coverage statement: measured, never a pass or a failure."""

    label: str
    detail: str
    status: str = Status.INFO

    def render(self) -> str:
        head = f"  [{self.status}] {self.label}"
        return f"{head.ljust(_WIDTH)} {self.detail}".rstrip()


@dataclass(frozen=True, slots=True)
class Section:
    title: str
    lines: tuple[Gate | Line, ...] = ()
    notes: tuple[str, ...] = ()

    def render(self, number: int) -> str:
        body = [f"{number}. {self.title}"]
        body += [line.render() for line in self.lines]
        body += [f"    {note}" for note in self.notes]
        return "\n".join(body)


# --------------------------------------------------------------- the database


@dataclass(frozen=True, slots=True)
class Source:
    """The counts the gates need from the database, read once.

    Read from the ``src_*`` schemas alone, so the same numbers are available
    before the crosswalk tables exist. That is what lets ``master validate`` gate
    exactly what ``master build`` gates, rather than a weaker version of it.
    """

    #: (sulekha_lb_id, year_label, project rows) -- the grain the finance join uses.
    projects: tuple[tuple[str, str, int], ...] = ()
    #: Project rows in the dump, including any that never reach a local body.
    projects_total: int = 0
    #: (sakarma lb_id, financial year, meetings)
    meetings: tuple[tuple[str, str, int], ...] = ()
    #: Meeting rows in the dump, including any that never reach a Sakarma body.
    meetings_total: int = 0
    #: The ``lb_code``s the geometry crosswalk carries a boundary for.
    geometry: frozenset[str] = frozenset()


PROJECTS_SELECT: Final = """
SELECT lb.id::text     AS sulekha_lb_id,
       d.year_label    AS year_label,
       count(*)::int   AS project_rows
FROM src_sulekha.projects p
JOIN src_sulekha.local_bodies lb ON lb.id = p.local_body_id
JOIN src_sulekha.districts   d  ON d.id  = lb.district_id
GROUP BY 1, 2
"""

# core.fin_year() is created by schema.sql, which a validate must not run, so
# the April-to-March rule is spelled out here. It has to agree with that
# function; if one changes, both do.
MEETINGS_SELECT: Final = """
SELECT m.lb_id::text AS lb_id,
       CASE WHEN extract(month FROM m.meeting_date) >= 4
            THEN extract(year FROM m.meeting_date)::int || '-'
                 || (extract(year FROM m.meeting_date)::int + 1)
            ELSE (extract(year FROM m.meeting_date)::int - 1) || '-'
                 || extract(year FROM m.meeting_date)::int
       END          AS year_label,
       count(*)::int AS meetings
FROM sakarma.meeting_manifest m
GROUP BY 1, 2
"""

GEOMETRY_SELECT: Final = """
SELECT lb_code FROM src_geo.ksmart_lb_crosswalk
UNION
SELECT lb_code FROM src_geo.ksmart_lb_overrides
"""


def read_source(db: Database) -> Source:
    """Aggregate the source tables the gates count against."""
    projects = db.query(PROJECTS_SELECT)
    meetings = db.query(MEETINGS_SELECT)
    return Source(
        projects=tuple(
            (r["sulekha_lb_id"], r["year_label"], int(r["project_rows"])) for r in projects
        ),
        projects_total=int(db.scalar("SELECT count(*) FROM src_sulekha.projects") or 0),
        meetings=tuple((r["lb_id"], r["year_label"], int(r["meetings"])) for r in meetings),
        meetings_total=int(db.scalar("SELECT count(*) FROM sakarma.meeting_manifest") or 0),
        geometry=frozenset(r["lb_code"] for r in db.query(GEOMETRY_SELECT) if r["lb_code"]),
    )


# --------------------------------------------------------------------- assess


@dataclass(frozen=True)
class Quality:
    """One run's gates, its coverage, and enough to write both down."""

    gates: tuple[Gate, ...]
    problems: tuple[str, ...]
    counts: dict[str, int]
    sakarma_methods: dict[str, int]
    sulekha_methods: dict[str, int]
    meetings_per_year: tuple[tuple[str, int], ...]
    no_election_result: tuple[str, ...]
    notes: tuple[str, ...] = ()
    inputs: tuple[Path, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures()

    def failures(self) -> list[str]:
        """Every reason this build must not be written. Empty means it passed."""
        return [
            f"{gate.name}: {gate.failed:,} of {gate.checked:,} -- {gate.detail}"
            for gate in self.gates
            if not gate.ok
        ] + list(self.problems)

    # -- the report ---------------------------------------------------------

    def sections(self) -> list[Section]:
        bodies = self.counts["bodies"]
        matched = sum(self.sakarma_methods.values())
        year_rows = sum(self.sulekha_methods.values())
        return [
            Section(
                title="Gates -- nothing is written unless every one passes",
                lines=self.gates,
            ),
            Section(
                title="Problems reported by the sources",
                lines=tuple(Line(label=p, detail="", status=Status.FAIL) for p in self.problems)
                or (Line(label="none", detail="no override or district name was left unresolved"),),
            ),
            Section(
                title="Coverage the site must state",
                lines=(
                    Line("bodies on the spine", f"{bodies:,}"),
                    Line(
                        "bodies with geometry",
                        f"{self.counts['bodies_with_geometry']:,} of {bodies:,}",
                    ),
                    Line(
                        "bodies with meetings",
                        f"{self.counts['bodies_with_meetings']:,} of {bodies:,}",
                    ),
                    Line(
                        "bodies with no election result (in_elections = false)",
                        f"{self.counts['bodies_without_election_result']:,} of {bodies:,}"
                        + _listed(self.no_election_result),
                    ),
                    Line("meetings", f"{self.counts['meetings']:,}"),
                    Line("project rows", f"{self.counts['project_rows']:,}"),
                ),
                notes=(
                    "A body absent here is absent from the source, not from the build. "
                    "The site must say which, on the page where the number is missing.",
                ),
            ),
            Section(
                title="How each match was made",
                lines=(
                    Line("sakarma bodies", _methods(self.sakarma_methods)),
                    Line("  resting on a similarity score", _fuzzy(self.sakarma_methods, matched)),
                    Line("sulekha year-rows", _methods(self.sulekha_methods)),
                    Line(
                        "  resting on a similarity score",
                        _fuzzy(self.sulekha_methods, year_rows),
                    ),
                ),
                notes=(
                    "A similarity match is a body a reviewer could still overturn. "
                    "An exact or elimination match is not a guess.",
                ),
            ),
            Section(
                title="Meetings per financial year",
                lines=tuple(Line(year, f"{count:,}") for year, count in self.meetings_per_year),
                notes=(
                    "Sakarma's record begins in 2015-2016 and thickens for years. "
                    "A thin year is a thin record, not a body that did not meet.",
                ),
            ),
        ]

    def render(self) -> str:
        out = ["Gram Sambandh master database - quality report", "=" * _WIDTH]
        out.append(f"{_stamp()}  master {__version__}")
        out.append("passed every gate" if self.ok else f"FAILED {len(self.failures())} gate(s)")
        out.append("")
        for number, section in enumerate(self.sections(), start=1):
            out.append(section.render(number))
            out.append("")
        if self.notes:
            out.append("Known limitations")
            out += [f"  {note}" for note in self.notes]
            out.append("")
        return "\n".join(out).rstrip() + "\n"

    # -- the manifest -------------------------------------------------------

    def _body(self) -> dict[str, Any]:
        """Everything that identifies this run, minus the wall clock.

        The stamp is excluded so two runs over unchanged sources produce the same
        fingerprint, which is the property worth being able to assert.
        """
        return {
            "version": __version__,
            "ok": self.ok,
            "gates": [
                {
                    "name": gate.name,
                    "ok": gate.ok,
                    "checked": gate.checked,
                    "failed": gate.failed,
                    "detail": gate.detail,
                }
                for gate in self.gates
            ],
            "problems": list(self.problems),
            "counts": dict(sorted(self.counts.items())),
            "match_methods": {
                "sakarma_bodies": dict(sorted(self.sakarma_methods.items())),
                "sulekha_year_rows": dict(sorted(self.sulekha_methods.items())),
            },
            "meetings_per_year": dict(self.meetings_per_year),
            "bodies_without_election_result": list(self.no_election_result),
            "inputs": [_input_entry(path) for path in self.inputs if path.exists()],
            "notes": list(self.notes),
        }

    def manifest(self) -> dict[str, Any]:
        body = self._body()
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        return {"generated_at": _stamp(), "fingerprint": digest, **body}


def assess(result: CrosswalkResult, source: Source, *, inputs: Sequence[Path] = ()) -> Quality:
    """Turn one crosswalk run into gates and coverage. Pure: no database, no files.

    Everything the gate needs is already in ``result`` and ``source``, which is
    what lets each failure mode be stated in a handful of rows rather than
    reproduced on a five-gigabyte restore.
    """
    spine = result.spine
    bodies = len(spine)
    geometry = sum(1 for r in spine if r["lb_code"] in source.geometry)
    unelected = tuple(
        f"{r['lb_name_en'] or r['lb_name_ml']} ({r['lb_code']})"
        for r in spine
        if not is_in_elections(r)
    )
    meetings_per_year = _per_year(source.meetings)

    return Quality(
        gates=tuple(_gates(result, source)),
        problems=tuple(result.problems),
        counts={
            "bodies": bodies,
            "bodies_with_geometry": geometry,
            "bodies_with_meetings": len(result.sakarma_matches),
            "bodies_without_election_result": len(unelected),
            "sakarma_bodies": result.sakarma_total,
            "sulekha_year_rows": result.sulekha_total,
            "meetings": source.meetings_total,
            "project_rows": source.projects_total,
        },
        sakarma_methods=dict(result.methods),
        sulekha_methods=dict(result.year_methods),
        meetings_per_year=meetings_per_year,
        no_election_result=unelected,
        notes=_notes(bodies, geometry, unelected),
        inputs=tuple(inputs),
    )


def _gates(result: CrosswalkResult, source: Source) -> list[Gate]:
    """The five things the build refuses to be wrong about, in failing order."""
    spine = result.spine
    resolved_years = {(row[1], str(row[2])) for row in result.sulekha_rows}
    resolved_sakarma = {str(sk["id"]) for sk, _, _, _ in result.sakarma_matches}

    orphan_projects = sum(
        rows for lb_id, year, rows in source.projects if (year, lb_id) not in resolved_years
    )
    # A project whose local body or district row is missing never reaches the
    # aggregate at all, so the total is compared as well as the join.
    orphan_projects += max(0, source.projects_total - sum(r for _, _, r in source.projects))
    orphan_meetings = sum(
        rows for lb_id, _, rows in source.meetings if lb_id not in resolved_sakarma
    )
    orphan_meetings += max(0, source.meetings_total - sum(r for _, _, r in source.meetings))

    duplicates = sorted(code for code, n in Counter(r["lb_code"] for r in spine).items() if n > 1)
    incomplete = [r["lb_code"] for r in spine if not r.get("district_name") or not r.get("lb_type")]

    return [
        Gate(
            name="every sakarma body resolves to a local body",
            checked=result.sakarma_total,
            failed=len(result.sakarma_unmatched),
            detail=_named(str(r.get("name_ml")) for r in result.sakarma_unmatched),
        ),
        Gate(
            name="every sulekha year-row resolves to a local body",
            checked=result.sulekha_total,
            failed=len(result.sulekha_unmatched),
            detail=_named(
                f"{r.get('year_label')} {r.get('lb_name')}" for r in result.sulekha_unmatched
            ),
        ),
        Gate(
            name="every src_sulekha.projects row joins to an lb_key",
            checked=source.projects_total,
            failed=orphan_projects,
            detail=_named(
                f"{year} {lb_id}"
                for lb_id, year, _ in source.projects
                if (year, lb_id) not in resolved_years
            ),
        ),
        Gate(
            name="every sakarma.meeting_manifest row joins to an lb_key",
            checked=source.meetings_total,
            failed=orphan_meetings,
            detail=_named(
                lb_id for lb_id, _, _ in source.meetings if lb_id not in resolved_sakarma
            ),
        ),
        Gate(
            name="no two bodies share an lb_code",
            checked=len(spine),
            failed=len(duplicates),
            detail=_named(duplicates),
        ),
        Gate(
            name="every body has a district and a tier",
            checked=len(spine),
            failed=len(incomplete),
            detail=_named(incomplete),
        ),
    ]


# ---------------------------------------------------------------- the outputs


def write(quality: Quality, paths: Paths) -> list[Path]:
    """Write the report and the manifest. Only reached once the gate has passed."""
    paths.out.mkdir(parents=True, exist_ok=True)
    report = paths.out / "quality_report.txt"
    manifest = paths.out / "manifest.json"
    report.write_text(quality.render(), encoding="utf-8")
    manifest.write_text(
        json.dumps(quality.manifest(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return [report, manifest]


def record_build(
    db: Database,
    target: Target,
    quality: Quality,
    *,
    dumps: Sequence[str] = SOURCE_DUMPS,
    dataset: str = DATASET_NAME,
) -> dict[str, Any]:
    """Write the one row of ``core.build_manifest``. Returns what it wrote.

    R9 asks that every figure on the site trace to a named dataset and a build
    date. The API had been taking that date from a constant it could be handed
    by an environment variable, so the site could state a date the data did not
    have -- provenance held by discipline rather than by construction. Here the
    date is written by the run that produced the tables, from the same
    connection, inside the same swap: it cannot describe a build that did not
    happen.

    The counts are read back from the tables rather than passed in, so the row
    states what is in the database and not what the build believed it put there.
    """
    counts = {
        "bodies": int(db.scalar(f"SELECT count(*) FROM {target.core}.local_body") or 0),
        "projects": int(db.scalar(f"SELECT count(*) FROM {target.finance}.project") or 0),
        "meetings": int(db.scalar(f"SELECT count(*) FROM {target.meetings}.meeting") or 0),
        "candidates": int(db.scalar(f"SELECT count(*) FROM {target.elections}.candidate") or 0),
    }
    row = {
        "dataset": dataset,
        "built_at": datetime.now(UTC),
        "master_version": __version__,
        "fingerprint": quality.manifest()["fingerprint"],
        "source_dumps": list(dumps),
        **counts,
    }
    columns = ", ".join(row)
    db.execute(
        f"INSERT INTO {target.core}.build_manifest ({columns}) "
        f"VALUES ({', '.join(['%s'] * len(row))})",
        list(row.values()),
    )
    return row


def declared_inputs(paths: Paths) -> list[Path]:
    """The files a run's result depends on, hashed into the manifest."""
    return [paths.overrides, paths.sec_registry_cache]


def gate(db: Database, paths: Paths, *, result: CrosswalkResult) -> Quality:
    """Assess one resolved crosswalk against the sources it came from."""
    return assess(result, read_source(db), inputs=declared_inputs(paths))


# ------------------------------------------------------------------- plumbing


def _named(items: Iterable[str], limit: int = NAMED) -> str:
    listed = [item for item in items][: limit + 1]
    if not listed:
        return ""
    if len(listed) > limit:
        return ", ".join(listed[:limit]) + ", ..."
    return ", ".join(listed)


def _per_year(rows: Iterable[tuple[str, str, int]]) -> tuple[tuple[str, int], ...]:
    tally: Counter[str] = Counter()
    for _, year, count in rows:
        tally[year] += count
    return tuple(sorted(tally.items()))


def _listed(names: Sequence[str]) -> str:
    return f" -- {', '.join(names)}" if names else ""


def _methods(methods: dict[str, int]) -> str:
    if not methods:
        return "none"
    return ", ".join(f"{name} {count:,}" for name, count in sorted(methods.items()))


def _fuzzy(methods: dict[str, int], total: int) -> str:
    similarity = methods.get("similarity", 0)
    if not total:
        return "0 of 0"
    return f"{similarity:,} of {total:,} ({similarity / total * 100:.1f}%)"


def _notes(bodies: int, geometry: int, unelected: Sequence[str]) -> tuple[str, ...]:
    """Limitations stated from the numbers, so they cannot go stale in prose."""
    notes = [
        f"Geometry covers {geometry:,} of {bodies:,} bodies. A KSMART limit, not a build error.",
        "finance.lb_year_continuity is name recurrence, not a source flag, and is "
        "unvalidated. Measure its false-match rate before any page presents it as fact.",
    ]
    if unelected:
        notes.insert(
            1,
            f"No published election result, so in_elections = false: "
            f"{', '.join(unelected)}. Finances and meetings are complete for "
            f"{'it' if len(unelected) == 1 else 'them'}; only the elections page "
            "has nothing to show.",
        )
    return tuple(notes)


def _stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _input_entry(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return {"path": path.name, "sha256": digest.hexdigest(), "bytes": path.stat().st_size}
