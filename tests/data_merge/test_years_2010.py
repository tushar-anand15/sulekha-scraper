"""The 2010 builder.

Fast tests build off a real six-ward slice of Athiyanoor Block Panchayat
(``B01003`` in the PDF, ``Athiyannoor`` on the LSGD portal -- the spelling
mismatch is deliberate, a real fuzzy-pairing case) plus small synthetic wards
for the scenarios the real slice does not exercise on its own: an unpaired
local body, an undecidable tie, and an inverted PDF sex column.

Everything that needs the full PDF or the full LSGD cache is marked
``integration`` and reproduces the plan's hard verification numbers exactly.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

from data_merge.parsers.lsgd import parse_member_page
from data_merge.parsers.pdf_candidates import SpineRow, parse_spine
from data_merge.schema import SCHEMA
from data_merge.sources.cache import ResponseCache
from data_merge.sources.pdf import extract
from data_merge.spec import Front, spec_for
from data_merge.years.y2010 import (
    LsgdMemberRow,
    Y2010Inputs,
    build,
    load_lsgd_members,
    parse_front_table,
)

FIXTURES = Path(__file__).parent / "fixtures"
SPEC = spec_for(2010)


def _front_table() -> dict[str, object]:
    with (FIXTURES / "y2010_party_front_small.csv").open(encoding="utf-8-sig") as fh:
        return parse_front_table(csv.DictReader(fh))


def _athiyanoor_spine() -> list[SpineRow]:
    lines = (FIXTURES / "y2010_pdf_lines_athiyanoor.txt").read_text(encoding="utf-8").splitlines()
    rows, report = parse_spine(lines)
    assert report.unparsed == [], "fixture must parse cleanly"
    return rows


def _athiyanoor_members() -> tuple[LsgdMemberRow, ...]:
    """The real LSGD ward table for Athiyannoor BP, wards 1-6 -- already a
    fixture of ``test_parsers_lsgd.py``, reused here to avoid duplicating it."""
    html = (FIXTURES / "lsgd_ward_table_athiyannoor.html").read_text(encoding="utf-8")
    page = parse_member_page(html, "Block Panchayat")
    return tuple(
        LsgdMemberRow(
            district=page.district,
            lb_type="Block Panchayat",
            lb_name=page.lb_name,
            ward_no=row.ward_no,
            ward_name=row.ward_name,
            member_name=row.member_name,
            role=row.role,
            party=row.party,
            reservation=row.reservation,
            gender="Male" if row.ward_no == 1 else "",
            age="",
            education="",
            occupation="",
        )
        for row in page.rows
    )


def _row(
    *,
    ward_code: str,
    ward_no: int,
    ward_name: str = "WARD",
    lb_code: str,
    lb_type: str = "Grama Panchayat",
    lb_name: str = "TESTPANCHAYAT",
    district_name: str = "KOLLAM",
    candidate_name: str,
    sex: str,
    party_pdf: str,
    votes: int,
    row_type: str = "candidate",
) -> SpineRow:
    return SpineRow(
        district_code="D02001",
        district_name=district_name,
        district_name_pdf=district_name,
        lb_type=lb_type,
        lb_code=lb_code,
        lb_name=lb_name,
        ward_code=ward_code,
        ward_no=ward_no,
        ward_name=ward_name,
        candidate_name=candidate_name,
        sex=sex,
        party_pdf=party_pdf,
        votes=votes,
        row_type=row_type,
    )


class TestAthiyanoorHappyPath:
    """A real six-ward slice: exact ward pairing, a fuzzy local-body name
    pairing (``Athiyanoor`` vs ``Athiyannoor``), and reserved-ward gender."""

    @pytest.fixture(scope="class")
    def result(self):
        inputs = Y2010Inputs(
            spine=_athiyanoor_spine(),
            lsgd_members=_athiyanoor_members(),
            front_table=_front_table(),
        )
        return build(SPEC, inputs)

    def test_every_candidate_row_is_the_canonical_31_columns(self, result) -> None:
        for row in result.candidates:
            assert tuple(row.keys()) == SCHEMA

    def test_invalid_vote_rows_never_became_candidates(self, result) -> None:
        assert result.report.candidate_rows == 21
        assert result.report.wards == 6
        assert result.report.local_bodies == 1

    def test_the_local_body_pairs_by_fuzzy_name_not_exact(self, result) -> None:
        """``Athiyanoor`` (PDF) and ``Athiyannoor`` (LSGD) never match exactly --
        the pairing cascade must fall through to ``wardnames_agree``."""
        assert result.report.lb_pairing_how["fuzzy_name"] == 1
        assert result.report.lb_pairing_how["exact"] == 0
        assert result.report.lb_unpaired == ()
        assert result.report.lb_gate_rejected == ()

    def test_every_row_is_stamped_mapped_2010_and_never_published(self, result) -> None:
        assert all(row["party_group_source"] == "mapped_2010" for row in result.candidates)
        assert not any(row["party_group_source"] == "published" for row in result.candidates)

    def test_winners_match_the_real_lsgd_member_by_name(self, result) -> None:
        winners = {row["ward_code"]: row for row in result.candidates if row["status"] == "won"}
        assert winners["B01003001"]["candidate_name"] == "UCHAKKADA SURESH"
        assert winners["B01003002"]["candidate_name"] == "GEETHA PRABHULLA CHANDRAN"
        assert winners["B01003003"]["candidate_name"] == "C THANKARAJ"
        assert winners["B01003004"]["candidate_name"] == "MARIYAMMA KESARI"
        assert winners["B01003006"]["candidate_name"] == "KOMALAM S"

    def test_the_winners_reservation_and_role_come_from_lsgd(self, result) -> None:
        winners = {row["ward_code"]: row for row in result.candidates if row["status"] == "won"}
        assert winners["B01003001"]["ward_reservation"] == "General"
        assert winners["B01003002"]["ward_reservation"] == "Woman"
        assert winners["B01003003"]["ward_reservation"] == "SC"
        assert winners["B01003001"]["candidate_role"] == "Member"
        # candidate_role and candidate_name_eng are winner-only, never on a loser.
        losers = [row for row in result.candidates if row["status"] == "lost"]
        assert all(row["candidate_role"] == "" for row in losers)
        assert all(row["candidate_name_eng"] == "" for row in losers)

    def test_a_reserved_ward_forces_female_regardless_of_the_pdf_column(self, result) -> None:
        """B01003002 and B01003004 are ``Woman``-reserved; every candidate
        there resolves ``F``, winner and losers alike."""
        for ward_code in ("B01003002", "B01003004"):
            rows = [r for r in result.candidates if r["ward_code"] == ward_code]
            assert all(r["candidate_gender"] == "F" for r in rows)
            assert all(r["gender_source"] == "reserved_ward" for r in rows)

    def test_a_party_absent_from_the_front_table_maps_to_oth_and_is_reported(self, result) -> None:
        """BSP is not in the trimmed front-table fixture."""
        bsp_rows = [r for r in result.candidates if r["party_name"] == "BSP"]
        assert bsp_rows
        assert all(r["party_group"] == "OTH" and r["party_front"] == "OTH" for r in bsp_rows)
        assert result.report.unmapped_parties["BSP"] == len(bsp_rows)

    def test_local_body_and_ward_output_rows_are_produced(self, result) -> None:
        assert len(result.local_bodies) == 1
        assert len(result.wards) == 6


class TestUnpairedLocalBody:
    """A local body the pairing cascade cannot match to any LSGD entry --
    its wards must survive with empty reservation and role, never dropped."""

    @pytest.fixture(scope="class")
    def result(self):
        spine = [
            _row(
                ward_code="G88888001",
                ward_no=1,
                lb_code="G88888",
                candidate_name="ALONE WINNER",
                sex="M",
                party_pdf="INC",
                votes=500,
            ),
            _row(
                ward_code="G88888001",
                ward_no=1,
                lb_code="G88888",
                candidate_name="ALONE LOSER",
                sex="F",
                party_pdf="BJP",
                votes=200,
            ),
        ]
        # An LSGD local body that shares nothing with G88888 -- different
        # name, different district -- so the cascade cannot pair them.
        members = (
            LsgdMemberRow(
                district="ERNAKULAM",
                lb_type="Grama Panchayat",
                lb_name="COMPLETELY UNRELATED",
                ward_no=1,
                ward_name="OTHER",
                member_name="SOMEONE ELSE",
                role="Member",
                party="INC",
                reservation="General",
                gender="",
                age="",
                education="",
                occupation="",
            ),
        )
        inputs = Y2010Inputs(spine=spine, lsgd_members=members, front_table=_front_table())
        return build(SPEC, inputs)

    def test_the_local_body_is_reported_unpaired(self, result) -> None:
        assert len(result.report.lb_unpaired) == 1
        assert result.report.lb_unpaired[0][2] == "TESTPANCHAYAT"

    def test_its_rows_are_not_dropped(self, result) -> None:
        rows = [r for r in result.candidates if r["lb_code"] == "G88888"]
        assert len(rows) == 2

    def test_its_rows_carry_empty_reservation_and_role(self, result) -> None:
        rows = [r for r in result.candidates if r["lb_code"] == "G88888"]
        assert all(r["ward_reservation"] == "" for r in rows)
        assert all(r["candidate_role"] == "" for r in rows)

    def test_the_local_body_still_appears_in_the_local_bodies_output(self, result) -> None:
        """Even with zero LSGD-derived data, the winner is still derivable
        from the PDF's own vote counts, so the local body is not missing."""
        codes = {lb["lb_code"] for lb in result.local_bodies}
        assert "G88888" in codes


