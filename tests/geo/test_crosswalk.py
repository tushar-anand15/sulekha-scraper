"""The crosswalk, and specifically the ways it must refuse to guess.

The failure this module exists to prevent is not "no match found" -- that is loud and
fixable. It is a *confident wrong match*: a local body paired to the wrong polygon,
which attaches a whole body's election results to the wrong place and renders as a
map that looks entirely plausible. So most of what is asserted here is rejection.
"""

from __future__ import annotations

import csv

import pytest

from geo.build.crosswalk import (
    LocalBody,
    build_crosswalk,
    load_overrides,
    ward_agreement,
    write_crosswalk,
)


def ours(code, name, lb_type="Grama Panchayat", district="KOLLAM", wards=()):
    return LocalBody(code=code, name=name, lb_type=lb_type, district=district, ward_names=tuple(wards))


def theirs(code, name, lb_type="Grama Panchayat", district="Kollam", wards=()):
    return LocalBody(code=code, name=name, lb_type=lb_type, district=district, ward_names=tuple(wards))


WARDS_A = ("Puthankada", "Kollayil", "Chenkal", "Parassala", "Uchakkada")
WARDS_B = ("Marthandam", "Kuzhithura", "Munchira", "Palliyadi", "Thiruvattar")


# --- ward_agreement ---------------------------------------------------------


def test_identical_ward_sets_agree_completely():
    assert ward_agreement(WARDS_A, WARDS_A) == 1.0


def test_disjoint_ward_sets_do_not_agree():
    assert ward_agreement(WARDS_A, WARDS_B) == 0.0


def test_transliteration_variants_still_agree():
    """The whole reason a fuzzy comparison is used at all."""
    assert ward_agreement(("Kottarakara",), ("Kottarakkara",)) == 1.0


def test_agreement_is_greedy_one_to_one():
    """A repeated name on one side must not be matched twice.

    Otherwise a local body whose wards share a name could score 1.0 against a
    single coincidental match on the other side.
    """
    assert ward_agreement(("Chenkal", "Chenkal"), ("Chenkal",)) == 0.5


def test_empty_ward_list_scores_zero_not_one():
    assert ward_agreement((), WARDS_A) == 0.0


def test_extra_wards_on_their_side_do_not_penalise():
    """Delimitation differences are expected; they are not evidence of mispairing."""
    assert ward_agreement(WARDS_A, WARDS_A + WARDS_B) == 1.0


# --- pairing ----------------------------------------------------------------


def test_exact_name_pairs_and_verifies():
    r = build_crosswalk(
        [ours("G02003", "Clappana", wards=WARDS_A)],
        [theirs("G020103", "Clappana", wards=WARDS_A)],
    )
    assert r.resolved_count == 1
    assert not r.unresolved and not r.rejected
    m = r.matches[0]
    assert (m.ours.code, m.theirs.code, m.method) == ("G02003", "G020103", "exact")
    assert m.ward_agreement == 1.0


def test_transliteration_pair_resolves_fuzzily():
    r = build_crosswalk(
        [ours("M02004", "Kottarakkara", lb_type="Municipality", wards=WARDS_A)],
        [theirs("M020400", "Kottarakara", lb_type="Municipality", wards=WARDS_A)],
    )
    assert r.resolved_count == 1
    assert r.matches[0].method == "fuzzy"


def test_same_name_in_two_districts_is_not_cross_assigned():
    """`Pallickal` is a real Grama Panchayat in both Thiruvananthapuram and
    Malappuram. Scoping by district is the only thing keeping them apart."""
    r = build_crosswalk(
        [
            ours("G01205", "Pallickal", district="THIRUVANANTHAPURAM", wards=WARDS_A),
            ours("G10405", "Pallickal", district="MALAPPURAM", wards=WARDS_B),
        ],
        [
            theirs("G010205", "Pallickal", district="Thiruvananthapuram", wards=WARDS_A),
            theirs("G100405", "Pallickal", district="Malappuram", wards=WARDS_B),
        ],
    )
    assert r.resolved_count == 2
    assert {(m.ours.code, m.theirs.code) for m in r.matches} == {
        ("G01205", "G010205"),
        ("G10405", "G100405"),
    }


# --- rejection --------------------------------------------------------------


def test_name_match_with_disjoint_wards_is_rejected():
    """The load-bearing assertion of the module.

    Two bodies sharing a name but no wards are not the same body. Accepting this
    would be worse than finding nothing.
    """
    r = build_crosswalk(
        [ours("G02003", "Clappana", wards=WARDS_A)],
        [theirs("G020103", "Clappana", wards=WARDS_B)],
    )
    assert r.resolved_count == 0
    assert len(r.rejected) == 1
    assert r.rejected[0].ward_agreement == 0.0


def test_thin_evidence_is_carried_not_rejected():
    """Below the sample floor an agreement ratio is noise, so the pairing stands."""
    r = build_crosswalk(
        [ours("G02003", "Clappana", wards=("Alpha", "Beta"))],
        [theirs("G020103", "Clappana", wards=("Gamma", "Delta"))],
    )
    assert r.resolved_count == 1


def test_partial_ward_agreement_above_the_floor_is_accepted():
    r = build_crosswalk(
        [ours("G02003", "Clappana", wards=WARDS_A)],
        [theirs("G020103", "Clappana", wards=WARDS_A[:3] + WARDS_B[:2])],
    )
    assert r.resolved_count == 1
    assert r.matches[0].ward_agreement == pytest.approx(0.6)


