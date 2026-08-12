"""Winner derivation and tie-breaking, all on a handful of inline candidates."""

from __future__ import annotations

from data_merge.transform.winner import Candidate, derive_winner


def test_highest_votes_wins_outright() -> None:
    candidates = [
        Candidate("c1", "RAJAN NAIR", "INC", 5000),
        Candidate("c2", "SAJITHA BEEVI", "CPM", 4200),
    ]
    result = derive_winner(candidates)
    assert result.winner_id == "c1"
    assert result.tie is False
    assert result.resolution == "outright"


def test_a_two_way_tie_resolved_by_member_name_picks_the_named_candidate() -> None:
    candidates = [
        Candidate("c1", "THOMAS SCARIA (P.T.SCARIA)", "INC", 3000),
        Candidate("c2", "BABU JOSEPH", "LDF", 3000),
    ]
    result = derive_winner(candidates, member_name="THOMAS SCARIA")
    assert result.winner_id == "c1"
    assert result.tie is True
    assert result.resolution == "member_name"
    assert set(result.tied_ids) == {"c1", "c2"}


def test_a_tie_with_no_external_name_yields_no_winner() -> None:
    candidates = [
        Candidate("c1", "RAJAN NAIR", "INC", 3000),
        Candidate("c2", "BABU JOSEPH", "LDF", 3000),
    ]
    result = derive_winner(candidates)
    assert result.winner_id is None
    assert result.tie is True
    assert result.resolution == "undecidable"


def test_a_tie_with_a_name_matching_none_of_the_leaders_yields_no_winner() -> None:
    candidates = [
        Candidate("c1", "RAJAN NAIR", "INC", 3000),
        Candidate("c2", "BABU JOSEPH", "LDF", 3000),
    ]
    result = derive_winner(candidates, member_name="SOMEONE ELSE ENTIRELY")
    assert result.winner_id is None
    assert result.resolution == "undecidable"


def test_a_tie_falls_back_to_member_party_when_no_name_is_supplied() -> None:
    candidates = [
        Candidate("c1", "RAJAN NAIR", "INC", 3000),
        Candidate("c2", "BABU JOSEPH", "LDF", 3000),
    ]
    result = derive_winner(candidates, member_party="LDF")
    assert result.winner_id == "c2"
    assert result.resolution == "member_party"


def test_no_candidates_yields_no_winner() -> None:
    result = derive_winner([])
    assert result.winner_id is None
    assert result.resolution == ""


class TestPartyTieBreakIsStrict:
    """Party is weak evidence, so it has to be unambiguous to count.

    A handful of fronts contest every ward. For 2025 party is the *only*
    evidence available -- the SEC publishes candidate names in Malayalam and
    WIYR publishes members in English -- so all 49 of that year's ties rest on
    it. Awarding a ward to whichever tied candidate happened to come first
    would decide it on list order.
    """

    def _tied(self, *parties: str) -> list[Candidate]:
        return [
            Candidate(candidate_id=str(i), name=f"CAND {i}", party=p, votes=500)
            for i, p in enumerate(parties, start=1)
        ]

    def test_a_single_party_match_decides_the_tie(self) -> None:
        result = derive_winner(self._tied("CPI(M)", "INC"), member_party="INC")
        assert result.winner_id == "2"
        assert result.resolution == "member_party"

    def test_two_candidates_sharing_the_party_yields_no_winner(self) -> None:
        result = derive_winner(self._tied("INC", "INC"), member_party="INC")
        assert result.winner_id is None
        assert result.resolution == "undecidable"

    def test_no_party_match_yields_no_winner(self) -> None:
        result = derive_winner(self._tied("CPI(M)", "BJP"), member_party="INC")
        assert result.winner_id is None
        assert result.resolution == "undecidable"

    def test_a_name_match_still_outranks_party(self) -> None:
        """Party only runs when the name signal cannot separate the leaders."""
        candidates = [
            Candidate(candidate_id="1", name="RAJESH CHANDRADAS", party="CPI(M)", votes=500),
            Candidate(candidate_id="2", name="SOMEONE ELSE", party="INC", votes=500),
        ]
        result = derive_winner(
            candidates, member_name="RAJESH CHANDRADAS R S", member_party="INC"
        )
        assert result.winner_id == "1"
        assert result.resolution == "member_name"