class TestVoteTies:
    """Ties are derived, never guessed: one resolves against the LSGD member
    name, a sibling with no matching LSGD data is left genuinely undecided."""

    @pytest.fixture(scope="class")
    def result(self):
        spine = [
            # Ward 1: a two-way tie, resolvable via the LSGD member name.
            _row(
                ward_code="G00001001",
                ward_no=1,
                ward_name="WARDA",
                lb_code="G00001",
                candidate_name="ALPHA ONE",
                sex="M",
                party_pdf="INC",
                votes=500,
            ),
            _row(
                ward_code="G00001001",
                ward_no=1,
                ward_name="WARDA",
                lb_code="G00001",
                candidate_name="BETA TWO",
                sex="M",
                party_pdf="BJP",
                votes=500,
            ),
            # Ward 2: a two-way tie with no LSGD ward to resolve against.
            _row(
                ward_code="G00001002",
                ward_no=2,
                ward_name="WARDB",
                lb_code="G00001",
                candidate_name="GAMMA THREE",
                sex="M",
                party_pdf="INC",
                votes=300,
            ),
            _row(
                ward_code="G00001002",
                ward_no=2,
                ward_name="WARDB",
                lb_code="G00001",
                candidate_name="DELTA FOUR",
                sex="M",
                party_pdf="BJP",
                votes=300,
            ),
        ]
        members = (
            LsgdMemberRow(
                district="KOLLAM",
                lb_type="Grama Panchayat",
                lb_name="TESTPANCHAYAT",
                ward_no=1,
                ward_name="WARDA",
                member_name="ALPHA ONE",
                role="President",
                party="INC",
                reservation="General",
                gender="Male",
                age="",
                education="",
                occupation="",
            ),
        )
        inputs = Y2010Inputs(spine=spine, lsgd_members=members, front_table=_front_table())
        return build(SPEC, inputs)

    def test_two_ties_are_counted(self, result) -> None:
        assert result.report.vote_ties == 2

    def test_the_tie_with_a_matching_lsgd_name_resolves(self, result) -> None:
        assert result.report.vote_ties_resolved == 1
        ward1 = [r for r in result.candidates if r["ward_code"] == "G00001001"]
        won = next(r for r in ward1 if r["status"] == "won")
        lost = next(r for r in ward1 if r["status"] == "lost")
        assert won["candidate_name"] == "ALPHA ONE"
        assert won["candidate_role"] == "President"
        assert lost["candidate_name"] == "BETA TWO"

    def test_the_tie_with_no_lsgd_ward_is_left_undecided_not_guessed(self, result) -> None:
        assert "G00001002" in result.report.vote_ties_unresolved
        rows = [r for r in result.candidates if r["ward_code"] == "G00001002"]
        assert all(r["status"] == "tie" for r in rows)
        assert all(r["ward_winner_party"] == "" for r in rows)