def test_unmatched_local_body_is_reported_not_dropped():
    r = build_crosswalk(
        [ours("G02003", "Zzyzx", wards=WARDS_A)],
        [theirs("G020103", "Clappana", wards=WARDS_B)],
    )
    assert [lb.code for lb in r.unresolved] == ["G02003"]
    assert [lb.code for lb in r.unclaimed] == ["G020103"]


# --- overrides and gate -----------------------------------------------------


def test_override_beats_a_plausible_fuzzy_match():
    r = build_crosswalk(
        [ours("G02003", "Clappana", wards=WARDS_A)],
        [
            theirs("G020103", "Clappanna", wards=WARDS_A),
            theirs("G020999", "Clappana", wards=WARDS_A),
        ],
        overrides={"G02003": "G020103"},
    )
    assert r.matches[0].theirs.code == "G020103"
    assert r.matches[0].method == "override"


def test_gate_passes_only_when_everything_resolves():
    r = build_crosswalk(
        [ours("G02003", "Clappana", wards=WARDS_A)],
        [theirs("G020103", "Clappana", wards=WARDS_A)],
    )
    assert r.gate(expected=1) == []
    assert r.gate(expected=2)


def test_gate_names_the_offending_body():
    r = build_crosswalk([ours("G02003", "Zzyzx", wards=WARDS_A)], [])
    problems = r.gate(expected=1)
    assert problems and "G02003" in problems[0]


def test_overrides_file_round_trips(tmp_path):
    p = tmp_path / "ov.csv"
    p.write_text("lb_code,ksmart_lb_code\nG02003,G020103\n", encoding="utf-8")
    assert load_overrides(p) == {"G02003": "G020103"}


def test_missing_overrides_file_is_not_an_error(tmp_path):
    assert load_overrides(tmp_path / "absent.csv") == {}


def test_written_crosswalk_is_reviewable(tmp_path):
    r = build_crosswalk(
        [ours("G02003", "Clappana", wards=WARDS_A)],
        [theirs("G020103", "Clappana", wards=WARDS_A)],
    )
    out = tmp_path / "cw.csv"
    write_crosswalk(r, out)
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["lb_code"] == "G02003"
    assert rows[0]["ksmart_lb_code"] == "G020103"
    assert rows[0]["match_method"] == "exact"
    assert rows[0]["ward_agreement"] == "1.000"


# --- district reconciliation ------------------------------------------------

from geo.build.crosswalk import DistrictMismatch, pair_districts  # noqa: E402

KERALA_OURS = ["KASARGOD", "KOLLAM", "THRISSUR", "WAYANAD"]
KERALA_THEIRS = ["Kasaragod", "Kollam", "Thrissur", "Wayanad"]


def test_identical_districts_map_to_themselves():
    m = pair_districts(["KOLLAM"], ["Kollam"])
    assert m == {"KOLLAM": "KOLLAM"}


def test_kasaragod_spelling_difference_is_reconciled():
    """The real one-letter difference that cost an entire district.

    Ours spells it KASARGOD, KSMART spells it Kasaragod. Thirteen of fourteen
    districts agree exactly; this one did not, and because district is the
    scoping key it silently disqualified all 38 local bodies inside it.
    """
    m = pair_districts(KERALA_OURS, KERALA_THEIRS)
    assert m["KASARAGOD"] == "KASARGOD"
    assert len(m) == 4


def test_district_with_no_counterpart_raises_rather_than_degrading():
    """Failing loudly matters more here than anywhere else in the module: the
    quiet alternative looks like 'those bodies just didn't match'."""
    with pytest.raises(DistrictMismatch, match="Ernakulam"):
        pair_districts(KERALA_OURS, KERALA_THEIRS + ["Ernakulam"])


def test_ambiguous_district_is_not_guessed():
    with pytest.raises(DistrictMismatch, match="ambiguous"):
        pair_districts(["KOLLAM", "KOLLAMX"], ["Kollamy"])


def test_blank_districts_are_ignored_not_matched():
    assert pair_districts(["KOLLAM", ""], ["Kollam", ""]) == {"KOLLAM": "KOLLAM"}


def test_crosswalk_pairs_across_a_district_spelling_difference():
    """End-to-end: the bug this guards against, at the level it actually bit."""
    r = build_crosswalk(
        [ours("G14001", "Kumbadaje", district="KASARGOD", wards=WARDS_A)],
        [theirs("G140202", "Kumbadaje", district="Kasaragod", wards=WARDS_A)],
    )
    assert r.resolved_count == 1
    assert not r.unresolved


def test_crosswalk_surfaces_an_unknown_district():
    with pytest.raises(DistrictMismatch):
        build_crosswalk(
            [ours("G02003", "Clappana", district="KOLLAM", wards=WARDS_A)],
            [theirs("G020103", "Clappana", district="Atlantis", wards=WARDS_A)],
        )


def test_a_side_with_no_ward_names_cannot_contradict():
    """Absence of evidence is not contradiction.

    The opendatakerala source is a local-body polygon set carrying no ward data
    at all. Scoring against an empty list gives 0.0 agreement for every pairing,
    and treating that as disagreement rejected all 1,200 bodies -- failing them
    for a comparison nobody could have made.
    """
    r = build_crosswalk(
        [ours("G02003", "Clappana", wards=WARDS_A)],
        [theirs("G02003", "Clappana", wards=())],
    )
    assert r.resolved_count == 1
    assert not r.rejected


def test_a_side_with_ward_names_still_can_contradict():
    """The relaxation above must not disarm the gate where evidence does exist."""
    r = build_crosswalk(
        [ours("G02003", "Clappana", wards=WARDS_A)],
        [theirs("G020103", "Clappana", wards=WARDS_B)],
    )
    assert r.resolved_count == 0
    assert len(r.rejected) == 1
