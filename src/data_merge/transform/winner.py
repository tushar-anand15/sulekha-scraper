"""Derive a ward's winner from vote counts, and break ties without guessing.

Most cycles publish a winner directly. 2010 does not -- its trend site was
decommissioned, so the PDF is the only candidate-level source and the winner
must be derived as the top vote in the ward. Either way, a vote tie needs a
tiebreaker no vote count can supply: an external record of who actually holds
the seat. Where the caller has one -- the LSGD member's own name -- it is used
to pick among the tied leaders. Where it does not, the tie is reported as a
tie -- nothing here breaks it by picking the first row in whatever order
the source happened to list them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from data_merge.transform.matching import name_score
from data_merge.transform.party import parties_agree


@dataclass(frozen=True, slots=True)
class Candidate:
    """The minimum a ward-winner decision needs to know about one candidate."""

    candidate_id: str
    name: str
    party: str
    votes: int


@dataclass(frozen=True, slots=True)
class WinnerResult:
    winner_id: str | None
    """``None`` when no winner could be determined -- an undecidable tie,
    never an arbitrary pick."""

    tie: bool
    tied_ids: tuple[str, ...]
    resolution: str
    """"outright" | "member_name" | "member_party" | "undecidable" | "" (no
    candidates at all)."""


def derive_winner(
    candidates: Sequence[Candidate],
    *,
    member_name: str = "",
    member_party: str = "",
) -> WinnerResult:
    """The top-vote candidate, with ties broken against external evidence.

    A two-way (or more) tie is resolved against ``member_name`` first, using
    the same name comparator that verifies local-body pairings, then against
    ``member_party`` as a weaker fallback. Supplying neither -- or supplying
    one that matches none of the tied leaders -- yields no winner rather than
    an arbitrary one: a vote tie is a real electoral outcome, and inventing a
    winner for it would misrepresent the ward.
    """
    if not candidates:
        return WinnerResult(None, False, (), "")

    top = max(c.votes for c in candidates)
    leaders = tuple(c for c in candidates if c.votes == top)

    if len(leaders) == 1:
        return WinnerResult(leaders[0].candidate_id, False, (leaders[0].candidate_id,), "outright")

    tied_ids = tuple(c.candidate_id for c in leaders)

    if member_name:
        # Ranked by strength of match, and taken only when strictly the best.
        # Two tied candidates can both resemble the elected member's name --
        # "Mohammed Asharaf" and "Mohammed Shareef" against a member recorded
        # as "MUHAMMED SHAREEF" -- and taking whichever comes first awards the
        # ward on list order. Where the two are indistinguishable by name, the
        # name signal genuinely cannot separate them, so it defers to party
        # rather than guessing.
        scored = sorted(
            ((name_score(member_name, c.name), c) for c in leaders),
            key=lambda pair: pair[0],
            reverse=True,
        )
        best_score, best = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else (0, 0.0)
        if best_score[0] > 0 and best_score > runner_up:
            return WinnerResult(best.candidate_id, True, tied_ids, "member_name")

    if member_party:
        for c in leaders:
            if parties_agree(c.party, member_party):
                return WinnerResult(c.candidate_id, True, tied_ids, "member_party")

    return WinnerResult(None, True, tied_ids, "undecidable")
