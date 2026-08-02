"""Roll ward wins up to local-body control: seats, majority, and who runs it.

Two things are kept in separate fields on purpose: ``ruling_front`` /
``control_type`` are arithmetic from ward wins, while a local body's actual
head (President, Mayor, ...) is a fact from the member roster. In a hung body
these can diverge -- the largest front does not always end up holding the
chair -- and collapsing them into one field would hide exactly that case.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

FRONTS: Final[tuple[str, ...]] = ("UDF", "LDF", "NDA", "OTH")


@dataclass(frozen=True, slots=True)
class ControlResult:
    total_seats: int
    majority_threshold: int
    seats_by_front: dict[str, int]

    largest_front: str
    """The front with the most seats, or "TIE" when two or more share the
    top, or "" when no seat is decided."""

    largest_front_seats: int

    ruling_front: str
    """Non-empty only when one front holds an outright majority."""

    control_type: str
    """"majority" | "hung" | "tie"."""


def rollup(seats_by_front: Mapping[str, int], *, fronts: Sequence[str] = FRONTS) -> ControlResult:
    """Seats-by-front to control type.

    ``majority`` needs a single front at or above the threshold. ``tie`` is
    reserved for a clean deadlock -- the tied leaders between them hold every
    decided seat, with nothing left over for anyone else. Anything else where
    no one has a majority is ``hung``, including a tie at the top that
    coexists with a seat held by some other front: that leftover seat means
    control is not a pure two-way split, and calling it a "tie" would hide
    the fact that a third party could still make or break control.
    """
    counts = {f: seats_by_front.get(f, 0) for f in fronts}
    total = sum(counts.values())
    threshold = total // 2 + 1

    top_n = max(counts.values(), default=0)
    leaders = [f for f in fronts if counts[f] == top_n and top_n > 0]

    if top_n >= threshold and len(leaders) == 1:
        control_type = "majority"
        ruling = leaders[0]
        largest = leaders[0]
    elif len(leaders) > 1 and sum(counts[f] for f in leaders) == total:
        control_type = "tie"
        ruling = ""
        largest = "TIE"
    else:
        control_type = "hung"
        ruling = ""
        largest = "TIE" if len(leaders) > 1 else (leaders[0] if leaders else "")

    return ControlResult(
        total_seats=total,
        majority_threshold=threshold,
        seats_by_front=counts,
        largest_front=largest,
        largest_front_seats=top_n,
        ruling_front=ruling,
        control_type=control_type,
    )


def head_cross_front(head_party_group: str, largest_front: str) -> str:
    """Whether the local body's recorded head sits outside the largest front.

    "Y"/"N" only when both a head and a determinate largest front are known;
    "" otherwise, since a hung-with-no-clear-largest body has nothing to
    compare the head against.
    """
    if not head_party_group or largest_front in ("", "TIE"):
        return ""
    return "Y" if head_party_group != largest_front else "N"
