"""2015 -- the first SEC-spine year built through ``years/base.py``.

Fast tests construct a small :class:`SecSpineInputs` by hand and call
:func:`~data_merge.years.base.build` directly -- the whole point of a builder
with no path to the filesystem is that these scenarios never need a cache or
a PDF. Slow tests (marked ``integration``) build the real 2015 cycle from the
caches on disk and check the numbers this unit's plan entry demands.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from data_merge.config import resolve_paths
from data_merge.parsers.contest import ContestCandidate
from data_merge.parsers.sec_trend import CanRow, DetailCandidate
from data_merge.spec import spec_for
from data_merge.years import y2015
from data_merge.years.base import (
    LocalBodyInfo,
    LsgdMember,
    SecSpineInputs,
    build,
)

SPEC_2015 = spec_for(2015)


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


class TestLocalBodyAbsentFromLsgd:
    """A local body the SEC enumerates but LSGD never published a page for.

    Its wards must not be dropped -- they carry no reservation and no role,
    which is a different outcome from disappearing entirely.
    """

    def test_ward_survives_with_blank_reservation_and_role(self) -> None:
        inputs = SecSpineInputs(
            local_bodies={
                "G99999": LocalBodyInfo(
                    district_code="D01001",
                    district_name="THIRUVANANTHAPURAM",
                    lb_type="Grama Panchayat",
                    lb_code="G99999",
                    lb_name="NOWHERE",
                )
            },
            ward_names={"G99999001": "Nowhere Ward"},
            can_by_ward={
                "G99999001": (
                    _can("INC", 1, "Shri", "A CANDIDATE", 500),
                    _can("CPI(M)", 2, "Smt", "B CANDIDATE", 300),
                )
            },
            detail_by_ward={
                "G99999001": (
                    _detail(1, "A CANDIDATE", "INC", 500, "UDF"),
                    _detail(2, "B CANDIDATE", "CPI(M)", 300, "LDF"),
                )
            },
            lsgd_members=(),  # no LSGD page for this local body at all
            pdf_patch=(),
        )
        result = build(SPEC_2015, inputs)

        assert len(result.candidates) == 2
        assert {c["ward_code"] for c in result.candidates} == {"G99999001"}
        for row in result.candidates:
            assert row["ward_reservation"] == ""
            assert row["candidate_role"] == ""
        winner = next(c for c in result.candidates if c["candidate_code"] == "1")
        assert winner["status"] == "won"


class TestPartyFrontHarmonisation:
    """2015 publishes the BJP-led front as ``BJP+``; ``party_front`` folds it
    to ``NDA`` while ``party_group`` keeps the published spelling."""

    def test_bjp_plus_becomes_nda_in_front_but_not_in_group(self) -> None:
        inputs = SecSpineInputs(
            local_bodies={
                "G88888": LocalBodyInfo(
                    "D01001", "THIRUVANANTHAPURAM", "Grama Panchayat", "G88888", "SOMEWHERE"
                )
            },
            ward_names={"G88888001": "Somewhere Ward"},
            can_by_ward={
                "G88888001": (
                    _can("BJP", 1, "Shri", "A CANDIDATE", 400),
                    _can("INC", 2, "Smt", "B CANDIDATE", 200),
                )
            },
            detail_by_ward={
                "G88888001": (
                    _detail(1, "A CANDIDATE", "BJP", 400, "BJP+"),
                    _detail(2, "B CANDIDATE", "INC", 200, "UDF"),
                )
            },
            lsgd_members=(),
            pdf_patch=(),
        )
        result = build(SPEC_2015, inputs)
        winner = next(c for c in result.candidates if c["candidate_code"] == "1")
        assert winner["party_group"] == "BJP+"
        assert winner["party_front"] == "NDA"
        assert winner["party_group_source"] == "published"


class TestWinnerTieBreak:
    """A genuine vote tie is resolved against the LSGD member's own name,
    never picked arbitrarily -- and every ward still ends with one winner."""

    def test_tie_resolves_to_the_lsgd_named_member(self) -> None:
        inputs = SecSpineInputs(
            local_bodies={
                "G77777": LocalBodyInfo(
                    "D01001", "THIRUVANANTHAPURAM", "Grama Panchayat", "G77777", "TIEVILLE"
                )
            },
            ward_names={"G77777001": "Tie Ward"},
            can_by_ward={
                "G77777001": (
                    _can("INC", 1, "Shri", "RAVEENDRAN NAIR", 300),
                    _can("CPI(M)", 2, "Shri", "SASIDHARAN PILLAI", 300),
                )
            },
            detail_by_ward={
                "G77777001": (
                    _detail(1, "RAVEENDRAN NAIR", "INC", 300, "UDF"),
                    _detail(2, "SASIDHARAN PILLAI", "CPI(M)", 300, "LDF"),
                )
            },
            lsgd_members=(
                LsgdMember(
                    district="THIRUVANANTHAPURAM",
                    lb_type="Grama Panchayat",
                    lb_name="TIEVILLE",
                    ward_no=1,
                    ward_name="Tie Ward",
                    member_name="SASIDHARAN PILLAI",
                    role="Member",
                    party="CPI(M)",
                    reservation="General",
                    gender="Male",
                ),
            ),
            pdf_patch=(),
        )
        result = build(SPEC_2015, inputs)
        winners = [c for c in result.candidates if c["status"] == "won"]
        assert len(winners) == 1
        assert winners[0]["candidate_code"] == "2"


class TestGenderFromReservedWardRule:
    """The reserved-ward rule binds every candidate in a women-reserved
    ward, winner and losers alike -- with no PDF data and no verified LSGD
    winner reaching either candidate here, the rule is the only source at
    all, and it still resolves both to female instead of leaving them
    unresolved."""

    def test_every_candidate_in_a_women_reserved_ward_is_female(self) -> None:
        inputs = SecSpineInputs(
            local_bodies={
                "G66666": LocalBodyInfo(
                    "D01001", "THIRUVANANTHAPURAM", "Grama Panchayat", "G66666", "RESERVEDTOWN"
                )
            },
            ward_names={"G66666001": "Reserved Ward"},
            can_by_ward={
                "G66666001": (
                    _can("INC", 1, "Smt", "A WOMAN", 400),
                    _can("CPI(M)", 2, "Shri", "B WOMAN", 200),
                )
            },
            detail_by_ward={
                "G66666001": (
                    _detail(1, "A WOMAN", "INC", 400, "UDF"),
                    _detail(2, "B WOMAN", "CPI(M)", 200, "LDF"),
                )
            },
            lsgd_members=(
                LsgdMember(
                    district="THIRUVANANTHAPURAM",
                    lb_type="Grama Panchayat",
                    lb_name="RESERVEDTOWN",
                    ward_no=1,
                    ward_name="Reserved Ward",
                    member_name="A WOMAN",
                    role="Member",
                    party="INC",
                    reservation="Woman",
                    gender="Female",
                ),
            ),
            pdf_patch=(),
        )
        result = build(SPEC_2015, inputs)
        assert all(c["candidate_gender"] == "F" for c in result.candidates)
        by_code = {c["candidate_code"]: c for c in result.candidates}
        # Candidate 1 is the verified winner, so LSGD's own "Female" also
        # agrees; candidate 2 has no source at all reaching it (the
        # honorific "Shri" printed next to its name is not one), and the
        # rule alone resolves it -- not conflict_reserved, because nothing
        # here disagrees with it.
        assert by_code["1"]["gender_source"] == "reserved_ward"
        assert by_code["2"]["gender_source"] == "reserved_ward"


class TestContestingCandidateFeedIsUnusedIn2015:
    """2015 has no contesting-candidate feed; the SEC-sex source must not
    appear even if a caller mistakenly supplies one."""

    def test_sec_sex_source_never_wins_for_2015(self) -> None:
        inputs = SecSpineInputs(
            local_bodies={
                "G55555": LocalBodyInfo(
                    "D01001", "THIRUVANANTHAPURAM", "Grama Panchayat", "G55555", "ELSEWHERE"
                )
            },
            ward_names={"G55555001": "Elsewhere Ward"},
            can_by_ward={"G55555001": (_can("INC", 1, "Shri", "A CANDIDATE", 100),)},
            detail_by_ward={"G55555001": (_detail(1, "A CANDIDATE", "INC", 100, "UDF"),)},
            lsgd_members=(),
            pdf_patch=(),
            contest_by_ward={
                "G55555001": (
                    ContestCandidate(
                        ward_code="G55555001",
                        candidate_code=1,
                        name_prefix="",
                        name_eng="A Candidate",
                        name_mal="",
                        party_full_mal="",
                        party_group="UDF",
                        sex="Female",
                    ),
                )
            },
        )
        result = build(SPEC_2015, inputs)
        row = result.candidates[0]
        assert row["gender_source"] != "sec_sex"
        # 2015 has no contesting-candidate feed, no PDF data in this fixture,
        # and no LSGD match -- nothing reaches this row, and the honorific
        # printed next to its name doesn't count as a fallback, so it stays
        # unresolved rather than guessed.
        assert row["candidate_gender"] == ""
        assert row["gender_source"] == ""


# ---------------------------------------------------------------------------
# Integration: the real 2015 cycle, built from the caches and PDF on disk.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_2015():
    paths = resolve_paths()
    if paths.missing_inputs():
        pytest.skip(f"data root missing inputs: {paths.missing_inputs()}")
    cache_dir = Path(paths.root) / "interim" / "pdf_text"
    return y2015.build_year(paths, pdf_cache_dir=cache_dir)


@pytest.mark.integration
class TestReal2015HappyPath:
    def test_candidate_ward_and_local_body_counts(self, real_2015) -> None:
        expect = SPEC_2015.expect
        assert len(real_2015.candidates) == expect.candidates
        assert len({c["ward_code"] for c in real_2015.candidates}) == expect.wards
        assert len({c["lb_code"] for c in real_2015.candidates}) == expect.local_bodies

    def test_local_body_type_breakdown(self, real_2015) -> None:
        lb_types = {c["lb_code"]: c["lb_type"] for c in real_2015.candidates}
        counts: dict[str, int] = {}
        for lb_type in lb_types.values():
            counts[lb_type] = counts.get(lb_type, 0) + 1
        assert counts == SPEC_2015.expect.local_bodies_by_type

    def test_gender_reaches_near_full_coverage_from_real_sources_only(self, real_2015) -> None:
        """Every resolved row carries a source, and the source is a real
        observation -- the PDF's own Sex field (oriented), LSGD's record for
        a verified winner, or the reserved-ward rule -- never the honorific:
        it carries no trust as a gender source at all (it is what produced
        the reservation bug R6 exists to fix). ``spec.expect.gendered_rows``
        (75,251, i.e. 100%) assumed an honorific fallback; dropping the
        honorific leaves 25 rows -- 0.03% -- that no real source reaches, and
        that gap is reported here as measured and accepted, not silently
        matched against a now-stale expectation.
        """
        resolved = [c for c in real_2015.candidates if c["candidate_gender"]]
        for row in resolved:
            assert row["gender_source"]
            assert row["gender_source"] != "honorific"
        unresolved = len(real_2015.candidates) - len(resolved)
        assert unresolved == 25, f"expected exactly 25 unresolved rows, got {unresolved}"

    def test_party_group_source_is_published_on_every_row(self, real_2015) -> None:
        assert all(c["party_group_source"] == "published" for c in real_2015.candidates)

    def test_exactly_one_winner_per_ward(self, real_2015) -> None:
        winners: dict[str, int] = {}
        for c in real_2015.candidates:
            if c["status"] == "won":
                winners[c["ward_code"]] = winners.get(c["ward_code"], 0) + 1
        wards = {c["ward_code"] for c in real_2015.candidates}
        # A ward whose poll was countermanded ("no result") legitimately has
        # zero winners; every other ward has exactly one.
        no_result_wards = {
            c["ward_code"] for c in real_2015.candidates if c["status"] == "no result"
        }
        # An undecidable vote tie -- no LSGD member name to break it -- is a
        # real electoral outcome. It is excluded here because it shows up in
        # ``report.vote_ties_unresolved``, never because it slipped through
        # silently.
        unresolved_ties = set(real_2015.report.vote_ties_unresolved)
        for ward in wards - no_result_wards - unresolved_ties:
            assert winners.get(ward, 0) == 1, ward
        for ward in unresolved_ties:
            assert winners.get(ward, 0) == 0, ward


@pytest.mark.integration
class TestReal2015ReservationRegression:
    """B04037 Veliyanad and G13008 Narath were previously blanked -- 30 wards
    -- because the alignment check fell back to the honorific instead of the
    SEC Sex field. With the PDF field, both keep their reservation."""

    def test_veliyanad_keeps_its_reservation(self, real_2015) -> None:
        rows = [c for c in real_2015.candidates if c["lb_code"] == "B04037"]
        assert rows
        reserved = {c["ward_code"] for c in rows if c["ward_reservation"]}
        assert reserved, "B04037 should have at least one ward with a reservation"

    def test_narath_keeps_its_reservation(self, real_2015) -> None:
        rows = [c for c in real_2015.candidates if c["lb_code"] == "G13008"]
        assert rows
        reserved = {c["ward_code"] for c in rows if c["ward_reservation"]}
        assert reserved, "G13008 should have at least one ward with a reservation"

    def test_no_local_body_is_flagged_misaligned(self, real_2015) -> None:
        assert real_2015.report.lb_reservation_misaligned == ()