class TestGenderOrientationIsMeasured:
    """The PDF sex column's orientation must be measured, never assumed --
    an inverted column here proves the resolver actually inverted it."""

    @pytest.fixture(scope="class")
    def result(self):
        spine: list[SpineRow] = []
        members: list[LsgdMemberRow] = []
        # 250 uncontested, women-reserved wards, every one recording "M" --
        # an inverted column, well above measure_orientation's sample floor.
        for i in range(250):
            code = f"G70000{i:03d}"
            spine.append(
                _row(
                    ward_code=code,
                    ward_no=i,
                    ward_name=f"RESERVED{i}",
                    lb_code="G70000",
                    lb_name="INVERTEDPANCHAYAT",
                    candidate_name=f"CANDIDATE {i}",
                    sex="M",
                    party_pdf="INC",
                    votes=100 + i,
                )
            )
            members.append(
                LsgdMemberRow(
                    district="KOLLAM",
                    lb_type="Grama Panchayat",
                    lb_name="INVERTEDPANCHAYAT",
                    ward_no=i,
                    ward_name=f"RESERVED{i}",
                    member_name=f"CANDIDATE {i}",
                    role="Member",
                    party="INC",
                    reservation="Woman",
                    gender="",
                    age="",
                    education="",
                    occupation="",
                )
            )
        # A control ward, unreserved, whose PDF-recorded "M" should flip to
        # "F" once the column is measured inverted.
        spine.append(
            _row(
                ward_code="G70000998",
                ward_no=998,
                ward_name="CONTROL",
                lb_code="G70000",
                lb_name="INVERTEDPANCHAYAT",
                candidate_name="CONTROL CANDIDATE",
                sex="M",
                party_pdf="INC",
                votes=999,
            )
        )
        members.append(
            LsgdMemberRow(
                district="KOLLAM",
                lb_type="Grama Panchayat",
                lb_name="INVERTEDPANCHAYAT",
                ward_no=998,
                ward_name="CONTROL",
                member_name="CONTROL CANDIDATE",
                role="Member",
                party="INC",
                reservation="General",
                gender="",
                age="",
                education="",
                occupation="",
            )
        )
        inputs = Y2010Inputs(spine=spine, lsgd_members=members, front_table=_front_table())
        return build(SPEC, inputs)

    def test_orientation_measures_inverted_not_assumed_aligned(self, result) -> None:
        assert result.report.gender_orientation == "inverted"

    def test_an_unreserved_candidates_recorded_sex_is_flipped(self, result) -> None:
        row = next(r for r in result.candidates if r["ward_code"] == "G70000998")
        assert row["candidate_gender"] == "F"

    def test_reserved_wards_still_resolve_female_via_the_law_not_the_column(self, result) -> None:
        rows = [r for r in result.candidates if r["ward_code"] != "G70000998"]
        assert all(r["candidate_gender"] == "F" for r in rows)
        assert all(r["gender_source"] == "reserved_ward" for r in rows)


