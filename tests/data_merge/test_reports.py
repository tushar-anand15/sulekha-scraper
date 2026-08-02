"""Report rendering.

The tests that matter here are about *absence*: a check that cannot run for a
cycle must appear in the report saying so, because an omitted section reads
exactly like one that passed.
"""

from __future__ import annotations

from data_merge.spec import spec_for
from data_merge.validate.checks import Checks
from data_merge.validate.reports import Status, data_quality_report, merge_report


def _checks(label: str = "2015") -> Checks:
    checks = Checks(label=label)
    checks.equals("candidates", 75_251, 75_251)
    checks.equals("wards", 21_863, 21_863)
    return checks


class TestDataQualityReport:
    def test_passing_checks_render_as_pass_lines(self) -> None:
        text = data_quality_report(spec_for(2015), _checks()).render()
        assert "[PASS] candidates" in text
        assert "[FAIL]" not in text

    def test_a_failing_check_renders_expected_and_actual(self) -> None:
        checks = Checks(label="2015")
        checks.equals("candidates", 75_251, 75_250)
        text = data_quality_report(spec_for(2015), checks).render()
        assert "[FAIL] candidates" in text
        assert "75251" in text.replace(",", "")
        assert "75250" in text.replace(",", "")

    def test_the_title_names_the_year(self) -> None:
        text = data_quality_report(spec_for(2020), _checks("2020")).render()
        assert "Kerala Local Body Elections 2020" in text

    def test_sections_are_numbered(self) -> None:
        text = data_quality_report(spec_for(2015), _checks()).render()
        assert "1. Structural expectations" in text
        assert "2. Checks that cannot run for this cycle" in text


class TestUnrunnableChecksAreStated:
    def test_2010_states_that_cross_endpoint_agreement_cannot_run(self) -> None:
        """The load-bearing case: 2010 has no second vote source.

        Omitting the check would let a reader assume vote counts were
        corroborated. They are single-sourced, and the report must say so.
        """
        text = data_quality_report(spec_for(2010), _checks("2010")).render()
        assert "cross-endpoint agreement" in text
        assert "single-sourced" in text
        assert Status.NOT_RUNNABLE.strip() in text

    def test_2015_does_not_claim_cross_endpoint_agreement_is_unrunnable(self) -> None:
        text = data_quality_report(spec_for(2015), _checks()).render()
        assert "cross-endpoint agreement" not in text

    def test_2025_states_that_its_front_is_derived_not_published(self) -> None:
        text = data_quality_report(spec_for(2025), _checks("2025")).render()
        assert "published front per candidate" in text
        assert "derived from the party's group" in text

    def test_2010_says_its_front_is_authored_not_merely_derived(self) -> None:
        """Authored and derived are different provenances, and the column exists
        precisely to record which one a row has."""
        text = data_quality_report(spec_for(2010), _checks("2010")).render()
        assert "authored from documentary evidence" in text
        assert "mapped_2010" in text
        assert "derived from the party's group" not in text

    def test_2025_states_it_has_no_roster(self) -> None:
        text = data_quality_report(spec_for(2025), _checks("2025")).render()
        assert "no-result wards recovered from the roster" in text

    def test_a_cycle_without_invalid_votes_says_so(self) -> None:
        text = data_quality_report(spec_for(2020), _checks("2020")).render()
        assert "invalid-votes row present in every contested ward" in text

    def test_2015_has_a_roster_a_detail_feed_and_invalid_votes(self) -> None:
        """The most complete cycle: nothing structural is missing."""
        text = data_quality_report(spec_for(2015), _checks()).render()
        assert "every check applies to this cycle" in text


class TestSpecNotesSurface:
    def test_the_specs_recorded_limitations_reach_the_report(self) -> None:
        text = data_quality_report(spec_for(2010), _checks("2010")).render()
        assert "Mattannur" in text
        assert "21,682" in text

    def test_2020_records_why_its_pdf_sex_column_is_unused(self) -> None:
        text = data_quality_report(spec_for(2020), _checks("2020")).render()
        assert "inverted at source" in text


class TestMergeReport:
    def test_sections_render_in_order_with_their_bodies(self) -> None:
        report = merge_report(
            spec_for(2015),
            sections=[
                ("Local body matching", ["SEC local bodies : 1199", "matched to LSGD  : 1192"]),
                ("Ward matching", ["SEC wards : 21865"]),
            ],
        )
        text = report.render()
        assert text.index("1. Local body matching") < text.index("2. Ward matching")
        assert "matched to LSGD  : 1192" in text

    def test_the_title_names_the_member_source_and_year(self) -> None:
        assert "WIYR" in merge_report(spec_for(2025), sections=[]).render()
        assert "2025" in merge_report(spec_for(2025), sections=[]).render()


def test_a_rendered_report_ends_with_exactly_one_newline() -> None:
    text = data_quality_report(spec_for(2015), _checks()).render()
    assert text.endswith("\n")
    assert not text.endswith("\n\n")
