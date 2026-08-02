"""Cell-level comparison against the shipped files.

Unit tests cannot prove that four years of assembly reproduce known-good data;
only a comparison against the files that actually shipped can. But a bare
"files differ" is useless at 75,000 rows, and a comparison that tolerates
anything is worse than none.

So the contract is: **every difference must be named in advance.** A build may
differ from the shipped file only in cells an allow-list explains. Anything
else -- an extra row, a missing row, a changed cell nobody predicted -- is a
failure, and the report names the row and column so it can be looked at.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AllowedDifference:
    """One predicted difference, with the reason it is expected.

    ``reason`` is not decoration. An allow-list entry without a stated cause is
    how a real regression gets waved through six months later.
    """

    column: str
    reason: str
    max_cells: int | None = None
    """Cap on how many cells may differ in this column. ``None`` means any
    number, which should be rare -- a cap turns "we changed a few" into a
    checked assertion, not just a hope."""


@dataclass
class Difference:
    key: str
    column: str
    shipped: str
    rebuilt: str


@dataclass
class GoldenResult:
    """The outcome of comparing a rebuilt year against its shipped file."""

    shipped_rows: int = 0
    rebuilt_rows: int = 0
    only_in_shipped: list[str] = field(default_factory=list)
    only_in_rebuilt: list[str] = field(default_factory=list)
    allowed: dict[str, int] = field(default_factory=dict)
    unexplained: list[Difference] = field(default_factory=list)
    over_cap: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.unexplained and not self.over_cap

    def summary(self) -> str:
        return (
            f"shipped {self.shipped_rows:,} vs rebuilt {self.rebuilt_rows:,} rows; "
            f"only-in-shipped {len(self.only_in_shipped)}, "
            f"only-in-rebuilt {len(self.only_in_rebuilt)}; "
            f"allowed {self.allowed}; unexplained {len(self.unexplained)}"
        )

    def describe(self, limit: int = 10) -> str:
        lines = [self.summary()]
        for diff in self.unexplained[:limit]:
            lines.append(
                f"  {diff.key} [{diff.column}] shipped={diff.shipped!r} rebuilt={diff.rebuilt!r}"
            )
        if len(self.unexplained) > limit:
            lines.append(f"  ... and {len(self.unexplained) - limit} more")
        for column in self.over_cap:
            lines.append(f"  column {column!r} exceeded its allowed cell cap")
        return "\n".join(lines)


def read_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def row_key(row: Mapping[str, str]) -> str:
    """Identify a candidate row across two independent builds.

    Ward plus votes plus name: ward alone is not unique (several candidates),
    and (ward, votes) collides where two candidates poll identically -- which
    really happens, so the name has to be in the key.
    """
    return f"{row['ward_code']}|{row['total_votes']}|{row.get('candidate_name', '')}"


def compare(
    shipped: Sequence[Mapping[str, str]],
    rebuilt: Sequence[Mapping[str, str]],
    *,
    allow: Iterable[AllowedDifference] = (),
    ignore_rows: Iterable[str] = (),
) -> GoldenResult:
    """Compare two candidate row sets cell by cell.

    ``ignore_rows`` lists keys expected to exist on only one side -- the
    recovered 2010 candidate, for instance -- so a known row-count change does
    not drown the real signal.
    """
    allowed_by_column = {entry.column: entry for entry in allow}
    skip = set(ignore_rows)

    shipped_by_key = {row_key(row): row for row in shipped}
    rebuilt_by_key = {row_key(row): row for row in rebuilt}

    result = GoldenResult(shipped_rows=len(shipped), rebuilt_rows=len(rebuilt))
    result.only_in_shipped = sorted(set(shipped_by_key) - set(rebuilt_by_key) - skip)
    result.only_in_rebuilt = sorted(set(rebuilt_by_key) - set(shipped_by_key) - skip)

    for key in sorted(set(shipped_by_key) & set(rebuilt_by_key)):
        if key in skip:
            continue
        left, right = shipped_by_key[key], rebuilt_by_key[key]
        for column in left:
            if left[column] == right.get(column, ""):
                continue
            if column in allowed_by_column:
                result.allowed[column] = result.allowed.get(column, 0) + 1
                continue
            result.unexplained.append(
                Difference(
                    key=key,
                    column=column,
                    shipped=left[column],
                    rebuilt=right.get(column, ""),
                )
            )

    result.over_cap = sorted(
        column
        for column, count in result.allowed.items()
        if (cap := allowed_by_column[column].max_cells) is not None and count > cap
    )
    return result
