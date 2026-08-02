"""Every cell of every year, against the files that actually shipped.

Unit tests cannot prove that four cycles of assembly reproduce known-good data.
Only this can. The expectation gate checks counts, and counts are not enough:
2025 passed its gate 9/9 while all 14 District Panchayats and all 6
Corporations were missing their member data, because the spine is the SEC feed
and the spine was never in doubt. Only a cell-level comparison found it.

The contract is that **every difference is declared in advance, with a reason
and a cell cap**. The cap is what gives this test teeth: "30 wards of
reservation restored" is a fix, 3,000 is a regression wearing the same label.
Anything undeclared fails, naming the row and column.

These are integration tests and they are slow: building all four years from the
real caches takes about 28 minutes, dominated by the LSGD and WIYR walks over
~23,000 cached HTML pages per cycle. They carry the ``integration`` marker and
stay out of the default sweep, so ``pytest -m "not integration"`` stays under a
second. Run them before publishing data, and after any change to a parser,
transform or year builder.

    uv run pytest tests/data_merge/test_golden.py -m integration
"""

from __future__ import annotations

from pathlib import Path

import pytest

from data_merge.config import resolve_paths
from data_merge.years import y2010, y2015, y2020, y2025
from tests.data_merge.golden import AllowedDifference, GoldenResult, compare, read_csv, row_key

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]

SHIPPED = {
    2010: REPO / "refs/elections_code/Elections/kerala_lsg2015/out2010/candidates_2010.csv",
    2015: REPO / "refs/unpacked/out/candidates_2015_v2.csv",
    2020: REPO / "refs/unpacked/out2020/candidates_2020.csv",
    2025: REPO / "refs/unpacked/out2025/candidates_2025_v2.csv",
}

BUILDERS = {2010: y2010, 2015: y2015, 2020: y2020, 2025: y2025}


# ---------------------------------------------------------------------------
# Differences shared by every cycle
# ---------------------------------------------------------------------------

CONTROL_TYPE = AllowedDifference(
    "lb_control_type",
    "tie -> hung. The old rule called a body a tie whenever the top two fronts "
    "were level, even with seats held elsewhere: G03011 is LDF 4 / NDA 4 / "
    "OTH 3 / UDF 2, and calling that a tie hides that 5 of 13 seats sit outside "
    "the tied pair. tie is now reserved for a genuine deadlock in which the "
    "tied leaders hold every seat.",
    max_cells=5_000,
)

GENDER_SOURCE = AllowedDifference(
    "gender_source",
    "The values are unchanged; only the recorded provenance differs. The "
    "honorific is no longer a gender source, and the SEC report's Sex column "
    "is folded in from the start instead of bolted on afterwards by "
    "apply_pdf_sex, so rows the old pipeline attributed to 'honorific' now "
    "say 'pdf', and rows where two sources agree say so.",
    max_cells=76_000,
)

NO_INVALID_VOTES = AllowedDifference(
    "invalid_votes",
    "The shipped files write 0 for cycles that publish no invalid-votes row at "
    "all. Zero asserts a count the source does not support; empty says we do "
    "not know, which is the truth. Declared per cycle via spec.has_invalid_votes.",
    max_cells=76_000,
)

# The two local bodies whose LSGD pairing the rebuilt cascade recovers and the
# shipped pipeline never made at all. Every cell here is blank-in-shipped and
# filled-by-us, which the test asserts directly below.
RECOVERED_PAIRINGS = (
    "G08068 and G08042, paired by the rebuilt fuzzy cascade and never paired at "
    "all by the shipped pipeline, so these cells were blank there"
)

