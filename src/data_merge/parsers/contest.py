"""The SEC's 2020 contesting-candidate feed: sex, English name, party group.

This is the one 2020 source that covers losers as well as winners and is not
limited to elected members. It supplies three things the trend site's other
endpoints do not for 2020:

    Sex          the commission's own Female/Male value, every candidate
    NameFull     the English name (the results feed publishes Malayalam only)
    PartyGroup   the front, per candidate rather than per winner

One response per ward, but the ward code the response describes is not always
present in the payload itself -- the grama, block and district variants carry
it as ``GramaWardCd`` / ``BlockWardCd`` / ``DistWardCd`` on every record, but
the urban (municipality/corporation) variant carries no ward-code field at
all. So ``ward_code`` is always taken from the caller, who already knows it
(it is the parameter the request itself was made with) -- reading it from the
payload would be inconsistent across variants.

The 2015 site returns empty for every ward on the equivalent request, so this
parser applies to the 2020 cache only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class ContestError(RuntimeError):
    """A cached contest-feed payload does not have the shape this parser expects."""


def _endpoint(key: str) -> str:
    return key.split("|", 1)[0]


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _to_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True, slots=True)
class ContestCandidate:
    """One candidate contesting one ward, from the 2020 contest feed."""

    ward_code: str
    candidate_code: int
    name_prefix: str
    name_eng: str
    name_mal: str
    party_full_mal: str
    party_group: str
    sex: str


def parse_contest_ward(payload: Any, ward_code: str, *, key: str) -> tuple[ContestCandidate, ...]:
    """Decode one ``contest_cand_ajax.php`` (``getContestCand*``) response.

    An empty ``data`` list is a real outcome -- a ward can return nothing on
    this endpoint even though it has candidates elsewhere -- and is handled
    with an empty tuple; it does not raise.
    """
    if not isinstance(payload, dict):
        raise ContestError(
            f"{_endpoint(key)} ({key}): contest payload is not a dict, "
            f"got {type(payload).__name__}"
        )
    records = payload.get("data")
    if records is None:
        raise ContestError(f"{_endpoint(key)} ({key}): contest payload has no 'data' field")

    rows = []
    for record in records:
        if not isinstance(record, dict):
            raise ContestError(
                f"{_endpoint(key)} ({key}): contest record is not a dict: {record!r}"
            )
        rows.append(
            ContestCandidate(
                ward_code=ward_code,
                candidate_code=_to_int(record.get("CanCd"), -999),
                name_prefix=_clean(record.get("NamePrefix")),
                name_eng=_clean(record.get("NameFull")),
                name_mal=_clean(record.get("NameMal")),
                party_full_mal=_clean(record.get("Party")),
                party_group=_clean(record.get("PartyGroup")),
                sex=_clean(record.get("Sex")),
            )
        )
    return tuple(rows)
