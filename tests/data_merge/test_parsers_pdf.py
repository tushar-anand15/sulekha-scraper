"""The SEC candidate-report parser.

The two regression classes at the top of this file are the reason the module
exists. Both defects shipped, both were invisible -- one fabricated candidates,
the other dropped wards before any counter saw them -- and both were found only
by comparing against an unrelated source.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from data_merge.parsers.pdf_candidates import (
    Layout,
    parse_patch,
    parse_spine,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


class TestFabricatedRowRegression:
    """A ward name that fills its column collides with the invalid-votes label.

    ``RE_INVALID`` required whitespace before the label, so these lines fell
    through to the candidate patterns, matched, and were emitted as real
    candidates carrying a gender, a vote count and a party of
    ``WARDInvalid Vote``. 261 fabricated rows in 2010, 36 in 2015.
    """

    GLUED = (
        "ALAPPUZHA   B04036   Champakkulam   B04036002   "
        "Mancombu ThekkekkaraInvalid Vote                      134"
    )
    GLUED_2015 = "ALAPPUZHA G04003 Panavally G04003004 TRICHATTUKULAM  H S WARDInvalid Votes 2"

    def test_a_glued_invalid_vote_row_is_never_a_candidate_in_spine_mode(self) -> None:
        rows, report = parse_spine([self.GLUED])
        assert [r.row_type for r in rows] == ["invalid"]
        assert report.candidate_rows == 0
        assert rows[0].votes == 134
        assert rows[0].ward_code == "B04036002"

    def test_the_glued_row_recovers_the_ward_name_and_flags_the_repair(self) -> None:
        rows, _ = parse_spine([self.GLUED])
        assert rows[0].ward_name == "Mancombu Thekkekkara"
        assert "wardname_glued_invalid" in rows[0].parse_flag

    def test_a_glued_invalid_vote_row_is_never_a_candidate_in_patch_mode(self) -> None:
        rows, report = parse_patch([self.GLUED_2015], Layout.OLD)
        assert rows == []
        assert report.invalid_rows == 1

    def test_a_spaced_invalid_vote_row_is_also_never_a_candidate(self) -> None:
        spaced = (
            "ALAPPUZHA   B04031    Thykkattussery   B04031001     "
            "Arookkutty        Invalid Vote                     143"
        )
        rows, report = parse_patch([spaced], Layout.OLD)
        assert rows == []
        assert report.invalid_rows == 1

    def test_a_mangled_invalid_label_still_never_reaches_the_candidate_rules(self) -> None:
        """Belt and braces: a future truncation must not reintroduce fabrication."""
        mangled = "ALAPPUZHA B04036 Champakkulam B04036002 Mancombu Invald Vote 134"
        rows, report = parse_patch([mangled], Layout.OLD)
        assert rows == []
        assert report.invalid_unparsed


class TestDroppedRowRegression:
    """The local-body name can run into the ward code with no separating space.

    ``\\b[GBDMC]\\d{8}\\b`` needs a non-word character before the ``G``, so
    ``Chennam PallippuG04002001`` failed the guard. The rows were rejected by a
    ``continue`` before reaching any counter -- 638 wards missing from 2015 with
    no diagnostic mentioning them.
    """

    GLUED = "ALAPPUZHA G04002 Chennam PallippuG04002001 PANACKAL TEMPLE TOMY ULAHANNAN M INC 508"

    def test_a_ward_code_glued_to_the_local_body_name_is_parsed(self) -> None:
        rows, report = parse_patch([self.GLUED], Layout.OLD)
        assert report.unparsed == []
        assert len(rows) == 1
        assert rows[0].ward_code == "G04002001"
        assert rows[0].votes == 508
        assert rows[0].sex == "M"

    # Spine mode reads ``pdftotext -layout`` output, whose columns are separated
    # by runs of spaces, so the same collision is spelled with that geometry.
    GLUED_LAYOUT = (
        "ALAPPUZHA   G04002   Chennam PallippuG04002001   "
        "PANACKAL TEMPLE   TOMY ULAHANNAN   M   INC   508"
    )

    def test_the_same_collision_parses_in_spine_mode(self) -> None:
        rows, report = parse_spine([self.GLUED_LAYOUT])
        assert report.unparsed == []
        assert len(rows) == 1
        assert rows[0].ward_code == "G04002001"
        assert rows[0].lb_code == "G04002"
        assert rows[0].lb_name == "Chennam Pallippu"

    def test_a_ward_code_is_not_matched_inside_a_longer_digit_run(self) -> None:
        """The anchor is ``(?<!\\d)...(?!\\d)``; a 9-digit run is not a ward code."""
        rows, _ = parse_patch(
            ["ALAPPUZHA G04002 Chennam Pallippu G040020011 SOMEONE M INC 508"], Layout.OLD
        )
        assert rows == []


class TestOldLayoutSpine:
    CLEAN = (
        "ALAPPUZHA   B04031    Thykkattussery   B04031001     Arookkutty        "
        "SMITHA UNNIKRISHNAN            F     BJP              220"
    )

    # A party label is admitted to the vocabulary only after three sightings, so
    # a single line in isolation cannot recognise its own party. Real input is
    # the whole report; these companions stand in for it.
    COMPANIONS = [
        "ALAPPUZHA   B04031    Thykkattussery   B04031001     Arookkutty        "
        f"CANDIDATE {i}            M     BJP              {100 + i}"
        for i in range(3)
    ]

    def test_a_clean_line_yields_all_nine_published_fields(self) -> None:
        rows, _ = parse_spine([self.CLEAN, *self.COMPANIONS])
        row = rows[0]
        assert row.district_code == "D04001"
        assert row.district_name == "ALAPPUZHA"
        assert row.lb_type == "Block Panchayat"
        assert row.lb_code == "B04031"
        assert row.lb_name == "Thykkattussery"
        assert row.ward_code == "B04031001"
        assert row.ward_no == 1
        assert row.ward_name == "Arookkutty"
        assert row.candidate_name == "SMITHA UNNIKRISHNAN"
        assert row.sex == "F"
        assert row.party_pdf == "BJP"
        assert row.votes == 220
        assert row.row_type == "candidate"
        assert row.parse_flag == ()

    def test_district_glued_to_the_lb_code_splits_correctly(self) -> None:
        glued = (
            "PATHANAMTHITTAB03023   Mallappally           B03023001    "
            "Mukkoor        V.K RAJESH                    M    BJP      540"
        )
        rows, _ = parse_spine([glued])
        assert rows[0].lb_code == "B03023"
        assert rows[0].district_code == "D03001"
        assert rows[0].district_name == "PATHANAMTHITTA"

    def test_the_truncated_district_column_is_replaced_from_the_lb_code(self) -> None:
        """The PDF truncates ``THIRUVANANTHAP``; the code is authoritative."""
        line = (
            "THIRUVANANTHAP   G01001   Amboori   G01001001   "
            "Meenankal   SOMEONE   M   INC   120"
        )
        rows, _ = parse_spine([line])
        assert rows[0].district_name == "THIRUVANANTHAPURAM"
        assert rows[0].district_name_pdf == "THIRUVANANTHAP"

    def test_sex_glued_to_a_truncated_name_recovers_both(self) -> None:
        line = (
            "ALAPPUZHA   B04031    Thykkattussery   B04031006     Makkekadavu       "
            "T.G.REGHUNADHAN PILLAI( THOTTATHM    INC          1938"
        )
        rows, _ = parse_spine([line])
        row = rows[0]
        assert row.sex == "M"
        assert row.party_pdf == "INC"
        assert row.votes == 1938
        assert row.candidate_name.endswith("THOTTATH")
        assert "sex_glued_name" in row.parse_flag

    def test_a_row_wrapped_onto_the_next_line_is_joined_before_parsing(self) -> None:
        wrapped = [
            "PATHANAMTHITTAB03023   Mallappally    B03023001    Mukkoor        "
            "RAJAPPAN GOPALAN(EDAPPARAMPIL M",
            "                                                                   INC      103",
        ]
        rows, report = parse_spine(wrapped)
        assert report.joined_lines == 1
        assert len(rows) == 1
        assert rows[0].votes == 103
        assert rows[0].party_pdf == "INC"

    def test_a_ward_whose_every_row_is_collided_recovers_its_name_by_prefix(self) -> None:
        """No row states the ward name cleanly, but every row starts with it.

        The ward name repeats across a ward's rows and the candidate names
        following it do not, so the longest shared prefix is the ward name.
        """
        collided = [
            "ALAPPUZHA   G04099   Somewhere   G04099001   PANACKAL TEMPLEALPHA ONE   M   INC   100",
            "ALAPPUZHA   G04099   Somewhere   G04099001   PANACKAL TEMPLEBETA TWO   F   BJP   90",
            "ALAPPUZHA   G04099   Somewhere   G04099001   PANACKAL TEMPLEGAMMA SIX   M   INC   80",
        ]
        rows, report = parse_spine(collided)
        assert report.inferred_ward_names == 1
        assert {r.ward_name for r in rows} == {"PANACKAL TEMPLE"}

    def test_a_party_seen_three_times_is_admitted_to_the_vocabulary(self) -> None:
        lines = [
            f"ALAPPUZHA   G04099   Somewhere   G04099001   WARDNAME   CAND{i}   M   INC   {100 + i}"
            for i in range(4)
        ]
        _, report = parse_spine(lines)
        assert "INC" in report.party_vocabulary

    def test_a_party_seen_only_once_is_not_admitted_to_the_vocabulary(self) -> None:
        """A label appearing once or twice is likelier debris than a party."""
        lines = [
            f"ALAPPUZHA   G04099   Somewhere   G04099001   WARDNAME   "
            f"CAND{i}   M   P{i}   {100 + i}"
            for i in range(4)
        ]
        _, report = parse_spine(lines)
        assert report.party_vocabulary == frozenset()

    def test_spine_mode_reports_lines_it_cannot_read_rather_than_dropping_them(self) -> None:
        """Single-spaced text (pypdf) loses the geometry spine mode depends on.

        The point is not that it fails, but that it says so -- the defect this
        parser fixes hid because rejected lines reached no counter at all.
        """
        rows, report = parse_spine(
            ["ALAPPUZHA G04099 Somewhere G04099001 WARDNAME SOLO NAME M INC 100"]
        )
        assert rows == []
        assert len(report.unparsed) == 1


class TestNewLayoutPatch:
    CLEAN = (
        "ALAPPUZHA B04031 Thykkattussery B04031001 Arookkutty BJP Mini M 47 "
        "Kizhakkemaliyekkal, Arookkutty PO 1086"
    )

    def test_a_new_layout_line_yields_ward_votes_sex_and_age(self) -> None:
        rows, _ = parse_patch([self.CLEAN], Layout.NEW)
        row = rows[0]
        assert row.ward_code == "B04031001"
        assert row.votes == 1086
        assert row.sex == "M"
        assert row.age == "47"

    def test_a_row_with_no_age_still_parses(self) -> None:
        line = "ALAPPUZHA B04031 Thykkattussery B04031002 Perumbalam INC Someone F Address here 402"
        rows, _ = parse_patch([line], Layout.NEW)
        assert rows[0].sex == "F"
        assert rows[0].age == ""
        assert rows[0].votes == 402

    def test_the_address_is_deliberately_not_extracted(self) -> None:
        rows, _ = parse_patch([self.CLEAN], Layout.NEW)
        assert not hasattr(rows[0], "address")


class TestDuplicateKeys:
    def test_duplicate_ward_votes_keys_are_reported_never_silently_collapsed(self) -> None:
        """(ward, votes) is the join key; two candidates can poll identically."""
        tied = [
            "ALAPPUZHA G04099 Somewhere G04099001 WARDNAME ALPHA ONE M INC 500",
            "ALAPPUZHA G04099 Somewhere G04099001 WARDNAME BETA TWO F BJP 500",
        ]
        rows, report = parse_patch(tied, Layout.OLD)
        assert len(rows) == 2, "both rows survive"
        assert report.duplicate_keys == 1
        assert ("G04099001", 500) in report.duplicate_key_examples


class TestNoise:
    @pytest.mark.parametrize(
        "line",
        [
            "Page 12",
            "District  LBCode  LBName  WardCode  WardName  Party  Candidate",
            "",
            "   ",
            "some text with no ward code at all",
        ],
    )
    def test_headers_and_blanks_are_ignored_without_being_called_unparsed(
        self, line: str
    ) -> None:
        rows, report = parse_spine([line])
        assert rows == []
        assert report.unparsed == []


class TestRealFixtures:
    """Contiguous real slices, so the shapes come from the report, not a hand-built fixture."""

    def test_one_real_2010_local_body_parses_completely(self) -> None:
        rows, report = parse_spine(_lines("pdf_lines_2010_spine.txt"))
        assert report.unparsed == []
        assert {r.lb_code for r in rows} == {"B04031"}
        assert report.candidate_rows > 0
        assert report.invalid_rows > 0
        assert all(r.votes >= 0 for r in rows)
        assert all(r.ward_code.startswith("B04031") for r in rows)

    def test_every_2010_ward_in_the_fixture_has_exactly_one_invalid_vote_row(self) -> None:
        rows, _ = parse_spine(_lines("pdf_lines_2010_spine.txt"))
        wards = {r.ward_code for r in rows}
        invalid = [r for r in rows if r.row_type == "invalid"]
        assert len(invalid) == len(wards)

    def test_a_real_2015_slice_parses_including_its_glued_lines(self) -> None:
        lines = _lines("pdf_lines_2015_patch.txt")
        assert any("PallippuG04002" in line for line in lines), "fixture lost its glued rows"
        rows, report = parse_patch(lines, Layout.OLD)
        assert report.unparsed == []
        assert rows
        assert all(r.ward_code.startswith("G04002") for r in rows)

    def test_a_real_2020_slice_parses_with_ages(self) -> None:
        rows, report = parse_patch(_lines("pdf_lines_2020_patch.txt"), Layout.NEW)
        assert report.unparsed == []
        assert rows
        assert sum(1 for r in rows if r.age) >= len(rows) - 1
