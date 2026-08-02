"""The SEC trend site's ajax payloads -- local bodies, wards and candidates.

The site (`stateView2_ajax.php`, `lb_ajax2.php`, and the four
`detailed_results_*_ajax.php` endpoints) answers with plain JSON, but every
payload is a positional array: ``row[3]`` is the winner's name on one endpoint
and the candidate's title on another. Indexing those positions inline at every
call site is exactly the kind of thing that goes stale silently when a column
is added -- so each shape's positions are named once, here, as an ``IntEnum``,
and nowhere else in this module (or its callers) indexes a row by a bare
integer.

Four response shapes, one per endpoint family:

``dv``  (``stateView2_ajax.php``, ``_p=dv``)
    Local-body summary rows for one district/family/selector. The row itself
    carries no district code or name -- that comes from the request the
    caller made, and from ``summary[0][1]`` in the same response.

``wv``  (``lb_ajax2.php``, ``_p=wv``)
    One row per ward that has a declared winner, with the runner-up alongside
    it. A ward whose poll produced no result at all (rare, but real) is
    simply absent from this feed -- recovering it is a job for the ward
    roster, which is out of scope here.

``can`` (``lb_ajax2.php``, ``_p=can``)
    Every candidate in one ward, winner and losers alike, with an
    ``is_first`` flag marking the winner. The invalid-votes pseudo-candidate
    (code 99) rides along in this feed and is not filtered out here -- the
    caller decides what to do with it, the same way the PDF parser keeps
    invalid-vote rows as data.

``detailed_results_*`` (grama / block / dist / urban)
    The same per-ward candidate list as ``can``, but keyed by candidate code
    and carrying the one field ``can`` never does: ``party_group``, the
    per-candidate front (UDF/LDF/NDA/OTH). The grama endpoint's dict values
    are flat; block, dist and urban wrap each one under a ``"cand"`` key.
    Both shapes are handled without the caller needing to know which family
    it asked for.

    The ward code these responses describe never appears inside the payload
    -- only in the cache key's ``wardCd=`` parameter -- so it must be passed
    in alongside the payload, since the payload alone doesn't reveal it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Final

INVALID_VOTES_CODE: Final = 99
"""The pseudo-candidate code the site uses for a ward's invalid-vote count."""

RE_CODE_PREFIX: Final = re.compile(r"^\s*-?\d+\s*-\s*")
"""2020/2025 ward views prepend a candidate code to the name
("4 - വേങ്..."); stripped so the name is comparable across years."""

RE_WARD_CD_PARAM: Final = re.compile(r"[?&]wardCd=([A-Z0-9]+)")


class SecTrendError(RuntimeError):
    """A cached payload does not have the shape this parser expects.

    Every raise names the endpoint and the cache key, because a shape defect
    found later -- once the payload has been merged into a CSV -- is far
    harder to trace back to one ajax response than a message that already
    says which one.
    """


def _endpoint(key: str) -> str:
    return key.split("|", 1)[0]


def _clean(value: Any) -> str:
    """Values arrive padded ('Attingal   ', 'IDUKKI ')."""
    if value is None:
        return ""
    return str(value).strip()


def _clean_name(value: Any) -> str:
    return RE_CODE_PREFIX.sub("", _clean(value))


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def ward_code_from_detail_key(key: str) -> str:
    """Pull ``wardCd`` out of a ``detailed_results_*`` cache key.

    The four endpoints identify their ward only in the request's query
    string; the response body never repeats it. This is a string operation
    on the key the caller already has -- no cache lookup involved.
    """
    match = RE_WARD_CD_PARAM.search(key)
    if match is None:
        raise SecTrendError(f"{_endpoint(key)} ({key}): no wardCd= parameter in the key")
    return match.group(1)


# ---------------------------------------------------------------------------
# dv: local-body summary rows
# ---------------------------------------------------------------------------


class DvCol(IntEnum):
    """Positions within one row of a ``stateView2_ajax.php`` ``dv`` payload."""

    LB_CODE = 0
    LB_NAME = 1
    TOTAL_WARDS = 2
    MAJORITY_NUMBER = 3
    SEATS_UDF = 4
    SEATS_LDF = 5
    SEATS_BJP = 6
    SEATS_OTH = 7


_DV_ROW_LEN: Final = 8


@dataclass(frozen=True, slots=True)
class DvRow:
    """One local body, as reported by the state-view summary."""

    lb_code: str
    lb_name: str
    total_wards: int
    majority_number: int
    seats_udf: int
    seats_ldf: int
    seats_bjp: int
    seats_oth: int