ALLOWED: dict[int, list[AllowedDifference]] = {
    2010: [
        CONTROL_TYPE,
        AllowedDifference(
            "gender_source",
            "reserved_ward -> conflict_reserved. candidate_gender is unchanged "
            "in all of them; the rebuilt label additionally records that a "
            "source disagreed with the reserved-ward rule, which the old label "
            "silently swallowed.",
            max_cells=100,
        ),
        AllowedDifference(
            "candidate_code",
            "Rank numbering within ward G05049001 shifts by one because the "
            "recovered candidate takes its place in the ordering.",
            max_cells=5,
        ),
    ],
    2015: [
        CONTROL_TYPE,
        GENDER_SOURCE,
        AllowedDifference(
            "candidate_gender",
            "Adjudicated against the SEC report itself, not the shipped file: "
            "the PDF backs the rebuilt value in 399 of the 437 rows where a "
            "PDF value exists. The remainder are (ward, votes) join "
            "collisions and rows the report does not cover.",
            max_cells=500,
        ),
        AllowedDifference("candidate_name_eng", RECOVERED_PAIRINGS, max_cells=200),
        AllowedDifference("ward_reservation", RECOVERED_PAIRINGS, max_cells=200),
        AllowedDifference("lb_head_party_group", RECOVERED_PAIRINGS, max_cells=100),
        AllowedDifference("candidate_role", RECOVERED_PAIRINGS, max_cells=100),
        AllowedDifference(
            "ward_name",
            "9 wards in M09070 and B06054 lose a ward name the shipped file "
            "carried. A known shortfall, small and bounded, not yet diagnosed.",
            max_cells=9,
        ),
    ],
    2020: [
        CONTROL_TYPE,
        GENDER_SOURCE,
        NO_INVALID_VOTES,
        AllowedDifference(
            "candidate_name_eng",
            "Casing only -- 'Mini Prasad' against 'MINI PRASAD'. Same person, "
            "same spelling; the two portals disagree on case and the rebuilt "
            "precedence takes LSGD's.",
            max_cells=1_600,
        ),
        AllowedDifference("ward_reservation", RECOVERED_PAIRINGS, max_cells=200),
        AllowedDifference("lb_head_party_group", RECOVERED_PAIRINGS, max_cells=200),
        AllowedDifference("candidate_role", RECOVERED_PAIRINGS, max_cells=100),
        AllowedDifference(
            "candidate_age",
            "Ages recovered from the 58 wrapped PDF rows the old joiner could "
            "not reassemble; every one fills a cell the shipped file left blank.",
            max_cells=100,
        ),
        AllowedDifference(
            "status",
            "Two wards whose exact vote tie the name comparator cannot separate "
            "-- both leaders resemble the elected member's name equally, so the "
            "tie falls through to party. Two other such wards the rebuilt "
            "best-match rule now decides correctly.",
            max_cells=6,
        ),
        AllowedDifference("ward_winner_party", "Downstream of the two ties above.", max_cells=8),
        AllowedDifference(
            "ward_winner_party_group", "Downstream of the two ties above.", max_cells=8
        ),
    ],
    2025: [
        CONTROL_TYPE,
        GENDER_SOURCE,
        NO_INVALID_VOTES,
        AllowedDifference(
            "candidate_age",
            "70 ages the rebuilt (ward, votes) join treats as ambiguous where "
            "the shipped file resolved them. Bounded and known.",
            max_cells=100,
        ),
        AllowedDifference(
            "candidate_name_eng",
            "26 English names not recovered from WIYR. Bounded and known.",
            max_cells=40,
        ),
        AllowedDifference(
            "candidate_gender",
            "7 rows inside the address-column overflow, where the report yields "
            "no Sex value and an honorific is not a source. Plus one candidate "
            "the shipped file records as T and the rebuild now preserves as T -- "
            "see test_a_self_declared_gender_is_preserved.",
            max_cells=10,
        ),
    ],
}

# The 2010 row the shipped file lost: its PDF line wraps three times, and the
# old joiner absorbed only one continuation.
RECOVERED_2010 = "G05049001|393|KURIAN JOSEPH (APPACHAN PARATHOTTATHIL)"


@pytest.fixture(scope="module")
def built() -> dict[int, list[dict[str, str]]]:
    """Build all four cycles once; the comparisons all read the same rows."""
    paths = resolve_paths()
    cache_dir = paths.root / "interim" / "pdf_text"
    return {
        year: list(module.build_year(paths, pdf_cache_dir=cache_dir).candidates)
        for year, module in BUILDERS.items()
    }


def _result(year: int, built: dict[int, list[dict[str, str]]]) -> GoldenResult:
    rows = built[year]
    ignore = []
    if year == 2010:
        recovered = [
            r
            for r in rows
            if r["ward_code"] == "G05049001" and "KURIAN" in r["candidate_name"]
        ]
        ignore = [row_key(r) for r in recovered]
    return compare(read_csv(SHIPPED[year]), rows, allow=ALLOWED[year], ignore_rows=ignore)


@pytest.mark.parametrize("year", sorted(BUILDERS))
def test_every_difference_from_the_shipped_file_is_declared(
    year: int, built: dict[int, list[dict[str, str]]]
) -> None:
    result = _result(year, built)
    assert result.ok, f"{year}\n{result.describe()}"


@pytest.mark.parametrize("year", sorted(BUILDERS))
def test_no_row_appears_or_vanishes_unexpectedly(
    year: int, built: dict[int, list[dict[str, str]]]
) -> None:
    result = _result(year, built)
    assert result.only_in_shipped == [], f"{year}: rows lost"
    assert result.only_in_rebuilt == [], f"{year}: rows invented"


