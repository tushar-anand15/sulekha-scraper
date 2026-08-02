"""2020 -- the second SEC-spine year built through ``years/base.py``.

Mirrors ``test_years_2015.py``'s split between fast fixture tests (calling
:func:`~data_merge.years.base.build` directly, no cache or PDF needed) and
slow ``integration`` tests against the real cycle. 2020's own edge cases:
gender comes from the contesting-candidate feed rather than the PDF (whose
Sex column is inverted at source), and no invalid-votes row is ever
published.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from data_merge.config import resolve_paths
from data_merge.parsers.contest import ContestCandidate
from data_merge.parsers.sec_trend import INVALID_VOTES_CODE, CanRow, DetailCandidate
from data_merge.spec import spec_for
from data_merge.years import y2020
from data_merge.years.base import LocalBodyInfo, SecSpineInputs, build

SPEC_2020 = spec_for(2020)


def _can(
    party: str, code: int, title: str, name: str, votes: int, *, status_flag: str = "Y"
) -> CanRow:
    return CanRow(
        party_name=party,
        candidate_code=code,
        candidate_title=title,
        candidate_name=name,
        votes=votes,
        is_first=False,
        status_flag=status_flag,
    )


def _detail(
    code: int, name: str, party: str, votes: int, group: str, pos: int = 0
) -> DetailCandidate:
    return DetailCandidate(
        candidate_code=code, name=name, party=party, votes=votes, pos=pos, party_group=group
    )


def _contest(
    ward_code: str, code: int, name_eng: str, party_group: str, sex: str
) -> ContestCandidate:
    return ContestCandidate(
        ward_code=ward_code,
        candidate_code=code,
        name_prefix="",
        name_eng=name_eng,
        name_mal="",
        party_full_mal="",
        party_group=party_group,
        sex=sex,
    )


class TestInvalidVotesIsDeclaredNotIncidental:
    """2020's ``can`` feed can still carry an invalid-votes pseudo-row -- the
    site does not stop sending it -- but ``spec.has_invalid_votes`` is
    ``False`` for 2020, and that declaration alone empties the column,
    regardless of whether the row showed up."""

    def test_invalid_votes_column_is_empty_even_though_a_row_was_seen(self) -> None:
        inputs = SecSpineInputs(
            local_bodies={
                "G44444": LocalBodyInfo(
                    "D01001", "THIRUVANANTHAPURAM", "Grama Panchayat", "G44444", "INVALIDTOWN"
                )
            },
            ward_names={"G44444001": "Invalid Ward"},
            can_by_ward={
                "G44444001": (
                    _can("INC", 1, "ശ്രീ", "A CANDIDATE", 400),
                    CanRow(
                        party_name="",
                        candidate_code=INVALID_VOTES_CODE,
                        candidate_title="",
                        candidate_name="Invalid Votes",
                        votes=7,
                        is_first=False,
                        status_flag="Y",
                    ),
                )
            },
            detail_by_ward={"G44444001": (_detail(1, "A CANDIDATE", "INC", 400, "UDF"),)},
            lsgd_members=(),
            pdf_patch=(),
        )
        result = build(SPEC_2020, inputs)

        assert all(c["invalid_votes"] == "" for c in result.candidates)
        # The row was genuinely seen and counted -- the report confirms this
        # is a declared omission, not a parsing bug.
        assert result.report.invalid_vote_rows == 1


class TestGenderFromContestingCandidateFeedNotThePdf:
    """2020's own PDF Sex column is inverted at source; gender must come
    from the contesting-candidate feed instead."""

    def test_sec_sex_source_wins_over_the_honorific(self) -> None:
        inputs = SecSpineInputs(
            local_bodies={
                "G33333": LocalBodyInfo(
                    "D01001", "THIRUVANANTHAPURAM", "Grama Panchayat", "G33333", "CONTESTVILLE"
                )
            },
            ward_names={"G33333001": "Contest Ward"},
            # No honorific on this row (an occupational title such as "Adv"
            # carries no gender) -- isolating the contest feed as the only
            # available source, so its label ("sec_sex") is unambiguous.
            can_by_ward={"G33333001": (_can("INC", 1, "Adv", "A CANDIDATE", 100),)},
            detail_by_ward={"G33333001": (_detail(1, "A CANDIDATE", "INC", 100, "UDF"),)},
            lsgd_members=(),
            pdf_patch=(),
            contest_by_ward={
                "G33333001": (_contest("G33333001", 1, "A Candidate", "UDF", "Female"),)
            },
        )
        result = build(SPEC_2020, inputs)
        row = result.candidates[0]
        assert row["candidate_gender"] == "F"
        assert row["gender_source"] == "sec_sex"

    def test_pdf_sex_is_never_a_source_for_2020(self) -> None:
        assert SPEC_2020.pdf_sex.value == "ignore"


class TestPartyFrontHarmonisation:
    """2020 already publishes the BJP-led front as ``NDA``; harmonisation is
    just a pass-through here."""

    def test_nda_passes_through_unchanged(self) -> None:
        inputs = SecSpineInputs(
            local_bodies={
                "G22222": LocalBodyInfo(
                    "D01001", "THIRUVANANTHAPURAM", "Grama Panchayat", "G22222", "SOMEWHERE"
                )
            },
            ward_names={"G22222001": "Somewhere Ward"},
            can_by_ward={"G22222001": (_can("BJP", 1, "ശ്രീ", "A CANDIDATE", 100),)},
            detail_by_ward={"G22222001": (_detail(1, "A CANDIDATE", "BJP", 100, "NDA"),)},
            lsgd_members=(),
            pdf_patch=(),
        )
        result = build(SPEC_2020, inputs)
        row = result.candidates[0]
        assert row["party_group"] == "NDA"
        assert row["party_front"] == "NDA"
        assert row["party_group_source"] == "published"


class TestLocalBodyAbsentFromLsgd:
    def test_ward_survives_with_blank_reservation_and_role(self) -> None:
        inputs = SecSpineInputs(
            local_bodies={
                "G11111": LocalBodyInfo(
                    "D01001", "THIRUVANANTHAPURAM", "Grama Panchayat", "G11111", "NOWHERE"
                )
            },
            ward_names={"G11111001": "Nowhere Ward"},
            can_by_ward={
                "G11111001": (
                    _can("INC", 1, "ശ്രീ", "A CANDIDATE", 500),
                    _can("CPI(M)", 2, "ശ്രീമതി", "B CANDIDATE", 300),
                )
            },
            detail_by_ward={
                "G11111001": (
                    _detail(1, "A CANDIDATE", "INC", 500, "UDF"),
                    _detail(2, "B CANDIDATE", "CPI(M)", 300, "LDF"),
                )
            },
            lsgd_members=(),
            pdf_patch=(),
        )
        result = build(SPEC_2020, inputs)
        assert len(result.candidates) == 2
        for row in result.candidates:
            assert row["ward_reservation"] == ""
            assert row["candidate_role"] == ""


# ---------------------------------------------------------------------------
# Integration: the real 2020 cycle, built from the caches and PDF on disk.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_2020():
    paths = resolve_paths()
    if paths.missing_inputs():
        pytest.skip(f"data root missing inputs: {paths.missing_inputs()}")
    cache_dir = Path(paths.root) / "interim" / "pdf_text"
    return y2020.build_year(paths, pdf_cache_dir=cache_dir)


@pytest.mark.integration
class TestReal2020HappyPath:
    def test_candidate_ward_and_local_body_counts(self, real_2020) -> None:
        expect = SPEC_2020.expect
        assert len(real_2020.candidates) == expect.candidates
        assert len({c["ward_code"] for c in real_2020.candidates}) == expect.wards
        assert len({c["lb_code"] for c in real_2020.candidates}) == expect.local_bodies

    def test_local_body_type_breakdown(self, real_2020) -> None:
        lb_types = {c["lb_code"]: c["lb_type"] for c in real_2020.candidates}
        counts: dict[str, int] = {}
        for lb_type in lb_types.values():
            counts[lb_type] = counts.get(lb_type, 0) + 1
        assert counts == SPEC_2020.expect.local_bodies_by_type

    def test_gender_reaches_full_coverage_with_a_source_on_every_row(self, real_2020) -> None:
        assert all(c["candidate_gender"] for c in real_2020.candidates)
        assert all(c["gender_source"] for c in real_2020.candidates)
        assert (
            sum(1 for c in real_2020.candidates if c["candidate_gender"])
            == SPEC_2020.expect.gendered_rows
        )

    def test_party_group_source_is_published_on_every_row(self, real_2020) -> None:
        assert all(c["party_group_source"] == "published" for c in real_2020.candidates)

    def test_invalid_votes_is_empty_throughout(self, real_2020) -> None:
        assert all(c["invalid_votes"] == "" for c in real_2020.candidates)

    def test_exactly_one_winner_per_ward(self, real_2020) -> None:
        winners: dict[str, int] = {}
        for c in real_2020.candidates:
            if c["status"] == "won":
                winners[c["ward_code"]] = winners.get(c["ward_code"], 0) + 1
        wards = {c["ward_code"] for c in real_2020.candidates}
        no_result_wards = {
            c["ward_code"] for c in real_2020.candidates if c["status"] == "no result"
        }
        # An undecidable vote tie -- no LSGD member name to break it -- is a
        # real electoral outcome. It is excluded here because it shows up in
        # the report below, never because it slipped through silently.
        unresolved_ties = set(real_2020.report.vote_ties_unresolved)
        for ward in wards - no_result_wards - unresolved_ties:
            assert winners.get(ward, 0) == 1, ward
        for ward in unresolved_ties:
            assert winners.get(ward, 0) == 0, ward

    def test_undecidable_ties_are_reported_not_silently_dropped(self, real_2020) -> None:
        assert real_2020.report.vote_ties >= len(real_2020.report.vote_ties_unresolved)
        for ward in real_2020.report.vote_ties_unresolved:
            rows = [c for c in real_2020.candidates if c["ward_code"] == ward]
            assert rows and all(c["status"] != "won" for c in rows)
