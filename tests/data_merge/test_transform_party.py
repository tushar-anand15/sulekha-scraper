"""Party-label comparison across vocabularies, and caller-supplied front lookup."""

from __future__ import annotations

from data_merge.transform.party import FrontEntry, parties_agree, party_key, resolve_front


def test_ml_and_iuml_compare_equal() -> None:
    assert parties_agree("ML", "IUML")


def test_both_spellings_are_untouched_by_the_comparison() -> None:
    # party_key must never rewrite the strings a caller intends to publish --
    # it only produces a key for equality testing, leaving the inputs as-is.
    a, b = "ML", "IUML"
    party_key(a)
    party_key(b)
    assert (a, b) == ("ML", "IUML")


def test_kcm_and_kc_paren_m_paren_compare_equal() -> None:
    assert parties_agree("KCM", "KC(M)")


def test_ind_and_independent_compare_equal() -> None:
    assert parties_agree("IND", "INDEPENDENT")


def test_unrelated_parties_disagree() -> None:
    assert not parties_agree("CPI", "CPM")


def test_a_mapped_party_resolves_its_front() -> None:
    table = {"CPM": FrontEntry(party_name="CPM", party_group="LDF", party_front="LDF")}
    resolution = resolve_front("CPM", table)
    assert resolution.mapped
    assert resolution.party_group == "LDF"


def test_an_unmapped_party_defaults_to_oth_and_is_reported() -> None:
    resolution = resolve_front("JSS", {})
    assert resolution.mapped is False
    assert resolution.party_group == "OTH"
    assert resolution.party_name == "JSS"