@dataclass(frozen=True, slots=True)
class DvResponse:
    """One ``dv`` call: the local bodies of one district, for one LB family.

    ``district_name`` comes from ``summary[0][1]``; the row shape has no
    district field at all, so it never comes from ``rows``.
    """

    district_name: str
    rows: tuple[DvRow, ...]


def parse_dv(payload: Any, *, key: str) -> DvResponse:
    """Decode one ``stateView2_ajax.php`` ``_p=dv`` response."""
    if not isinstance(payload, dict):
        raise SecTrendError(
            f"{_endpoint(key)} ({key}): dv payload is not a dict, got {type(payload).__name__}"
        )
    summary = payload.get("summary") or []
    district_name = ""
    if summary and isinstance(summary[0], list) and len(summary[0]) > 1:
        district_name = _clean(summary[0][1])

    raw_rows = payload.get("payload")
    if raw_rows is None:
        raise SecTrendError(f"{_endpoint(key)} ({key}): dv payload has no 'payload' field")

    rows = []
    for raw in raw_rows:
        if len(raw) < _DV_ROW_LEN:
            raise SecTrendError(
                f"{_endpoint(key)} ({key}): dv row has {len(raw)} fields, "
                f"expected at least {_DV_ROW_LEN}: {raw!r}"
            )
        rows.append(
            DvRow(
                lb_code=_clean(raw[DvCol.LB_CODE]),
                lb_name=_clean(raw[DvCol.LB_NAME]),
                total_wards=_to_int(raw[DvCol.TOTAL_WARDS]),
                majority_number=_to_int(raw[DvCol.MAJORITY_NUMBER]),
                seats_udf=_to_int(raw[DvCol.SEATS_UDF]),
                seats_ldf=_to_int(raw[DvCol.SEATS_LDF]),
                seats_bjp=_to_int(raw[DvCol.SEATS_BJP]),
                seats_oth=_to_int(raw[DvCol.SEATS_OTH]),
            )
        )
    return DvResponse(district_name=district_name, rows=tuple(rows))


# ---------------------------------------------------------------------------
# wv: ward-level winner/runner-up rows
# ---------------------------------------------------------------------------


class WvCol(IntEnum):
    """Positions within one row of a ``lb_ajax2.php`` ``wv`` payload."""

    WARD_CODE = 0
    WINNER_GROUP = 1
    WINNER_CODE = 2
    WINNER_NAME = 3
    WINNER_VOTES = 4
    WARD_NAME = 5
    DECLARED = 6
    RUNNERUP_CODE = 7
    RUNNERUP_NAME = 8
    RUNNERUP_VOTES = 9


_WV_ROW_LEN: Final = 10


@dataclass(frozen=True, slots=True)
class WvRow:
    """One ward, as reported by the ward-list view.

    Kept alongside the per-candidate ``can``/detail data for cross-checking:
    ``wv`` only names the winner and runner-up, so it can't serve as the
    primary candidate source.
    """

    ward_code: str
    ward_no: int
    ward_name: str
    winner_group: str
    winner_code: int
    winner_name: str
    winner_votes: int
    declared: str
    runnerup_code: int
    runnerup_name: str
    runnerup_votes: int


def parse_wv(payload: Any, *, key: str) -> tuple[WvRow, ...]:
    """Decode one ``lb_ajax2.php`` ``_p=wv`` response into ward rows."""
    if not isinstance(payload, dict):
        raise SecTrendError(
            f"{_endpoint(key)} ({key}): wv payload is not a dict, got {type(payload).__name__}"
        )
    raw_rows = payload.get("payload")
    if raw_rows is None:
        raise SecTrendError(f"{_endpoint(key)} ({key}): wv payload has no 'payload' field")

    rows = []
    for raw in raw_rows:
        if len(raw) < _WV_ROW_LEN:
            raise SecTrendError(
                f"{_endpoint(key)} ({key}): wv row has {len(raw)} fields, "
                f"expected at least {_WV_ROW_LEN}: {raw!r}"
            )
        ward_code = _clean(raw[WvCol.WARD_CODE])
        rows.append(
            WvRow(
                ward_code=ward_code,
                ward_no=_to_int(ward_code[6:9]),
                ward_name=_clean(raw[WvCol.WARD_NAME]),
                winner_group=_clean(raw[WvCol.WINNER_GROUP]),
                winner_code=_to_int(raw[WvCol.WINNER_CODE]),
                winner_name=_clean_name(raw[WvCol.WINNER_NAME]),
                winner_votes=_to_int(raw[WvCol.WINNER_VOTES]),
                declared=_clean(raw[WvCol.DECLARED]),
                runnerup_code=_to_int(raw[WvCol.RUNNERUP_CODE]),
                runnerup_name=_clean_name(raw[WvCol.RUNNERUP_NAME]),
                runnerup_votes=_to_int(raw[WvCol.RUNNERUP_VOTES]),
            )
        )
    return tuple(rows)


