"""Per-year declarations -- the one place cycle-to-cycle differences live.

Whether a year has a trend site, LSGD member data, a published front, Malayalam
names or a usable PDF sex column differs per cycle. In the pipeline this
rebuild replaces, those differences were spread across five modules. Here they
are data, and every other module reads them rather than branching on the year.

Adding a fifth cycle should mean adding a ``YearSpec`` and an assembly module,
touching nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final


class Spine(Enum):
    """What enumerates the candidates for a cycle."""

    SEC = "sec"
    """The SEC trend site's ajax feed -- the shape 2015, 2020 and 2025 share."""

    PDF = "pdf"
    """The SEC's own candidate report. 2010 only: its trend site was
    decommissioned, so the PDF is the sole candidate-level source."""


class Members(Enum):
    """Where elected-member detail (reservation, role, demographics) comes from."""

    LSGD = "lsgd"
    WIYR = "wiyr"
    NONE = "none"


class Front(Enum):
    """How a candidate's front (UDF / LDF / NDA) is established."""

    PUBLISHED = "published"
    """A source asserted the front per candidate."""

    DERIVED = "derived"
    """Inferred from the party's group where no feed published it."""

    AUTHORED = "authored"
    """Hand-assembled from documentary evidence. 2010 only -- see
    ``data/reference/party_front_2010.csv``. Rows are stamped
    ``party_group_source=mapped_2010`` and may never claim ``published``."""


class PdfSex(Enum):
    """How a cycle uses the SEC report's Sex column."""

    MEASURE = "measure"
    """Use it, but measure its orientation against the women-reserved-ward rule
    first. The orientation is never configured -- 2020's column is inverted at
    source, which is exactly the kind of fact a hardcoded constant gets wrong."""

    IGNORE = "ignore"
    """Do not take gender from the PDF at all; a better source exists."""


@dataclass(frozen=True, slots=True)
class Expectations:
    """Known-good numbers for a cycle, asserted by the build rather than eyeballed.

    Every number here was measured from the shipped outputs. A build that does
    not reproduce them fails; two parser defects previously hid for months
    because nothing compared a count to a expectation.
    """

    candidates: int
    wards: int
    local_bodies: int
    local_bodies_by_type: dict[str, int]
    gendered_rows: int
    """Rows carrying a non-empty ``candidate_gender``, counted exactly -- a
    percentage would round away the handful of rows that are the interesting ones."""

    invalid_vote_rows: int | None = None
    """Invalid-vote rows recovered from the PDF, where the cycle publishes them."""

    sec_wards_parsed: int | None = None
    """Wards the SEC feed enumerates, before candidate-bearing wards are
    selected. Differs from ``wards`` where the roster lists no-result wards."""

    wards_without_winner: int = 0
    """Contested wards left with no winner because the tie is genuinely
    undecidable from the sources.

    Asserted as an exact number: refusing to invent a winner is correct, but
    a count that moves from one to two means something changed and should
    be looked at. Zero for every cycle but 2025."""

    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class YearSpec:
    """Everything that differs between cycles, declared once."""

    year: int
    spine: Spine
    members: Members
    front: Front
    pdf_sex: PdfSex
    expect: Expectations

    pdf: str
    """Filename of the SEC candidate report under ``data/raw/sec_pdfs``."""

    sec_cache: str | None = None
    member_cache: str | None = None
    contest_cache: str | None = None

    nda_label: str = "NDA"
    """The published label for the BJP-led front. 2015 says ``BJP+``; the
    others say ``NDA``. Harmonised downstream, preserved here."""

    has_invalid_votes: bool = False
    has_lb_malayalam: bool = False
    """Malayalam local-body and urban ward names in the SEC feed."""

    has_malayalam_candidate_names: bool = False
    """2020 publishes candidate names and honorifics in Malayalam, not English."""

    has_detail: bool = False
    """The ``detailed_results_*`` endpoints, which carry the per-candidate front."""

    has_roster: bool = False
    """The full ward list, including wards with no result."""

    has_contest_feed: bool = False
    """2020's contesting-candidate feed, which carries the SEC Sex field."""

    front_table: str | None = None
    """Reference CSV under ``data/reference`` when ``front is Front.AUTHORED``."""

    def __post_init__(self) -> None:
        if self.front is Front.AUTHORED and not self.front_table:
            raise ValueError(f"{self.year}: an authored front needs a front_table")
        if self.spine is Spine.SEC and not self.sec_cache:
            raise ValueError(f"{self.year}: an SEC spine needs a sec_cache")
        if self.has_contest_feed and not self.contest_cache:
            raise ValueError(f"{self.year}: a contest feed needs a contest_cache")


_LB_TYPES_POST_2010: Final[dict[str, int]] = {
    "Block Panchayat": 152,
    "Corporation": 6,
    "District Panchayat": 14,
    "Grama Panchayat": 941,
    "Municipality": 86,
}

