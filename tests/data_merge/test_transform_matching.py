"""Comparators, the local-body pairing cascade, and the rejection gate.

No I/O, no fixtures -- every scenario is a handful of inline strings, which is
the point of a year-agnostic transform layer.
"""

from __future__ import annotations

from data_merge.transform.matching import (
    WardTally,
    apply_gate,
    names_agree,
    pair_local_bodies,
    wardnames_agree,
)


def test_names_agree_on_a_loose_transliteration() -> None:
    assert names_agree("Neelamperoor", "NEELAMPERROR")


def test_names_agree_on_a_parenthesised_alias() -> None:
    assert names_agree("THOMAS SCARIA (P.T.SCARIA)", "THOMAS SCARIA")


def test_names_disagree_on_unrelated_names() -> None:
    assert names_agree("RAJAN NAIR", "SAJITHA BEEVI") == ""


def test_wardnames_agree_ignoring_the_sec_ward_suffix() -> None:
    assert wardnames_agree("Chelakkara WARD", "Chelakkara")


def test_wardnames_disagree_on_unrelated_names() -> None:
    assert not wardnames_agree("Chelakkara", "Perumbavoor")


def test_two_equally_close_fuzzy_candidates_yield_no_pairing() -> None:
    # Both pool names are one edit away from the target and equidistant, so
    # the uniqueness margin is not cleared and the pick would be arbitrary.
    pool = {("KOLLAM", "Grama Panchayat"): frozenset({"KADAKKAL", "KADAKKAD"})}
    result = pair_local_bodies([("KOLLAM", "Grama Panchayat", "KADAKKAX")], pool)
    assert result.matched == {}
    assert result.unpaired == (("KOLLAM", "Grama Panchayat", "KADAKKAX"),)


def test_exact_match_wins_the_cascade_immediately() -> None:
    pool = {("KOLLAM", "Grama Panchayat"): frozenset({"CHITHARA", "KADAKKAL"})}
    result = pair_local_bodies([("KOLLAM", "Grama Panchayat", "CHITHARA")], pool)
    assert result.matched[("KOLLAM", "Grama Panchayat", "CHITHARA")] == "CHITHARA"
    assert result.how["exact"] == 1


def test_one_per_district_pairs_a_district_panchayat_by_position_not_name() -> None:
    # LSGD names the District Panchayat for the district; the SEC report
    # truncates its own district column, so the names need not match at all.
    pool = {("KOLLAM", "District Panchayat"): frozenset({"KOLLAM DISTRICT PANCHAYAT"})}
    result = pair_local_bodies([("KOLLAM", "District Panchayat", "KOLLAM DIST PANCHAYA")], pool)
    assert (
        result.matched[("KOLLAM", "District Panchayat", "KOLLAM DIST PANCHAYA")]
        == "KOLLAM DISTRICT PANCHAYAT"
    )
    assert result.how["one_per_district"] == 1


def test_a_local_body_whose_wards_agree_on_nothing_is_rejected_and_reported() -> None:
    tally = WardTally()
    for _ in range(6):
        tally = tally.add(ward=False, name=False, party=False)

    gate = apply_gate({"G01001": tally})

    assert "G01001" not in gate.kept
    assert "G01001" in gate.rejected
    assert gate.rejected["G01001"].total == 6


def test_a_local_body_that_clears_the_ward_name_threshold_is_kept() -> None:
    tally = WardTally()
    for _ in range(4):
        tally = tally.add(ward=True, name=False, party=False)
    for _ in range(4):
        tally = tally.add(ward=False, name=False, party=False)

    gate = apply_gate({"G01002": tally})

    assert "G01002" in gate.kept
    assert gate.rejected == {}


def test_below_the_sample_floor_a_pairing_is_kept_by_default() -> None:
    tally = WardTally().add(ward=False, name=False, party=False)
    gate = apply_gate({"G01003": tally})
    assert "G01003" in gate.kept


class TestPrefixRule:
    """One portal records a fuller name than the other.

    LSGD's "SALIKUTTY JOSEPH" against the SEC's "Sali Kutty" scores only 0.75
    by ratio -- the surname is pure unmatched length -- yet space-free one is a
    clean prefix of the other. Without this rule ward G06048007's 342-342 tie
    in 2020 had no resolvable winner, though the shipped data resolves it.
    """

    def test_a_fuller_name_matches_its_prefix(self) -> None:
        assert names_agree("SALIKUTTY JOSEPH", "Sali Kutty") == "prefix"

    def test_the_rule_is_symmetric(self) -> None:
        assert names_agree("Sali Kutty", "SALIKUTTY JOSEPH") == "prefix"

    def test_a_short_shared_prefix_is_not_a_match(self) -> None:
        """Kerala names repeat their opening syllables, so a short prefix is
        usually just coincidence."""
        assert names_agree("RAJAN", "RAJANBABU") == ""
        assert names_agree("SUNIL", "SUNILKUMAR") == ""

    def test_a_shared_prefix_below_the_floor_on_one_side_only_is_not_a_match(self) -> None:
        assert names_agree("ANITHA", "ANITHAKUMARI DEVI") == ""

    def test_two_different_people_sharing_no_prefix_still_do_not_match(self) -> None:
        assert names_agree("SALIKUTTY JOSEPH", "M C .Thankachan") == ""

    def test_the_rule_does_not_disturb_the_existing_agreements(self) -> None:
        assert names_agree("THOMAS SCARIA (P.T.SCARIA)", "THOMAS SCARIA")
        assert names_agree("Neelamperoor", "NEELAMPERROR")