# ---------------------------------------------------------------------------
# can: every candidate in one ward
# ---------------------------------------------------------------------------


class CanCol(IntEnum):
    """Positions within one row of a ``lb_ajax2.php`` ``can`` payload."""

    PARTY_NAME = 0
    CANDIDATE_CODE = 1
    CANDIDATE_TITLE = 2
    CANDIDATE_NAME = 3
    VOTES = 4
    IS_FIRST = 5
    STATUS_FLAG = 6
    """Always ``"Y"`` in every row observed across 2015 and 2020; its meaning
    is not documented anywhere the original scraper used it. Carried through
    in case a future cache shows it varying."""


_CAN_ROW_LEN: Final = 7


@dataclass(frozen=True, slots=True)
class CanRow:
    """One candidate in one ward -- the winner and every loser alike.

    The invalid-votes pseudo-candidate (``candidate_code ==
    INVALID_VOTES_CODE``) rides along in this feed; filtering it out is left
    to the caller, matching how the PDF parser keeps invalid-vote rows as
    data.
    """

    party_name: str
    candidate_code: int
    candidate_title: str
    candidate_name: str
    votes: int
    is_first: bool
    status_flag: str


def parse_can(payload: Any, *, key: str) -> tuple[CanRow, ...]:
    """Decode one ``lb_ajax2.php`` ``_p=can`` response into candidate rows."""
    if not isinstance(payload, dict):
        raise SecTrendError(
            f"{_endpoint(key)} ({key}): can payload is not a dict, got {type(payload).__name__}"
        )
    raw_rows = payload.get("payload")
    if raw_rows is None:
        raise SecTrendError(f"{_endpoint(key)} ({key}): can payload has no 'payload' field")

    rows = []
    for raw in raw_rows:
        if len(raw) < _CAN_ROW_LEN:
            raise SecTrendError(
                f"{_endpoint(key)} ({key}): can row has {len(raw)} fields, "
                f"expected at least {_CAN_ROW_LEN}: {raw!r}"
            )
        rows.append(
            CanRow(
                party_name=_clean(raw[CanCol.PARTY_NAME]),
                candidate_code=_to_int(raw[CanCol.CANDIDATE_CODE], -1),
                candidate_title=_clean(raw[CanCol.CANDIDATE_TITLE]),
                candidate_name=_clean_name(raw[CanCol.CANDIDATE_NAME]),
                votes=_to_int(raw[CanCol.VOTES]),
                is_first=_to_int(raw[CanCol.IS_FIRST]) == 1,
                status_flag=_clean(raw[CanCol.STATUS_FLAG]),
            )
        )
    return tuple(rows)


# ---------------------------------------------------------------------------
# detailed_results_*: per-candidate party group, keyed by candidate code
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DetailCandidate:
    """One candidate's front, from a ``detailed_results_*`` endpoint.

    This is the only feed that carries ``party_group`` for every candidate,
    not just the winner -- ``wv`` only states the winner's front.
    """

    candidate_code: int
    name: str
    party: str
    votes: int
    pos: int
    party_group: str


def parse_detail(payload: Any, *, key: str) -> tuple[DetailCandidate, ...]:
    """Decode one ``detailed_results_*_ajax.php`` response.

    The grama endpoint's values are flat dicts; block, dist and urban wrap
    the same shape under a ``"cand"`` key. Both are read without the caller
    naming which family it asked -- the wrapper, if present, is simply
    unwrapped.

    A ward with no candidate data at all answers with a bare ``[]`` instead
    of the usual dict -- no ``mdata`` envelope, nothing to key by candidate
    code -- and that case is handled by returning an empty tuple. Any other
    list shape is still unexpected and raises.
    """
    if payload == []:
        return ()
    if not isinstance(payload, dict):
        raise SecTrendError(
            f"{_endpoint(key)} ({key}): detail payload is not a dict, "
            f"got {type(payload).__name__}"
        )

    rows = []
    for code_str, entry in payload.items():
        if isinstance(entry, dict) and isinstance(entry.get("cand"), dict):
            entry = entry["cand"]
        if not isinstance(entry, dict):
            raise SecTrendError(
                f"{_endpoint(key)} ({key}): detail entry for candidate "
                f"{code_str!r} is not a dict: {entry!r}"
            )
        rows.append(
            DetailCandidate(
                candidate_code=_to_int(code_str, -1),
                name=_clean(entry.get("name")),
                party=_clean(entry.get("party")),
                votes=_to_int(entry.get("votes")),
                pos=_to_int(entry.get("pos")),
                party_group=_clean(entry.get("pg")),
            )
        )
    return tuple(rows)