class TestMattannurGapIsNeverInvented:
    def test_the_spec_names_mattannur_as_a_known_gap(self) -> None:
        assert any("Mattannur" in note for note in SPEC.expect.notes)

    def test_a_build_with_no_mattannur_ward_never_fabricates_one(self) -> None:
        inputs = Y2010Inputs(
            spine=_athiyanoor_spine(),
            lsgd_members=_athiyanoor_members(),
            front_table=_front_table(),
        )
        result = build(SPEC, inputs)
        assert not any("MATTANNUR" in row["lb_name"].upper() for row in result.candidates)
        assert result.report.notes == SPEC.expect.notes


class TestFrontIsAuthoredNeverPublished:
    def test_2010s_front_is_authored_not_published(self) -> None:
        assert SPEC.front is Front.AUTHORED
        assert SPEC.front is not Front.PUBLISHED


class TestLoadLsgdMembers:
    """The 2010-only LSGD site walk, over a tiny synthetic cache that mirrors
    the real portal's index -> per-district -> ward-table -> person shape."""

    @pytest.fixture()
    def cache(self, tmp_path: Path):
        db_path = tmp_path / "mini_lsgd_2010.sqlite"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE resp (key TEXT PRIMARY KEY, json TEXT NOT NULL, fetched_at REAL NOT NULL)"
        )

        def put(key: str, value: str) -> None:
            import json

            conn.execute("INSERT INTO resp VALUES (?, ?, 0)", (key, json.dumps(value)))

        base = "https://lsgkerala.gov.in"
        # Type 2 (Block Panchayat) is not a direct type: index -> district
        # list -> member page, exactly like the real portal.
        put(
            f"GET|{base}/en/lbelection/electdistrict/2010/2",
            '<a href="/en/lbelection/electlbrpt/2/1/2010">Thiruvananthapuram</a>',
        )
        put(
            f"GET|{base}/en/lbelection/electlbrpt/2/1/2010",
            '<a href="/en/lbelection/electdmemberdet/2010/11">Athiyannoor</a>',
        )
        ward_table = (FIXTURES / "lsgd_ward_table_athiyannoor.html").read_text(encoding="utf-8")
        put(f"GET|{base}/en/lbelection/electdmemberdet/2010/11", ward_table)
        person = (FIXTURES / "lsgd_person_blank_fields.html").read_text(encoding="utf-8")
        put(
            f"GET|{base}/en/lbelection/electdmemberpersondet/2010/11/2010001100101",
            person,
        )
        # Types 1, 3, 4, 5 have no index page cached -- the loader simply
        # skips a missing index and does not raise.
        conn.commit()
        conn.close()
        with ResponseCache(db_path) as opened:
            yield opened

    def test_the_site_walk_finds_the_local_body_through_the_district_list(self, cache) -> None:
        members = load_lsgd_members(cache)
        assert len(members) == 6
        assert all(m.lb_type == "Block Panchayat" for m in members)
        assert all(m.lb_name == "Athiyannoor" for m in members)

    def test_the_one_cached_person_page_is_joined_by_ward(self, cache) -> None:
        members = load_lsgd_members(cache)
        ward_one = next(m for m in members if m.ward_no == 1)
        assert ward_one.gender == "Male"
        others = [m for m in members if m.ward_no != 1]
        assert all(m.gender == "" for m in others)


