"""Gate a year's output against the numbers declared in its spec.

The numbers themselves live in ``spec.py`` beside everything else that differs
per cycle, each with a note recording why it is what it is. This module is the
gate that applies them: it runs before anything is written, and a mismatch
stops the build.

This is the direct answer to defects that hid because nothing asserted shape.
A parser bug that fabricates 261 rows or drops 638 wards changes a count. If a
count is checked, the bug surfaces the same day rather than months later
against an unrelated source.
"""

from __future__ import annotations

from collections.abc import Sequence

from data_merge.schema import SCHEMA, CandidateRow
from data_merge.spec import Front, YearSpec
from data_merge.validate.checks import (
    Checks,
    count_by,
    count_distinct,
    rows_missing,
    wards_without_exactly_one_winner,
)


def check_year(spec: YearSpec, rows: Sequence[CandidateRow]) -> Checks:
    """Every structural expectation for one cycle.

    Returns the accumulated results; the caller decides when to abort, so a
    diagnostic run can report everything wrong at once.
    """
    checks = Checks(label=f"{spec.year}")
    expect = spec.expect

    checks.equals("candidates", expect.candidates, len(rows))
    checks.equals("wards", expect.wards, count_distinct(rows, "ward_code"))
    checks.equals("local_bodies", expect.local_bodies, count_distinct(rows, "lb_code"))
    checks.equals(
        "local_bodies_by_type",
        dict(sorted(expect.local_bodies_by_type.items())),
        count_by(rows, "lb_type", "lb_code"),
    )
    checks.equals(
        "gendered_rows",
        expect.gendered_rows,
        len(rows) - rows_missing(rows, "candidate_gender"),
    )

    # Every gendered row must say where the value came from. A gender with no
    # provenance cannot be audited, and provenance is how the reserved-ward
    # rule was shown to beat the honorific.
    gendered_without_source = sum(
        1 for row in rows if row.get("candidate_gender") and not row.get("gender_source")
    )
    checks.equals("gender_source_present", 0, gendered_without_source)

    # Asserted as an exact count: a tie no source can break should leave the
    # ward without a winner rather than acquire an invented one, but the
    # number of such wards is itself a fact worth pinning, so a second one
    # appearing fails the build instead of blending into an allowance.
    winnerless = wards_without_exactly_one_winner(rows)
    checks.equals(
        "one_winner_per_ward",
        expect.wards_without_winner,
        len(winnerless),
        detail=f"wards whose winner count is not exactly 1: {winnerless[:5]}",
    )

    _check_columns(checks, rows)
    _check_front_provenance(checks, spec, rows)
    _check_invalid_votes(checks, spec, rows)
    return checks


def _check_columns(checks: Checks, rows: Sequence[CandidateRow]) -> None:
    """All four years must stack, so the column set is identical or nothing is."""
    if not rows:
        checks.equals("columns", list(SCHEMA), [])
        return
    offenders = [i for i, row in enumerate(rows) if tuple(row.keys()) != SCHEMA]
    checks.equals(
        "columns",
        0,
        len(offenders),
        detail=f"rows whose columns differ from the schema (first: {offenders[:3]})",
    )


def _check_front_provenance(checks: Checks, spec: YearSpec, rows: Sequence[CandidateRow]) -> None:
    """``party_group_source`` keeps its meaning across cycles.

    ``published`` means a source asserted the front. 2010's front is authored
    from documentary evidence, so it may never claim ``published`` -- that
    distinction is the whole reason the column exists.
    """
    sources = {row.get("party_group_source", "") for row in rows}
    if spec.front is Front.AUTHORED:
        checks.equals("party_group_source", {"mapped_2010"}, sources)
        checks.is_empty(
            "authored_front_never_published",
            [s for s in sources if s == "published"],
            detail="an authored front may never claim to be published",
        )
    elif spec.front is Front.PUBLISHED:
        checks.is_empty(
            "published_front_has_no_mapped_rows",
            [s for s in sources if s == "mapped_2010"],
            detail="only 2010 may be mapped",
        )


def _check_invalid_votes(checks: Checks, spec: YearSpec, rows: Sequence[CandidateRow]) -> None:
    """Declared in the spec, so its absence is intentional rather than incidental."""
    populated = len(rows) - rows_missing(rows, "invalid_votes")
    if spec.has_invalid_votes:
        checks.at_least(
            "invalid_votes_present",
            1,
            populated,
            detail="this cycle publishes invalid votes, so the column cannot be empty",
        )
    else:
        checks.equals(
            "invalid_votes_absent",
            0,
            populated,
            detail="this cycle publishes no invalid-votes row at all",
        )