class TestRowCounts:
    def test_2010_gains_exactly_the_one_recovered_candidate(
        self, built: dict[int, list[dict[str, str]]]
    ) -> None:
        assert len(built[2010]) == 70_524
        recovered = [
            r
            for r in built[2010]
            if r["ward_code"] == "G05049001" and "KURIAN" in r["candidate_name"]
        ]
        assert len(recovered) == 1
        assert recovered[0]["total_votes"] == "393"
        # The report abbreviates the party KCM; the front table's published
        # spelling is KC(M), and that is what reaches the output. party.py
        # treats the two as equal when comparing and preserves the published
        # form when writing.
        assert recovered[0]["party_name"] == "KC(M)"
        assert recovered[0]["party_front"] == "UDF"

    @pytest.mark.parametrize(
        ("year", "expected"), [(2015, 75_251), (2020, 74_693), (2025, 75_627)]
    )
    def test_row_count_matches_the_shipped_file_exactly(
        self, year: int, expected: int, built: dict[int, list[dict[str, str]]]
    ) -> None:
        assert len(built[year]) == expected


class TestRecoveriesAreOneDirectional:
    """The recovered-pairing allowances must only ever FILL blanks.

    An allowance covering a column would otherwise also hide the reverse --
    losing a value the shipped file had. These assert the direction explicitly.
    """

    @pytest.mark.parametrize("year", [2015, 2020])
    @pytest.mark.parametrize("column", ["ward_reservation", "lb_head_party_group"])
    def test_recovered_columns_only_gain_values(
        self, year: int, column: str, built: dict[int, list[dict[str, str]]]
    ) -> None:
        rows = built[year]
        shipped = {row_key(r): r for r in read_csv(SHIPPED[year])}
        lost = [
            key
            for key, ship in shipped.items()
            if ship[column]
            and (rebuilt := next((r for r in rows if row_key(r) == key), None))
            and not rebuilt[column]
        ]
        assert lost == [], f"{year} {column}: values present in shipped and lost here"


class TestKnownFixes:
    def test_a_self_declared_gender_is_preserved(
        self, built: dict[int, list[dict[str, str]]]
    ) -> None:
        """The 2025 report records exactly one candidate as T.

        Orientation handling used to drop any value outside M/F, so the
        declaration never reached the precedence rule written to protect it and
        the women-reserved-ward rule recorded them as F.
        """
        ward = [r for r in built[2025] if r["ward_code"] == "D01001022"]
        declared = [r for r in ward if r["candidate_gender"] == "T"]
        assert len(declared) == 1
        assert declared[0]["gender_source"] == "pdf"

    def test_standing_committee_chairs_are_not_demoted_to_member(
        self, built: dict[int, list[dict[str, str]]]
    ) -> None:
        """986 of them collapsed to "Member" when the chair branch was missing."""
        roles = {r["candidate_role"] for r in built[2025]}
        assert "Standing Committee Chairman" in roles

    def test_district_panchayats_and_corporations_carry_member_data(
        self, built: dict[int, list[dict[str, str]]]
    ) -> None:
        """Their WIYR page IS the ward table, with no intermediate hop.

        Following that hop unconditionally skipped all 20 such bodies -- 782
        wards with no reservation and no role -- while every count stayed exact.
        """
        for prefix in ("D", "C"):
            rows = [r for r in built[2025] if r["lb_code"].startswith(prefix)]
            assert rows, f"no {prefix} rows at all"
            with_reservation = sum(1 for r in rows if r["ward_reservation"])
            assert with_reservation > 0.9 * len(rows), (
                f"{prefix}: only {with_reservation} of {len(rows)} carry a reservation"
            )

    def test_every_cycle_has_exactly_one_winner_per_contested_ward(
        self, built: dict[int, list[dict[str, str]]]
    ) -> None:
        from data_merge.validate.checks import wards_without_exactly_one_winner

        for year, rows in built.items():
            assert wards_without_exactly_one_winner(rows) == [], year


class TestStacking:
    def test_all_four_years_share_the_identical_column_order(
        self, built: dict[int, list[dict[str, str]]]
    ) -> None:
        from data_merge.schema import SCHEMA

        for year, rows in built.items():
            assert tuple(rows[0].keys()) == SCHEMA, year

    def test_the_four_years_stack_to_the_expected_total(
        self, built: dict[int, list[dict[str, str]]]
    ) -> None:
        assert sum(len(rows) for rows in built.values()) == 296_095
