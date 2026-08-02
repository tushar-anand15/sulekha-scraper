"""The 31-column schema and the per-year declarations."""

from __future__ import annotations

import pytest

from data_merge.schema import (
    OPTIONAL_COLUMNS,
    SCHEMA,
    SchemaError,
    blank_row,
    check_columns,
    conform,
)
from data_merge.spec import SPECS, YEARS, Front, PdfSex, Spine, spec_for

# The order is the contract, so it is written out literally here, independent
# of the module under test.
EXPECTED_ORDER = [
    "district_code", "district_name", "lb_type", "lb_code", "lb_name",
    "lb_name_mal", "ward_code", "ward_no", "ward_name", "ward_name_mal",
    "party_name", "party_group", "party_front", "party_group_source",
    "candidate_code", "candidate_title", "candidate_gender", "gender_source",
    "candidate_age", "candidate_name", "candidate_name_eng", "status",
    "total_votes", "invalid_votes", "ward_reservation", "candidate_role",
    "ward_winner_party", "ward_winner_party_group", "lb_ruling_front",
    "lb_control_type", "lb_head_party_group",
]  # fmt: skip


def test_schema_is_31_columns_in_the_documented_order() -> None:
    assert len(SCHEMA) == 31
    assert list(SCHEMA) == EXPECTED_ORDER
    assert len(set(SCHEMA)) == 31, "a duplicated column name would silently drop data"


def test_optional_columns_are_a_subset_of_the_schema() -> None:
    assert OPTIONAL_COLUMNS <= set(SCHEMA)


def test_blank_row_has_every_column_empty() -> None:
    row = blank_row()
    assert list(row) == list(SCHEMA)
    assert set(row.values()) == {""}


def test_conform_fills_missing_columns_as_empty_strings() -> None:
    row = conform({"lb_code": "G01001", "total_votes": "1204"})
    assert list(row) == list(SCHEMA)
    assert row["lb_code"] == "G01001"
    assert row["ward_name_mal"] == ""


def test_conform_turns_none_into_empty_string_not_the_word_none() -> None:
    row = conform({"candidate_age": None})
    assert row["candidate_age"] == ""


def test_conform_rejects_columns_outside_the_schema() -> None:
    with pytest.raises(SchemaError, match="outside the schema"):
        conform({"lb_code": "G01001", "candidate_phone": "999"})


def test_check_columns_accepts_the_canonical_order() -> None:
    check_columns(list(SCHEMA), origin="test")


def test_check_columns_reports_a_wrong_set() -> None:
    with pytest.raises(SchemaError, match="missing="):
        check_columns(list(SCHEMA)[:-1], origin="candidates_2015.csv")


def test_check_columns_rejects_the_right_set_in_the_wrong_order() -> None:
    shuffled = [SCHEMA[1], SCHEMA[0], *SCHEMA[2:]]
    with pytest.raises(SchemaError, match="wrong order"):
        check_columns(shuffled, origin="candidates_2015.csv")


class TestYearSpecs:
    def test_all_four_cycles_are_declared(self) -> None:
        assert YEARS == (2010, 2015, 2020, 2025)

    def test_2010_is_the_only_pdf_spine_and_the_only_authored_front(self) -> None:
        pdf_spine = [y for y, s in SPECS.items() if s.spine is Spine.PDF]
        authored = [y for y, s in SPECS.items() if s.front is Front.AUTHORED]
        assert pdf_spine == [2010]
        assert authored == [2010]

    def test_2010_declares_a_front_table_because_its_front_is_authored(self) -> None:
        spec = spec_for(2010)
        assert spec.front_table == "party_front_2010.csv"

    def test_2020_is_the_only_cycle_that_ignores_the_pdf_sex_column(self) -> None:
        """Its Sex column is inverted at source; gender comes from the contest feed."""
        ignoring = [y for y, s in SPECS.items() if s.pdf_sex is PdfSex.IGNORE]
        assert ignoring == [2020]

    def test_2015_is_the_only_cycle_labelling_the_bjp_front_bjp_plus(self) -> None:
        assert spec_for(2015).nda_label == "BJP+"
        assert {spec_for(y).nda_label for y in (2010, 2020, 2025)} == {"NDA"}

    def test_invalid_votes_are_declared_only_where_the_cycle_publishes_them(self) -> None:
        assert [y for y, s in SPECS.items() if s.has_invalid_votes] == [2010, 2015]

    def test_every_spec_declares_the_pdf_it_reads(self) -> None:
        for year, spec in SPECS.items():
            assert spec.pdf == f"candidates_GE{year}.pdf"

    def test_sec_spine_cycles_declare_a_sec_cache(self) -> None:
        for spec in SPECS.values():
            if spec.spine is Spine.SEC:
                assert spec.sec_cache, f"{spec.year} has an SEC spine but no cache"

    def test_expectations_are_internally_consistent(self) -> None:
        for year, spec in SPECS.items():
            expect = spec.expect
            assert sum(expect.local_bodies_by_type.values()) == expect.local_bodies, year
            assert expect.gendered_rows <= expect.candidates, year
            assert expect.wards < expect.candidates, year

    def test_spec_for_names_the_known_years_when_asked_for_an_unknown_one(self) -> None:
        with pytest.raises(KeyError, match="2005"):
            spec_for(2005)


def test_an_authored_front_without_a_front_table_is_rejected_at_construction() -> None:
    from data_merge.spec import Expectations, Members, YearSpec

    with pytest.raises(ValueError, match="authored front needs a front_table"):
        YearSpec(
            year=1999,
            spine=Spine.PDF,
            members=Members.NONE,
            front=Front.AUTHORED,
            pdf_sex=PdfSex.MEASURE,
            pdf="x.pdf",
            expect=Expectations(
                candidates=1, wards=1, local_bodies=1,
                local_bodies_by_type={"Grama Panchayat": 1}, gendered_rows=1,
            ),
        )
