"""Orientation measurement and precedence resolution, both against tiny sets.

The orientation tests are the direct regression for the defect that motivates
this module: 2020's PDF sex column is inverted at source, and nothing may
recover that fact from a per-year constant -- it must be measured.
"""

from __future__ import annotations

from data_merge.transform.gender import (
    GenderResolution,
    GenderSource,
    Orientation,
    Verdict,
    measure_orientation,
    oriented_sex,
    resolve_gender,
)

_SAMPLE_SIZE = 300


def test_a_mostly_female_reserved_sample_measures_aligned() -> None:
    values = (["F"] * 295) + (["M"] * 5)
    orientation = measure_orientation(values)
    assert orientation.verdict is Verdict.ALIGNED
    assert orientation.n == _SAMPLE_SIZE


def test_the_same_set_with_sexes_swapped_measures_inverted() -> None:
    values = (["M"] * 295) + (["F"] * 5)
    orientation = measure_orientation(values)
    assert orientation.verdict is Verdict.INVERTED


def test_fifty_rows_measures_unclear_regardless_of_ratio() -> None:
    # All-female, which would be a slam-dunk ALIGNED verdict at full sample
    # size, must still come back unclear: 50 observations is too few to
    # conclude anything, no matter how lopsided the ratio looks.
    values = ["F"] * 50
    orientation = measure_orientation(values)
    assert orientation.verdict is Verdict.UNCLEAR
    assert orientation.n == 50


def test_unclear_orientation_yields_no_value_rather_than_a_guess() -> None:
    unclear = measure_orientation(["F"] * 50)
    assert oriented_sex("M", unclear) == ""
    assert oriented_sex("F", unclear) == ""


def test_aligned_orientation_passes_the_value_through() -> None:
    aligned = measure_orientation((["F"] * 295) + (["M"] * 5))
    assert oriented_sex("F", aligned) == "F"
    assert oriented_sex("M", aligned) == "M"


def test_inverted_orientation_flips_the_value() -> None:
    inverted = measure_orientation((["M"] * 295) + (["F"] * 5))
    assert oriented_sex("F", inverted) == "M"
    assert oriented_sex("M", inverted) == "F"


def test_reserved_ward_outranks_a_conflicting_honorific_the_veliyanad_case() -> None:
    # SEC writes "Shri" (male) for a woman candidate in a women-reserved
    # ward; the LSGD's own gender field for the winner correctly says
    # female. The reservation rule must side with the source that agrees
    # with the law, not the honorific.
    resolution = resolve_gender(
        reserved=True,
        sources=[
            GenderSource("lsgd", "F"),
            GenderSource("honorific", "M"),
        ],
    )
    assert resolution == GenderResolution("F", "reserved_ward")


def test_reserved_ward_with_no_sources_still_resolves_female() -> None:
    resolution = resolve_gender(reserved=True, sources=[])
    assert resolution == GenderResolution("F", "reserved_ward")


def test_reserved_ward_where_every_source_says_male_is_flagged_not_trusted() -> None:
    resolution = resolve_gender(
        reserved=True, sources=[GenderSource("lsgd", "M"), GenderSource("honorific", "M")]
    )
    assert resolution == GenderResolution("F", "conflict_reserved")


def test_a_self_declared_transgender_value_outranks_the_reservation_rule() -> None:
    resolution = resolve_gender(reserved=True, sources=[GenderSource("sec_sex", "T")])
    assert resolution == GenderResolution("T", "sec_sex")


def test_two_agreeing_sources_resolve_as_both_agree() -> None:
    resolution = resolve_gender(
        reserved=False, sources=[GenderSource("pdf", "F"), GenderSource("honorific", "F")]
    )
    assert resolution == GenderResolution("F", "both_agree")


def test_disagreeing_sources_use_the_strongest_and_flag_the_conflict() -> None:
    resolution = resolve_gender(
        reserved=False, sources=[GenderSource("pdf", "M"), GenderSource("lsgd", "F")]
    )
    assert resolution == GenderResolution("M", "conflict_pdf_used")


def test_a_single_source_is_named_for_its_own_origin() -> None:
    resolution = resolve_gender(reserved=False, sources=[GenderSource("honorific", "M")])
    assert resolution == GenderResolution("M", "honorific")


def test_no_sources_at_all_is_unresolved() -> None:
    resolution = resolve_gender(reserved=False, sources=[])
    assert resolution == GenderResolution("", "")


class TestSelfDeclaredGenderSurvives:
    """One real candidate in 296,095 rows, and the pipeline erased them.

    The 2025 SEC report records exactly one candidate as ``T``. Orientation
    handling dropped any value outside M/F, so the declaration never reached
    the precedence rule written to protect it, and the women-reserved-ward rule
    recorded that candidate as F.
    """

    def test_t_passes_through_an_aligned_column(self) -> None:
        aligned = Orientation(verdict=Verdict.ALIGNED, share_female=0.99, n=1000)
        assert oriented_sex("T", aligned) == "T"

    def test_t_passes_through_an_inverted_column(self) -> None:
        """Inversion swaps M and F; it says nothing about a third value."""
        inverted = Orientation(verdict=Verdict.INVERTED, share_female=0.01, n=1000)
        assert oriented_sex("T", inverted) == "T"

    def test_t_survives_an_unclear_orientation(self) -> None:
        unclear = Orientation(verdict=Verdict.UNCLEAR, share_female=0.5, n=10)
        assert oriented_sex("T", unclear) == "T"

    def test_a_reserved_ward_does_not_overwrite_a_self_declaration(self) -> None:
        resolved = resolve_gender(
            reserved=True, sources=[GenderSource("pdf", "T")]
        )
        assert resolved.gender == "T"
        assert resolved.source == "pdf"

    def test_an_unknown_value_is_still_discarded(self) -> None:
        aligned = Orientation(verdict=Verdict.ALIGNED, share_female=0.99, n=1000)
        assert oriented_sex("X", aligned) == ""
        assert oriented_sex("", aligned) == ""
