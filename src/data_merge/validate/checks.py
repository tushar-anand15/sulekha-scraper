"""Reusable assertions.

Each check records a pass or a failure with enough context to act on -- the
expected value, what was actually seen, and which check it was. Checks
accumulate rather than raising one at a time, so a run reports *every* way it
disagrees with expectation instead of only the first.

The run still fails; accumulating just makes the diagnosis complete.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final


class CheckError(AssertionError):
    """One or more expectations were not met."""


@dataclass(frozen=True, slots=True)
class CheckResult:
    """The outcome of a single assertion."""

    name: str
    ok: bool
    expected: object
    actual: object
    detail: str = ""

    def __str__(self) -> str:
        mark = "ok  " if self.ok else "FAIL"
        line = f"{mark} {self.name}: expected {self.expected!r}, got {self.actual!r}"
        return f"{line} -- {self.detail}" if self.detail else line


@dataclass
class Checks:
    """A run's accumulated assertions."""

    label: str
    results: list[CheckResult] = field(default_factory=list)

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if not r.ok]

    @property
    def ok(self) -> bool:
        return not self.failures

    def record(self, name: str, *, expected: object, actual: object, detail: str = "") -> bool:
        passed = expected == actual
        self.results.append(
            CheckResult(name=name, ok=passed, expected=expected, actual=actual, detail=detail)
        )
        return passed

    def equals(self, name: str, expected: object, actual: object, detail: str = "") -> bool:
        return self.record(name, expected=expected, actual=actual, detail=detail)

    def at_least(self, name: str, minimum: float, actual: float, detail: str = "") -> bool:
        passed = actual >= minimum
        self.results.append(
            CheckResult(
                name=name,
                ok=passed,
                expected=f">= {minimum}",
                actual=actual,
                detail=detail,
            )
        )
        return passed

    def is_empty(self, name: str, items: Iterable[object], detail: str = "") -> bool:
        listed = list(items)
        self.results.append(
            CheckResult(
                name=name,
                ok=not listed,
                expected="nothing",
                actual=f"{len(listed)} item(s): {listed[:5]}" if listed else "nothing",
                detail=detail,
            )
        )
        return not listed

    def raise_if_failed(self) -> None:
        """Abort the build. Called before anything is written, never after.

        A half-written year on disk is worse than no year at all: it looks like
        a successful run to everything downstream.
        """
        if self.ok:
            return
        lines = "\n".join(f"  {result}" for result in self.failures)
        raise CheckError(
            f"{self.label}: {len(self.failures)} of {len(self.results)} checks failed\n{lines}"
        )

    def summary(self) -> str:
        passed = len(self.results) - len(self.failures)
        return f"{self.label}: {passed}/{len(self.results)} checks passed"


# ---------------------------------------------------------------------------
# Shape assertions over canonical candidate rows
# ---------------------------------------------------------------------------


def count_distinct(rows: Iterable[Mapping[str, str]], column: str) -> int:
    return len({row.get(column, "") for row in rows})


def count_by(rows: Iterable[Mapping[str, str]], key: str, value: str) -> dict[str, int]:
    """Distinct ``value`` per ``key`` -- e.g. distinct local bodies per lb_type.

    Counts distinct values, so "941 Grama Panchayats" means 941 bodies
    rather than 941 candidates standing in them.
    """
    grouped: dict[str, set[str]] = {}
    for row in rows:
        grouped.setdefault(row.get(key, ""), set()).add(row.get(value, ""))
    return {key_name: len(values) for key_name, values in sorted(grouped.items())}


WON: Final = "won"
NO_RESULT: Final = "no result"
"""A published state: 2015 and 2020 each carry a handful of wards where the
feed reports no outcome at all. Such a ward is excluded from the
one-winner rule rather than counted as a failure -- the shipped data-quality
reports draw the same distinction."""


def wards_without_exactly_one_winner(
    rows: Iterable[Mapping[str, str]], *, winner_status: str = WON
) -> list[str]:
    """Contested wards whose winner count is not exactly one.

    Both directions matter. Zero winners means a ward silently lost its result;
    two means a tie was resolved by picking twice.
    """
    tally: Counter[str] = Counter()
    contested: set[str] = set()
    for row in rows:
        if row.get("status") == NO_RESULT:
            continue
        contested.add(row.get("ward_code", ""))
        if row.get("status") == winner_status:
            tally[row.get("ward_code", "")] += 1
    return sorted(ward for ward in contested if tally[ward] != 1)


def rows_missing(rows: Iterable[Mapping[str, str]], column: str) -> int:
    return sum(1 for row in rows if not row.get(column))


def column_values(rows: Iterable[Mapping[str, str]], column: str) -> Counter[str]:
    return Counter(row.get(column, "") for row in rows)