SPECS: Final[dict[int, YearSpec]] = {
    2010: YearSpec(
        year=2010,
        spine=Spine.PDF,
        members=Members.LSGD,
        front=Front.AUTHORED,
        pdf_sex=PdfSex.MEASURE,
        pdf="candidates_GE2010.pdf",
        member_cache="lsgd_cache_2010.sqlite",
        front_table="party_front_2010.csv",
        has_invalid_votes=True,
        expect=Expectations(
            candidates=70_524,
            wards=21_648,
            local_bodies=1_208,
            local_bodies_by_type={
                "Block Panchayat": 152,
                "Corporation": 5,
                "District Panchayat": 14,
                "Grama Panchayat": 978,
                "Municipality": 59,
            },
            gendered_rows=70_519,
            invalid_vote_rows=21_648,
            notes=(
                "70,524, not the shipped file's 70,523: one row in ward "
                "G05049001 (Kidangoor) wraps across three physical lines, and "
                "the old joiner absorbed only one continuation, so KURIAN "
                "JOSEPH (KCM, 393 votes) never acquired a vote count and was "
                "dropped. Recovering it is an intentional difference.",
                "Mattannur (34 wards) is absent from the SEC report; 21,648 + 34 "
                "= 21,682, LSGD's published ward total. The builder must not "
                "invent it -- the shortfall is reported as a known gap.",
                "7 local bodies (185 wards) never paired with LSGD, so they carry "
                "empty reservation and role rather than being dropped.",
                "Vote counts are single-sourced: no second 2010 feed exists, so "
                "cross-endpoint agreement is structurally unrunnable for 2010.",
            ),
        ),
    ),
    2015: YearSpec(
        year=2015,
        spine=Spine.SEC,
        members=Members.LSGD,
        front=Front.PUBLISHED,
        pdf_sex=PdfSex.MEASURE,
        pdf="candidates_GE2015.pdf",
        sec_cache="raw_cache.sqlite",
        member_cache="lsgd_cache.sqlite",
        nda_label="BJP+",
        has_invalid_votes=True,
        has_lb_malayalam=True,
        has_detail=True,
        has_roster=True,
        expect=Expectations(
            candidates=75_251,
            wards=21_863,
            local_bodies=1_199,
            local_bodies_by_type=dict(_LB_TYPES_POST_2010),
            gendered_rows=75_226,
            sec_wards_parsed=21_865,
            notes=(
                "B04037 Veliyanad and G13008 Narath keep their reservation. Both "
                "were previously blanked -- 30 wards -- because the alignment "
                "check fell back to the honorific instead of the SEC Sex field. "
                "Their ward numbers in fact align with LSGD exactly, ward for "
                "ward; there was never a misalignment to correct.",
                "75,226 gendered rows, not the shipped file's 75,251. An "
                "honorific records how someone is addressed, not gender, and "
                "the SEC writes 'Shri' for women often enough to have blanked "
                "the two local bodies above. The SEC report's Sex column "
                "resolves 99.96% of rows on its own; the remaining 25 "
                "have no Sex value in the report and no other source, so they "
                "are left empty rather than guessed from a title.",
            ),
        ),
    ),
    2020: YearSpec(
        year=2020,
        spine=Spine.SEC,
        members=Members.LSGD,
        front=Front.PUBLISHED,
        pdf_sex=PdfSex.IGNORE,
        pdf="candidates_GE2020.pdf",
        sec_cache="raw_cache_2020.sqlite",
        member_cache="lsgd_cache_2020.sqlite",
        contest_cache="contest_cache_2020.sqlite",
        has_lb_malayalam=True,
        has_malayalam_candidate_names=True,
        has_detail=True,
        has_roster=True,
        has_contest_feed=True,
        expect=Expectations(
            candidates=74_693,
            wards=21_821,
            local_bodies=1_199,
            local_bodies_by_type=dict(_LB_TYPES_POST_2010),
            gendered_rows=74_693,
            notes=(
                "The PDF Sex column is inverted at source, so gender comes from "
                "the contesting-candidate feed instead. The PDF's AGE column is "
                "sound and is still used.",
                "The PDF patch now yields 74,688 rows against the shipped "
                "parse's 74,630: 58 wrapped rows the old joiner could not "
                "reassemble. The candidate count is unchanged -- the spine is "
                "the SEC feed -- but 58 more rows gain candidate_age.",
                "2020 publishes no invalid-votes row at all.",
            ),
        ),
    ),
    2025: YearSpec(
        year=2025,
        spine=Spine.SEC,
        members=Members.WIYR,
        front=Front.DERIVED,
        pdf_sex=PdfSex.MEASURE,
        pdf="candidates_GE2025.pdf",
        sec_cache="raw_cache_2025.sqlite",
        member_cache="wiyr_cache_2025.sqlite",
        expect=Expectations(
            candidates=75_627,
            wards=23_573,
            local_bodies=1_199,
            local_bodies_by_type=dict(_LB_TYPES_POST_2010),
            gendered_rows=75_591,
            notes=(
                "75,591 gendered rows, not the shipped file's 75,598. The 7 "
                "missing rows sit inside the address-column overflow described "
                "below -- the PDF patch never recovers them, so there is no Sex "
                "value to take, and an honorific is not a source.",
                "The WIYR walk must branch on page shape: District Panchayats "
                "and Corporations have one body per district, so their local-"
                "body page IS the ward table and links straight to members, "
                "with no intermediate ward-table hop. Following the ward-table "
                "hop unconditionally silently skips all 14 DPs and all 6 "
                "Corporations -- 782 wards with no reservation and no role, and "
                "ward C08004038's four-way tie left unresolvable for want of "
                "the member record that decides it.",
                "Runs on a separate host with a thinner feed: no detailed-results "
                "and no roster endpoint, so the front is derived, not published.",
                "The dv payload reports 0 wards for every local body in Wayanad "
                "and Kasargod; wv returns their wards correctly, so only the "
                "summary counts are broken.",
                "The PDF patch yields 75,619 rows against the shipped parse's "
                "75,626. 8 lines remain unparsed, all address-column overflow "
                "that glues one record's votes to the next record's name -- a "
                "known layout defect deferred as out of scope.",
            ),
        ),
    ),
}

YEARS: Final[tuple[int, ...]] = tuple(sorted(SPECS))


def spec_for(year: int) -> YearSpec:
    """The declaration for one cycle."""
    try:
        return SPECS[year]
    except KeyError:
        raise KeyError(f"no spec for {year}; known years are {list(YEARS)}") from None
