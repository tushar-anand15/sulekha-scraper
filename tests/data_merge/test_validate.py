"""The expectation gate.

The load-bearing test here is the corrupted-expectation one: a gate that never
fails is indistinguishable from no gate, and this pipeline exists because two
defects sailed past exactly that kind of non-check.
"""

from __future__ import annotations

import dataclasses

import pytest

from data_merge.schema import blank_row
from data_merge.spec import Expectations, spec_for
from data_merge.validate.checks import (
    CheckError,
    Checks,
    count_by,
    wards_without_exactly_one_winner,
)
from data_merge.validate.expectations import check_year


def _rows(spec_year: int = 2010) -> list[dict[str, str]]:
    """A miniature but structurally complete year: 2 local bodies, 3 wards."""
    built: list[dict[str, str]] = []
    plan = [
        ("Grama Panchayat", "G01001", "G01001001", 2),
        ("Grama Panchayat", "G01001", "G01001002", 2),
        ("Block Panchayat", "B01001", "B01001001", 2),
    ]
    for lb_type, lb_code, ward_code, n in plan:
        for index in range(n):
            row = blank_row()
            row.update(
                district_code="D01001",
                district_name="THIRUVANANTHAPURAM",
                lb_type=lb_type,
                lb_code=lb_code,
                ward_code=ward_code,
                ward_no=ward_code[6:],
                party_name="INC",
                party_group="UDF",
                party_group_source="mapped_2010" if spec_year == 2010 else "published",
                candidate_gender="F" if index else "M",
                gender_source="pdf",
                candidate_name=f"CAND {ward_code} {index}",
                status="won" if index == 0 else "lost",
                total_votes=str(500 - index),
                invalid_votes="12",
            )
            built.append(row)
    return built


def _spec_matching(rows: list[dict[str, str]], year: int = 2010):
    """A spec whose expectations describe exactly the rows given."""
    base = spec_for(year)
    return dataclasses.replace(
        base,
        expect=Expectations(
            candidates=len(rows),
            wards=len({r["ward_code"] for r in rows}),
            local_bodies=len({r["lb_code"] for r in rows}),
            local_bodies_by_type=count_by(rows, "lb_type", "lb_code"),
            gendered_rows=sum(1 for r in rows if r["candidate_gender"]),
            invalid_vote_rows=None,
        ),
    )


class TestGatePasses:
    def test_a_year_matching_its_expectations_passes_every_check(self) -> None:
        rows = _rows()
        result = check_year(_spec_matching(rows), rows)
        assert result.ok, result.summary()
        result.raise_if_failed()


class TestGateGates:
    """Proving the gate actually gates."""

    @pytest.mark.parametrize(
        ("field", "wrong"),
        [
            ("candidates", 999),
            ("wards", 2),
            ("local_bodies", 5),
            ("gendered_rows", 1),
        ],
    )
    def test_a_corrupted_expectation_fails_the_build_naming_the_check(
        self, field: str, wrong: int
    ) -> None:
        rows = _rows()
        spec = _spec_matching(rows)
        spec = dataclasses.replace(
            spec, expect=dataclasses.replace(spec.expect, **{field: wrong})
        )

        result = check_year(spec, rows)
        assert not result.ok
        with pytest.raises(CheckError, match=field):
            result.raise_if_failed()

    def test_a_corrupted_local_body_type_count_fails(self) -> None:
        """The plan's example: a Grama Panchayat count of 977 instead of 978."""
        rows = _rows()
        spec = _spec_matching(rows)
        wrong = dict(spec.expect.local_bodies_by_type)
        wrong["Grama Panchayat"] += 1
        spec = dataclasses.replace(
            spec, expect=dataclasses.replace(spec.expect, local_bodies_by_type=wrong)
        )
        with pytest.raises(CheckError, match="local_bodies_by_type"):
            check_year(spec, rows).raise_if_failed()

    def test_every_failure_is_reported_not_just_the_first(self) -> None:
        rows = _rows()
        spec = _spec_matching(rows)
        spec = dataclasses.replace(
            spec,
            expect=dataclasses.replace(spec.expect, candidates=999, wards=99, local_bodies=9),
        )
        result = check_year(spec, rows)
        assert len(result.failures) >= 3


class TestStructuralChecks:
    def test_a_ward_with_two_winners_is_caught(self) -> None:
        rows = _rows()
        rows[1]["status"] = "won"  # same ward as rows[0]
        with pytest.raises(CheckError, match="one_winner_per_ward"):
            check_year(_spec_matching(rows), rows).raise_if_failed()

    def test_a_ward_with_no_winner_is_caught(self) -> None:
        rows = _rows()
        rows[0]["status"] = "lost"
        assert wards_without_exactly_one_winner(rows) == ["G01001001"]

    def test_a_ward_the_feed_reports_as_no_result_is_excluded_not_failed(self) -> None:
        """2015 and 2020 each carry a few. It is a published, deliberate
        state, and the shipped data-quality reports exclude it too."""
        rows = _rows()
        for row in rows:
            if row["ward_code"] == "G01001001":
                row["status"] = "no result"
        assert wards_without_exactly_one_winner(rows) == []

    def test_a_gender_without_a_source_is_caught(self) -> None:
        rows = _rows()
        rows[0]["gender_source"] = ""
        with pytest.raises(CheckError, match="gender_source_present"):
            check_year(_spec_matching(rows), rows).raise_if_failed()

    def test_an_authored_front_claiming_published_is_caught(self) -> None:
        """2010's front is authored; ``published`` must be unreachable for it."""
        rows = _rows()
        rows[0]["party_group_source"] = "published"
        with pytest.raises(CheckError, match="party_group_source"):
            check_year(_spec_matching(rows), rows).raise_if_failed()

    def test_a_published_year_claiming_mapped_2010_is_caught(self) -> None:
        rows = _rows(spec_year=2015)
        rows[0]["party_group_source"] = "mapped_2010"
        with pytest.raises(CheckError, match="published_front_has_no_mapped_rows"):
            check_year(_spec_matching(rows, 2015), rows).raise_if_failed()

    def test_a_cycle_declaring_no_invalid_votes_may_not_carry_them(self) -> None:
        rows = _rows(spec_year=2020)
        for row in rows:
            row["invalid_votes"] = "12"
        with pytest.raises(CheckError, match="invalid_votes_absent"):
            check_year(_spec_matching(rows, 2020), rows).raise_if_failed()

    def test_a_cycle_declaring_invalid_votes_must_carry_them(self) -> None:
        rows = _rows()
        for row in rows:
            row["invalid_votes"] = ""
        with pytest.raises(CheckError, match="invalid_votes_present"):
            check_year(_spec_matching(rows), rows).raise_if_failed()

    def test_a_row_off_the_canonical_schema_is_caught(self) -> None:
        rows = _rows()
        del rows[0]["lb_name_mal"]
        with pytest.raises(CheckError, match="columns"):
            check_year(_spec_matching(rows), rows).raise_if_failed()


class TestCheckReporting:
    def test_the_summary_counts_passes_and_total(self) -> None:
        checks = Checks(label="2015")
        checks.equals("a", 1, 1)
        checks.equals("b", 1, 2)
        assert checks.summary() == "2015: 1/2 checks passed"

    def test_a_failure_message_carries_expected_and_actual(self) -> None:
        checks = Checks(label="2015")
        checks.equals("candidates", 75_251, 75_250)
        with pytest.raises(CheckError) as excinfo:
            checks.raise_if_failed()
        assert "75251" in str(excinfo.value).replace(",", "")
        assert "75250" in str(excinfo.value).replace(",", "")