# ---------------------------------------------------------------------------
# Integration: the full PDF and the full LSGD cache, against the plan's
# hard verification numbers.
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestFullBuild:
    @pytest.fixture(scope="class")
    def result(self):
        from data_merge.config import resolve_paths

        paths = resolve_paths()
        pdf_path = paths.sec_pdfs / SPEC.pdf
        text = extract(pdf_path, engine="pdftotext", cache_dir=paths.root / "interim/pdf_text")
        spine, parse_report = parse_spine(text.lines())
        assert parse_report.unparsed == []

        assert SPEC.member_cache is not None
        assert SPEC.front_table is not None
        with ResponseCache(paths.caches / SPEC.member_cache) as cache:
            members = load_lsgd_members(cache)

        with (paths.reference / SPEC.front_table).open(encoding="utf-8-sig") as fh:
            front_table = parse_front_table(csv.DictReader(fh))

        inputs = Y2010Inputs(spine=spine, lsgd_members=members, front_table=front_table)
        return build(SPEC, inputs)

    def test_candidate_count_is_70524_not_the_shipped_70523(self, result) -> None:
        assert len(result.candidates) == 70_524

    def test_ward_and_local_body_counts(self, result) -> None:
        assert result.report.wards == 21_648
        assert result.report.local_bodies == 1_208

    def test_local_bodies_by_type(self, result) -> None:
        from collections import Counter

        seen: dict[str, str] = {}
        for row in result.candidates:
            seen[row["lb_code"]] = row["lb_type"]
        assert Counter(seen.values()) == Counter(SPEC.expect.local_bodies_by_type)

    def test_every_row_is_mapped_2010_never_published(self, result) -> None:
        assert all(row["party_group_source"] == "mapped_2010" for row in result.candidates)
        assert not any(row["party_group_source"] == "published" for row in result.candidates)

    def test_all_49_vote_ties_resolve_against_lsgd(self, result) -> None:
        assert result.report.vote_ties == 49
        assert result.report.vote_ties_resolved == 49
        assert result.report.vote_ties_unresolved == ()

    def test_the_seven_unpaired_local_bodies_leave_185_wards_with_empty_reservation(
        self, result
    ) -> None:
        assert len(result.report.lb_unpaired) == 7
        empty = {row["ward_code"] for row in result.candidates if row["ward_reservation"] == ""}
        assert len(empty) == 185

    def test_mattannur_is_absent_and_not_invented(self, result) -> None:
        assert not any("MATTANNUR" in row["lb_name"].upper() for row in result.candidates)
        # 21,648 + 34 Mattannur wards = 21,682, LSGD's published ward total.
        assert result.report.wards + 34 == 21_682

    def test_gendered_rows_and_female_winner_share(self, result) -> None:
        gendered = sum(1 for row in result.candidates if row["candidate_gender"])
        assert gendered == SPEC.expect.gendered_rows
        assert result.report.gender_orientation == "aligned"
        female_share = 100.0 * result.report.winners_female / result.report.winners_gendered
        assert female_share == pytest.approx(53.05, abs=0.01)
