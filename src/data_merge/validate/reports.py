"""Human-readable reports: what the build checked, and what it could not.

Two reports per cycle, in the format the existing files established -- a
numbered data-quality report and a merge report.

The unusual requirement is the second half of the first sentence. A report that
silently omits a check it could not run reads exactly like a report where the
check passed. 2010 has no second vote source, so cross-endpoint agreement is
not merely unmeasured but *structurally impossible*, and the report has to say
so in the place the check would otherwise appear. Anything less overstates what
the data supports.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from data_merge.spec import Front, Spine, YearSpec
from data_merge.validate.checks import CheckResult, Checks

_WIDTH = 62


class Status:
    PASS = "PASS"
    FAIL = "FAIL"
    INFO = "INFO"
    NOT_RUNNABLE = "N/A "
    """Not a pass and not a failure: the check cannot exist for this cycle."""


@dataclass(frozen=True, slots=True)
class Line:
    status: str
    label: str
    detail: str = ""

    def render(self) -> str:
        head = f"  [{self.status}] {self.label}"
        return f"{head.ljust(_WIDTH)} {self.detail}".rstrip() if self.detail else head.rstrip()


@dataclass
class Section:
    title: str
    lines: list[Line] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def render(self, number: int) -> str:
        body = [f"{number}. {self.title}"]
        body += [line.render() for line in self.lines]
        body += [f"    {note}" for note in self.notes]
        return "\n".join(body)


@dataclass
class Report:
    """A numbered report, rendered in the format the existing files established."""

    title: str
    sections: list[Section] = field(default_factory=list)
    preamble: list[str] = field(default_factory=list)

    def section(self, title: str) -> Section:
        created = Section(title=title)
        self.sections.append(created)
        return created

    def render(self) -> str:
        out = [self.title, "=" * _WIDTH]
        out += self.preamble
        if self.preamble:
            out.append("")
        for number, section in enumerate(self.sections, start=1):
            out.append(section.render(number))
            out.append("")
        return "\n".join(out).rstrip() + "\n"


def data_quality_report(spec: YearSpec, checks: Checks) -> Report:
    """Render the expectation gate's results, plus what this cycle cannot check."""
    report = Report(title=f"Kerala Local Body Elections {spec.year} - data quality report")

    counts = report.section("Structural expectations")
    counts.lines.extend(_line_for(result) for result in checks.results)

    unrunnable = report.section("Checks that cannot run for this cycle")
    not_runnable = _unrunnable_checks(spec)
    if not_runnable:
        unrunnable.lines.extend(
            Line(status=Status.NOT_RUNNABLE, label=label, detail=reason)
            for label, reason in not_runnable
        )
    else:
        unrunnable.lines.append(
            Line(status=Status.INFO, label="none", detail="every check applies to this cycle")
        )

    if spec.expect.notes:
        known = report.section("Known limitations recorded in the spec")
        known.notes.extend(spec.expect.notes)

    return report


def _line_for(result: CheckResult) -> Line:
    if result.ok:
        return Line(status=Status.PASS, label=result.name, detail=f"actual={result.actual!r}")
    return Line(
        status=Status.FAIL,
        label=result.name,
        detail=f"actual={result.actual!r} expected={result.expected!r}",
    )


def _unrunnable_checks(spec: YearSpec) -> list[tuple[str, str]]:
    """Checks this cycle structurally cannot support, with the reason.

    Named explicitly rather than omitted. An absent section is indistinguishable
    from a section that passed.
    """
    missing: list[tuple[str, str]] = []
    if spec.spine is Spine.PDF:
        missing.append(
            (
                "cross-endpoint agreement",
                "no second 2010 vote source exists; the PDF is the only "
                "candidate-level feed, so vote counts are single-sourced",
            )
        )
    if not spec.has_detail:
        # The reason differs by cycle and the distinction is the point: 2025's
        # front is inferred from the party's group, 2010's is hand-assembled
        # from documentary evidence. Calling both "derived" would misdescribe
        # exactly the provenance this column exists to record.
        because = (
            "the front is authored from documentary evidence and stamped "
            "party_group_source=mapped_2010"
            if spec.front is Front.AUTHORED
            else "the front is derived from the party's group"
        )
        missing.append(
            (
                "published front per candidate",
                f"no detailed-results feed exists for this cycle, so {because}",
            )
        )
    if not spec.has_roster:
        missing.append(
            (
                "no-result wards recovered from the roster",
                "this cycle publishes no ward roster, so wards with no result "
                "cannot be distinguished from wards absent from the feed",
            )
        )
    if not spec.has_invalid_votes:
        missing.append(
            (
                "invalid-votes row present in every contested ward",
                "this cycle publishes no invalid-votes row at all",
            )
        )
    return missing


def merge_report(
    spec: YearSpec,
    *,
    sections: Sequence[tuple[str, Iterable[str]]],
) -> Report:
    """Render a merge report from pre-computed sections.

    The builder knows what it matched and what it rejected; this only formats
    it. Keeping the arithmetic in the builder and the formatting here is what
    stops a report from quietly recomputing -- and disagreeing with -- the
    numbers the build actually used.
    """
    report = Report(title=f"{spec.members.value.upper()} -> spine merge report {spec.year}")
    for title, body in sections:
        created = report.section(title)
        created.notes.extend(body)
    return report
