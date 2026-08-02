"""The SEC-spine assembly shared by 2015, 2020 and (eventually) 2025.

Every SEC-spine year enumerates candidates from the trend site's ``can``
endpoint (winner and losers alike, one response per ward) and takes the
per-candidate front from the ``detailed_results_*`` endpoints. What differs
between cycles is declared in :class:`~data_merge.spec.YearSpec` and read from
it here -- never branched on a year literal:

    member detail source     ``spec.members`` (LSGD for 2015/2020, WIYR for
                              2025 -- only the LSGD path is implemented here;
                              a WIYR-backed year supplies its own member feed
                              through :class:`SecSpineInputs` and calls
                              :func:`build` directly, bypassing
                              :func:`load_lsgd_members`)
    front provenance         ``spec.front`` (published for 2015/2020, derived
                              for 2025 -- ``build`` only fills ``party_group``
                              from ``detailed_results_*`` when it is present;
                              a derived-front year leaves it for the caller to
                              harmonise before calling ``build``, since there
                              is nothing here that invents a front)
    independent gender check ``spec.pdf_sex`` -- MEASURE folds the PDF's own
                              Sex column into gender precedence; IGNORE (2020,
                              whose column is inverted at source) does not,
                              but the reservation-alignment check below always
                              uses the PDF, oriented, regardless -- that
                              independence is what R6 needs and no year
                              disables it.
    contesting-candidate feed ``spec.has_contest_feed`` (2020 only)
    invalid-vote rows        ``spec.has_invalid_votes``

Two things load real data and are the only I/O in this module:
:func:`load_lsgd_members`, which walks the LSGD portal's cached type/district
index pages down to ward tables exactly as ``scrape_lsgd.py`` did when it
built the cache, and :func:`load_inputs`, which opens every cache and PDF a
year's spec names and hands :func:`build` an already-parsed
:class:`SecSpineInputs`. ``build`` itself never touches a path -- callers that
already have parsed collections (a future WIYR-backed year, or a test fixture)
call it directly.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from data_merge.config import Paths
from data_merge.parsers.contest import ContestCandidate, parse_contest_ward
from data_merge.parsers.lsgd import parse_member_page, parse_person
from data_merge.parsers.pdf_candidates import LB_FAMILY, Layout, PatchRow, parse_patch
from data_merge.parsers.sec_trend import (
    INVALID_VOTES_CODE,
    CanRow,
    DetailCandidate,
    parse_can,
    parse_detail,
    parse_dv,
    parse_wv,
    ward_code_from_detail_key,
)
from data_merge.schema import CandidateRow, conform
from data_merge.sources.cache import ResponseCache
from data_merge.sources.pdf import extract
from data_merge.spec import PdfSex, YearSpec
from data_merge.transform import gender, rollup, winner
from data_merge.transform.gender import GenderSource
from data_merge.transform.matching import (
    LBKey,
    WardTally,
    apply_gate,
    has_latin,
    names_agree,
    normalize,
    pair_local_bodies,
    wardnames_agree,
)
from data_merge.transform.party import parties_agree
from data_merge.transform.winner import Candidate as WinnerCandidate

PARTY_GROUP_SOURCE: Final = "published"
"""Both SEC-spine years take ``party_group`` straight from the
detailed-results feed for every candidate -- see the module docstring on
``spec.front``. A derived-front year (2025) stamps its own value instead of
calling this constant."""

HEAD_ROLES: Final[frozenset[str]] = frozenset({"President", "Chairperson", "Chairman", "Mayor"})

# LSGD renders a handful of district names differently from the SEC's own
# spelling. Comparison-only -- a candidate row's own district_name always
# keeps the SEC's spelling; this normalised form exists only for matching.
_DISTRICT_ALIASES: Final[dict[str, str]] = {
    "COCHIN": "ERNAKULAM",
    "KASARAGOD": "KASARGOD",
    "KASARGODE": "KASARGOD",
    "KOZHIKODE": "KOZHIKKODE",
    "TRIVANDRUM": "THIRUVANANTHAPURAM",
}

# No TITLE_GENDER table: the honorific ("Shri"/"Smt" and their Malayalam
# equivalents) is not used as a gender source. It is a title the same feed
# prints next to the name, and it is wrong often enough that trusting it
# produced the honorific-fallback reservation
# bug this module fixes elsewhere (see ``_reservation_orientation``). See
# ``_resolve_genders`` for the sources that replace it.

_SEX_MAP: Final[dict[str, str]] = {
    "female": "F",
    "male": "M",
    "f": "F",
    "m": "M",
    "transgender": "T",
    "t": "T",
}


def _canon_district(name: str) -> str:
    key = normalize(name)
    return _DISTRICT_ALIASES.get(key, key)


def _harmonise_front(party_group: str, nda_label: str) -> str:
    """``party_group`` keeps whatever the source published; this is the one
    harmonisation ``party_front`` applies -- the BJP-led front's label
    differs by year (``spec.nda_label``), everything else passes through."""
    if not party_group:
        return ""
    return "NDA" if party_group == nda_label else party_group


# ---------------------------------------------------------------------------
# Cache-key parsing. The trend site's ajax keys are "<endpoint>|<k=v&...>";
# nothing in parsers/ needs to walk a whole cache by key, so that lives here.
# ---------------------------------------------------------------------------


def _cache_params(key: str) -> dict[str, str]:
    _endpoint, _, query = key.partition("|")
    out: dict[str, str] = {}
    for pair in query.split("&"):
        name, _, value = pair.partition("=")
        if name:
            out[name] = value
    return out


_L_PARAM_TYPE: Final[dict[str, str]] = {
    "P": "Grama Panchayat",
    "B": "Block Panchayat",
    "D": "District Panchayat",
}
"""``stateView2_ajax.php``'s ``_l`` selector for the three families that name
themselves directly. ``C`` (the urban family) covers both Municipality and
Corporation, distinguished only by the local body's own code prefix."""

_RE_CONTEST_WARD: Final = re.compile(r"[?&]ward=([A-Z0-9]+)")


@dataclass(frozen=True, slots=True)
class LocalBodyInfo:
    """One local body, as the SEC's own state-view summary names it."""

    district_code: str
    district_name: str
    lb_type: str
    lb_code: str
    lb_name: str


def _load_local_bodies(cache: ResponseCache) -> dict[str, LocalBodyInfo]:
    out: dict[str, LocalBodyInfo] = {}
    for key in cache.keys("stateView2_ajax.php"):
        params = _cache_params(key)
        if params.get("_s") != "L":
            continue  # "_s=M" enumerates only majority-holding bodies.
        payload = cache.get(key)
        if payload is None:
            continue
        response = parse_dv(payload, key=key)
        family = params.get("_l", "")
        district_code = params.get("_d", "")
        for row in response.rows:
            lb_type = _L_PARAM_TYPE.get(family) or LB_FAMILY.get(row.lb_code[:1], "")
            out[row.lb_code] = LocalBodyInfo(
                district_code=district_code,
                district_name=response.district_name,
                lb_type=lb_type,
                lb_code=row.lb_code,
                lb_name=row.lb_name,
            )
    return out


def _load_can(cache: ResponseCache) -> dict[str, tuple[CanRow, ...]]:
    out: dict[str, tuple[CanRow, ...]] = {}
    for key in cache.keys("lb_ajax2.php"):
        params = _cache_params(key)
        if params.get("_p") != "can":
            continue
        ward_code = params.get("_w", "")
        if not ward_code:
            continue
        payload = cache.get(key)
        if payload is None:
            continue
        rows = parse_can(payload, key=key)
        if rows:
            out[ward_code] = rows
    return out


def _load_wv_ward_names(cache: ResponseCache) -> dict[str, str]:
    """Ward names, from the ward-list view. Cheap (one request per local
    body) and covers essentially every candidate-bearing ward; the rare ward
    ``wv`` omits (its poll produced no declared winner) is left for the
    caller to tolerate an empty ``ward_name`` on rather than treat as fatal."""
    out: dict[str, str] = {}
    for key in cache.keys("lb_ajax2.php"):
        params = _cache_params(key)
        if params.get("_p") != "wv":
            continue
        payload = cache.get(key)
        if payload is None:
            continue
        for row in parse_wv(payload, key=key):
            out[row.ward_code] = row.ward_name
    return out


_DETAIL_ENDPOINTS: Final[tuple[str, ...]] = (
    "detailed_results_grama_ajax.php",
    "detailed_results_block_ajax.php",
    "detailed_results_dist_ajax.php",
    "detailed_results_urban_ajax.php",
)


def _load_detail(cache: ResponseCache) -> dict[str, tuple[DetailCandidate, ...]]:
    """Per-candidate front, keyed by ward. Each endpoint also answers a
    district-level "list the local bodies" request under the same prefix;
    only the per-ward ``wardCd=`` shape is the one this module wants."""
    out: dict[str, tuple[DetailCandidate, ...]] = {}
    for endpoint in _DETAIL_ENDPOINTS:
        for key in cache.keys(endpoint):
            if "wardCd=" not in key:
                continue
            ward_code = ward_code_from_detail_key(key)
            payload = cache.get(key)
            if payload is None:
                continue
            rows = parse_detail(payload, key=key)
            if rows:
                out[ward_code] = rows
    return out


def _load_contest(cache: ResponseCache) -> dict[str, tuple[ContestCandidate, ...]]:
    out: dict[str, tuple[ContestCandidate, ...]] = {}
    for key in cache.keys():
        match = _RE_CONTEST_WARD.search(key)
        if match is None:
            continue
        ward_code = match.group(1)
        payload = cache.get(key)
        if payload is None:
            continue
        rows = parse_contest_ward(payload, ward_code, key=key)
        if rows:
            out[ward_code] = rows
    return out


_LB_NAME_MAL_SOURCES: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("detailed_results_grama_ajax.php", "getPanchayathsByDistrict", "GramaCd", "GramaNameMal"),
    ("detailed_results_block_ajax.php", "getBlocksByDistrict", "BlockCd", "BlockNameMal"),
    ("detailed_results_urban_ajax.php", "getUrbansByDistrict", "UrbanCd", "UrbanNameMal"),
)
"""District Panchayat has no equivalent feed -- the commission never
publishes one -- so its ``lb_name_mal`` stays empty by construction; there is
nothing here for this loader to omit."""


def _load_lb_names_mal(cache: ResponseCache) -> dict[str, str]:
    out: dict[str, str] = {}
    for endpoint, process, code_key, mal_key in _LB_NAME_MAL_SOURCES:
        for key in cache.keys(endpoint):
            if f"process={process}" not in key:
                continue
            payload = cache.get(key)
            records = payload.get("data") if isinstance(payload, dict) else None
            if not records:
                continue
            for record in records:
                code = str(record.get(code_key, "")).strip()
                name = str(record.get(mal_key, "")).strip()
                if code and name:
                    out[code] = name
    return out


def _load_ward_names_mal(cache: ResponseCache) -> dict[str, str]:
    """Only the urban roster (``getUrbanPanchayathWards``) carries a
    Malayalam ward name; Grama, Block and District rosters do not."""
    out: dict[str, str] = {}
    for key in cache.keys("contest_cand_ajax.php"):
        if "process=getUrbanPanchayathWards" not in key:
            continue
        payload = cache.get(key)
        records = payload.get("data") if isinstance(payload, dict) else None
        if not records:
            continue
        for record in records:
            code = str(record.get("UrbanWardCd", "")).strip()
            name = str(record.get("UrbanWardNameMal", "")).strip()
            if code and name:
                out[code] = name
    return out


# ---------------------------------------------------------------------------
# LSGD site walk. The SEC feed enumerates local bodies directly for every
# SEC-spine year, but the LSGD portal does not -- its member pages are
# reached only by walking the same type/district index pages
# ``scrape_lsgd.py`` walked when it built the cache. That walk is itself
# cached, so replaying it here touches no network.
# ---------------------------------------------------------------------------

_LSGD_BASE_URL: Final = "https://lsgkerala.gov.in"

LSGD_TYPE: Final[dict[int, str]] = {
    1: "District Panchayat",
    2: "Block Panchayat",
    3: "Municipality",
    4: "Corporation",
    5: "Grama Panchayat",
}
_LSGD_DIRECT_TYPES: Final[frozenset[int]] = frozenset({1, 4})
"""District Panchayat and Corporation index pages link straight to member
pages; the other three types link to a per-district list first."""


@dataclass(frozen=True, slots=True)
class LsgdMember:
    """One elected member, from the ward table joined to their person page."""

    district: str
    lb_type: str
    lb_name: str
    ward_no: int
    ward_name: str
    member_name: str
    role: str
    party: str
    reservation: str
    gender: str


def _get_html(cache: ResponseCache, url: str) -> str | None:
    value = cache.get(f"GET|{url}")
    return value if isinstance(value, str) else None


def _discover_lsgd_local_bodies(cache: ResponseCache, year: int) -> list[tuple[int, str]]:
    re_member = re.compile(rf'href="(/en/lbelection/electdmemberdet/{year}/(\d+))"')
    re_lbrpt = re.compile(rf'href="(/en/lbelection/electlbrpt/\d+/(\d+)/{year})"')

    found: list[tuple[int, str]] = []
    for lb_type_id in sorted(LSGD_TYPE):
        index_html = _get_html(
            cache, f"{_LSGD_BASE_URL}/en/lbelection/electdistrict/{year}/{lb_type_id}"
        )
        if index_html is None:
            continue
        if lb_type_id in _LSGD_DIRECT_TYPES:
            for _href, lbid in dict.fromkeys(re_member.findall(index_html)):
                found.append((lb_type_id, lbid))
            continue
        for path, _district_id in dict.fromkeys(re_lbrpt.findall(index_html)):
            district_html = _get_html(cache, f"{_LSGD_BASE_URL}{path}")
            if district_html is None:
                continue
            for _href, lbid in dict.fromkeys(re_member.findall(district_html)):
                found.append((lb_type_id, lbid))

    seen: set[str] = set()
    ordered: list[tuple[int, str]] = []
    for lb_type_id, lbid in found:
        if lbid not in seen:
            seen.add(lbid)
            ordered.append((lb_type_id, lbid))
    return ordered


def load_lsgd_members(cache: ResponseCache, year: int) -> tuple[LsgdMember, ...]:
    """Every elected member the LSGD cache holds, joined to their person page.

    The only I/O this function performs: ``cache`` is already open, so this
    reads cached responses, never the filesystem or the network directly.
    """
    rows: list[LsgdMember] = []
    for lb_type_id, lbid in _discover_lsgd_local_bodies(cache, year):
        page_url = f"{_LSGD_BASE_URL}/en/lbelection/electdmemberdet/{year}/{lbid}"
        page_html = _get_html(cache, page_url)
        if page_html is None:
            continue
        page = parse_member_page(page_html, LSGD_TYPE[lb_type_id])
        for ward_row in page.rows:
            person_gender = ""
            if ward_row.person_url:
                person_html = _get_html(cache, f"{_LSGD_BASE_URL}{ward_row.person_url}")
                if person_html is not None:
                    person_gender = parse_person(person_html).gender
            rows.append(
                LsgdMember(
                    district=page.district,
                    lb_type=LSGD_TYPE[lb_type_id],
                    lb_name=page.lb_name,
                    ward_no=ward_row.ward_no,
                    ward_name=ward_row.ward_name,
                    member_name=ward_row.member_name,
                    role=ward_row.role,
                    party=ward_row.party,
                    reservation=ward_row.reservation,
                    gender=person_gender,
                )
            )
    return tuple(rows)


# ---------------------------------------------------------------------------
# Inputs -- already parsed, no path to the filesystem from here on.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SecSpineInputs:
    """Everything :func:`build` needs, already parsed.

    ``member_source`` names where ``lsgd_members`` came from -- LSGD for
    2015/2020, and the field a WIYR-backed year would populate the same
    collection from, since :func:`build` reads it structurally and does not
    care which portal produced it.
    """

    local_bodies: Mapping[str, LocalBodyInfo]
    ward_names: Mapping[str, str]
    can_by_ward: Mapping[str, tuple[CanRow, ...]]
    detail_by_ward: Mapping[str, tuple[DetailCandidate, ...]]
    lsgd_members: Sequence[LsgdMember]
    pdf_patch: Sequence[PatchRow]
    contest_by_ward: Mapping[str, tuple[ContestCandidate, ...]] = field(default_factory=dict)
    lb_names_mal: Mapping[str, str] = field(default_factory=dict)
    """``lb_code`` -> Malayalam name. No source publishes one for District
    Panchayat -- the feed itself has no such field."""
    ward_names_mal: Mapping[str, str] = field(default_factory=dict)
    """``ward_code`` -> Malayalam name. Only the urban (Municipality /
    Corporation) ward roster carries one; Grama, Block and District wards
    have no Malayalam name in any endpoint this cycle publishes."""


def load_inputs(
    spec: YearSpec,
    paths: Paths,
    *,
    pdf_layout: Layout,
    pdf_cache_dir: Path | None = None,
) -> SecSpineInputs:
    """Read every cache and PDF this year's spec names.

    ``pdf_layout`` is passed by the caller instead of derived from
    ``spec.year`` here: which of the report's two column layouts applies is a
    property of the document, and the per-year wiring module is where that
    genuinely year-specific knowledge belongs.
    """
    if not spec.sec_cache or not spec.member_cache:
        raise ValueError(f"{spec.year}: an SEC-spine build needs sec_cache and member_cache")

    with ResponseCache(paths.caches / spec.sec_cache) as sec:
        local_bodies = _load_local_bodies(sec)
        ward_names = _load_wv_ward_names(sec)
        can_by_ward = _load_can(sec)
        detail_by_ward = _load_detail(sec) if spec.has_detail else {}
        lb_names_mal = _load_lb_names_mal(sec) if spec.has_lb_malayalam else {}
        ward_names_mal = _load_ward_names_mal(sec) if spec.has_lb_malayalam else {}

    with ResponseCache(paths.caches / spec.member_cache) as lsgd_cache:
        lsgd_members = load_lsgd_members(lsgd_cache, spec.year)

    contest_by_ward: dict[str, tuple[ContestCandidate, ...]] = {}
    if spec.has_contest_feed:
        if not spec.contest_cache:
            raise ValueError(f"{spec.year}: has_contest_feed needs a contest_cache")
        with ResponseCache(paths.caches / spec.contest_cache) as contest_cache:
            contest_by_ward = _load_contest(contest_cache)

    pdf_text = extract(paths.sec_pdfs / spec.pdf, cache_dir=pdf_cache_dir)
    pdf_rows, _report = parse_patch(pdf_text.lines(), pdf_layout)

    return SecSpineInputs(
        local_bodies=local_bodies,
        ward_names=ward_names,
        can_by_ward=can_by_ward,
        detail_by_ward=detail_by_ward,
        lsgd_members=lsgd_members,
        pdf_patch=tuple(pdf_rows),
        contest_by_ward=contest_by_ward,
        lb_names_mal=lb_names_mal,
        ward_names_mal=ward_names_mal,
    )


# ---------------------------------------------------------------------------
# Build -- pure from here down.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Cand:
    ward_code: str
    lb_code: str
    candidate_code: int
    candidate_title: str
    candidate_name: str
    party_name: str
    party_group: str
    votes: int


@dataclass(frozen=True, slots=True)
class _TentativeMatch:
    """One ward's provisional LSGD pairing, plus whether the person-level
    signal (winner name, or ward+party where names are incomparable) held.
    ``person_ok`` gates demographic fields; reservation and role do not need
    it, because the local-body-level gate already screened the pairing."""

    member: LsgdMember
    person_ok: bool


@dataclass
class BuildReport:
    """What the build did -- every known gap named, never silent."""

    candidates: int = 0
    wards: int = 0
    local_bodies: int = 0
    invalid_vote_rows: int = 0

    lb_pairing_how: Counter[str] = field(default_factory=Counter)
    lb_unpaired: tuple[LBKey, ...] = ()
    lb_fallback_matched: tuple[LBKey, ...] = ()
    """Local bodies the shared pairing cascade left unpaired (too close to an
    unrelated candidate to trust) but a single-best fuzzy match, verified by
    the same rejection gate as everything else, recovered."""
    lb_gate_rejected: tuple[str, ...] = ()
    lb_reservation_misaligned: tuple[str, ...] = ()
    """Local bodies whose reservation was blanked because their women-reserved
    wards measured all-male under the independent PDF sex field (R6)."""

    reservation_orientation: str = ""
    reservation_share_female: float = 0.0

    gender_source_counts: Counter[str] = field(default_factory=Counter)
    gendered_rows: int = 0

    vote_ties: int = 0
    vote_ties_unresolved: tuple[str, ...] = ()

    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BuildResult:
    candidates: tuple[CandidateRow, ...]
    wards: tuple[dict[str, str], ...]
    local_bodies: tuple[dict[str, str], ...]
    report: BuildReport


def _assemble(inputs: SecSpineInputs) -> tuple[dict[str, list[_Cand]], dict[str, int]]:
    """The `can` feed straight to per-ward candidate lists.

    A ward's payload can be genuinely empty (no candidate data reached the
    site at all) -- such a ward contributes no rows and is not part of the
    output, the same way ``scrape.py``'s ``empty_wards`` never became rows.
    """
    by_ward: dict[str, list[_Cand]] = {}
    invalid_by_ward: dict[str, int] = {}
    for ward_code, rows in inputs.can_by_ward.items():
        detail_map = {d.candidate_code: d for d in inputs.detail_by_ward.get(ward_code, ())}
        entries: list[_Cand] = []
        invalid_votes = 0
        for row in rows:
            if row.candidate_code == INVALID_VOTES_CODE and not row.party_name:
                invalid_votes = row.votes
                continue
            detail = detail_map.get(row.candidate_code)
            entries.append(
                _Cand(
                    ward_code=ward_code,
                    lb_code=ward_code[:6],
                    candidate_code=row.candidate_code,
                    candidate_title=row.candidate_title,
                    candidate_name=row.candidate_name,
                    party_name=row.party_name,
                    party_group=detail.party_group if detail else "",
                    votes=row.votes,
                )
            )
        if not entries:
            continue
        by_ward[ward_code] = entries
        invalid_by_ward[ward_code] = invalid_votes
    return by_ward, invalid_by_ward


def _sec_targets(local_bodies: Mapping[str, LocalBodyInfo]) -> dict[str, LBKey]:
    """``lb_code`` -> ``(district_norm, lb_type, name_norm)``, the pairing target."""
    return {
        lb.lb_code: (_canon_district(lb.district_name), lb.lb_type, normalize(lb.lb_name))
        for lb in local_bodies.values()
    }


def _lsgd_pool(
    members: Sequence[LsgdMember],
) -> tuple[dict[LBKey, dict[int, LsgdMember]], dict[tuple[str, str], frozenset[str]]]:
    by_key: dict[LBKey, dict[int, LsgdMember]] = defaultdict(dict)
    for member in members:
        key: LBKey = (_canon_district(member.district), member.lb_type, normalize(member.lb_name))
        by_key[key][member.ward_no] = member

    pool: dict[tuple[str, str], set[str]] = defaultdict(set)
    for district, lb_type, name in by_key:
        pool[(district, lb_type)].add(name)

    return dict(by_key), {k: frozenset(v) for k, v in pool.items()}


_FALLBACK_DIFFLIB_CUTOFF: Final = 0.72
"""Matches ``transform.matching``'s own cutoff (a private constant there, so
restated here rather than imported). Used only as a last resort, for local
bodies the shared cascade left unpaired because two candidates were too
close to choose between automatically -- whichever single best match this
finds still goes through ``_verify_and_gate``'s rejection gate, so a wrong
guess here is caught the same way a wrong guess anywhere in the cascade is."""


def _fallback_pair_unresolved(
    unpaired: Sequence[LBKey],
    pool: Mapping[tuple[str, str], frozenset[str]],
    already_matched: Mapping[LBKey, str],
) -> dict[LBKey, str]:
    """A single best fuzzy match for local bodies the pairing cascade left
    unresolved as ambiguous.

    ``pair_local_bodies`` requires a clear margin between the top two fuzzy
    candidates before trusting either -- a real safety property, but one
    that also rejects a genuine match sitting near an unrelated look-alike:
    SEC's "Amballur" is 0.82 similar to LSGD's actual match "Amballoor" and
    0.78 similar to the unrelated "Karumallur", a margin too thin for that
    check even though the match itself is not actually ambiguous. Taking the
    single best candidate here, then handing it to the same rejection gate
    every other pairing passes through, recovers cases like this without
    weakening the cascade's own ambiguity guard for genuinely close calls.

    ``already_matched`` -- the primary cascade's own successful pairings --
    is excluded from every candidate pool before matching starts, and each
    fallback match is excluded in turn as soon as it is made. Without this,
    SEC's genuinely LSGD-less "Vadanappilly" fuzzy-matched to
    "VARANDARAPPILLY" -- the LSGD entry the primary cascade had *already*,
    correctly, given to a different SEC body, "Varantharappilly" -- and
    silently stamped that body's reservation and roles onto Vadanappilly's
    wards too. One LSGD local body can be claimed at most once.
    """
    claimed: dict[tuple[str, str], set[str]] = defaultdict(set)
    for (district, lb_type, _name), matched_name in already_matched.items():
        claimed[(district, lb_type)].add(matched_name)

    out: dict[LBKey, str] = {}
    for district, lb_type, name in sorted(unpaired):
        available = pool.get((district, lb_type), frozenset()) - claimed[(district, lb_type)]
        close = difflib.get_close_matches(
            name, sorted(available), n=1, cutoff=_FALLBACK_DIFFLIB_CUTOFF
        )
        if close:
            out[(district, lb_type, name)] = close[0]
            claimed[(district, lb_type)].add(close[0])
    return out


def _verify_and_gate(
    by_ward: Mapping[str, list[_Cand]],
    sec_targets: Mapping[str, LBKey],
    matched: Mapping[LBKey, str],
    lsgd_by_key: Mapping[LBKey, dict[int, LsgdMember]],
    ward_names: Mapping[str, str],
) -> tuple[dict[str, _TentativeMatch], tuple[str, ...]]:
    """Join each ward to its LSGD member row, then drop any local body whose
    wards agree on nothing -- the rejection gate, applied to the whole body
    even where an individual ward happened to agree.

    The verification signal uses the ward's provisional top-vote candidate
    rather than the final winner of record: the final winner can depend on
    this very pairing (a genuine tie is broken against the LSGD member name),
    so using it here would be circular. The provisional pick is wrong only on the rare
    ward with a genuine vote tie, and a fuzzy verification signal tolerates
    that.
    """
    tallies: dict[str, WardTally] = {}
    tentative: dict[str, _TentativeMatch] = {}

    for ward_code, group in by_ward.items():
        lb_code = group[0].lb_code
        key = sec_targets.get(lb_code)
        if key is None:
            continue
        lsgd_name = matched.get(key)
        if lsgd_name is None:
            continue
        member = lsgd_by_key.get((key[0], key[1], lsgd_name), {}).get(int(ward_code[6:9]))
        if member is None:
            continue

        top_vote = max(c.votes for c in group)
        leader = next(c for c in group if c.votes == top_vote)
        ward_ok = wardnames_agree(member.ward_name, ward_names.get(ward_code, ""))
        name_ok = bool(names_agree(member.member_name, leader.candidate_name))
        party_ok = parties_agree(leader.party_name, member.party)
        comparable = has_latin(leader.candidate_name) and has_latin(member.member_name)
        person_ok = name_ok or (party_ok and (ward_ok or not comparable))

        tallies[lb_code] = tallies.get(lb_code, WardTally()).add(
            ward=ward_ok, name=name_ok, party=party_ok
        )
        tentative[ward_code] = _TentativeMatch(member=member, person_ok=person_ok)

    gate = apply_gate(tallies)
    kept = {wc: t for wc, t in tentative.items() if wc[:6] in gate.kept}
    return kept, tuple(sorted(gate.rejected))


_MIN_MISALIGNED_SAMPLE: Final = 4
_MISALIGNED_THRESHOLD: Final = 0.5


def _reservation_orientation(
    by_ward: Mapping[str, list[_Cand]],
    kept: Mapping[str, _TentativeMatch],
    pdf_sex: Mapping[tuple[str, int], str],
) -> gender.Orientation:
    """Measure the PDF's Sex column against every candidate in a kept,
    women-reserved ward -- the independent source R6 requires, never the
    honorific fallback that blanked B04037 and G13008's reservation."""
    sexes: list[str] = []
    for ward_code, match in kept.items():
        if match.member.reservation not in gender.WOMEN_RESERVED:
            continue
        for cand in by_ward[ward_code]:
            sex = pdf_sex.get((ward_code, cand.votes), "")
            if sex:
                sexes.append(sex)
    return gender.measure_orientation(sexes)


def _misaligned_local_bodies(
    by_ward: Mapping[str, list[_Cand]],
    kept: Mapping[str, _TentativeMatch],
    pdf_sex: Mapping[tuple[str, int], str],
    orientation: gender.Orientation,
) -> frozenset[str]:
    """Local bodies whose women-reserved wards look all-male under the PDF's
    (oriented) Sex field -- evidence that this body's LSGD ward numbering is
    what disagrees, not the reservation itself. Their reservation is blanked;
    every other kept pairing is untouched."""
    tally: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for ward_code, match in kept.items():
        if match.member.reservation not in gender.WOMEN_RESERVED:
            continue
        seen = [
            gender.oriented_sex(pdf_sex.get((ward_code, cand.votes), ""), orientation)
            for cand in by_ward[ward_code]
        ]
        seen = [s for s in seen if s]
        if not seen:
            continue
        lb_code = ward_code[:6]
        tally[lb_code][1] += 1
        if all(s == "M" for s in seen):
            tally[lb_code][0] += 1
    return frozenset(
        code
        for code, (bad, total) in tally.items()
        if total >= _MIN_MISALIGNED_SAMPLE and bad / total >= _MISALIGNED_THRESHOLD
    )


def _reservation_trusted(ward_code: str, misaligned: frozenset[str]) -> bool:
    """Whether this ward's own reservation should be kept.

    A per-ward override -- keep the reservation wherever this ward's own
    name still agrees with its LSGD counterpart, even inside a flagged body
    -- was tried and measured empirically against the shipped files. It does
    not hold: 2020's M06065 (Ranni-Perunad -- 11 of the 20
    flagged local bodies show the same shape) has ward names that agree
    perfectly with LSGD on every ward (Vazhavara/VAZHAVARA,
    Society/SOCIETY, ...), yet the shipped file blanks the reservation for
    the *entire* body, all 127 rows at once, rather than a ward-level
    subset. The wards there are correctly identified; the evidence is
    that real, oriented PDF sex shows an all-male women-reserved ward
    despite the correct pairing, which a name-agreement override cannot see
    and would wrongly restore. So the verdict stays per local body, matching
    what the shipped files actually do: B04037 and G13008 are unaffected
    either way, because neither one is flagged as misaligned in the first
    place (R6 already resolves them without any override).
    """
    return ward_code[:6] not in misaligned


def _derive_winners(
    by_ward: Mapping[str, list[_Cand]],
    kept: Mapping[str, _TentativeMatch],
    contest_by_ward: Mapping[str, tuple[ContestCandidate, ...]],
) -> dict[str, winner.WinnerResult]:
    """One winner per ward, with ties broken against the LSGD member name.

    A ward whose poll produced no result at all -- every candidate at zero
    votes, more than one candidate -- is not a tie to resolve: it is a
    countermanded election, and gets no winner regardless of what LSGD says.

    The tie-break compares names with ``names_agree``, which cannot bridge a
    script it was never asked to transliterate. 2020 publishes candidate
    names in Malayalam while LSGD's are English, so the tie-break must use
    the contesting-candidate feed's English name where one exists -- the same
    substitution ``_verify_and_gate``'s ``comparable`` check already makes for
    its own, weaker signal. Without it, every 2020 tie is silently
    undecidable even where the correct winner is one ``names_agree`` call
    away.
    """
    results: dict[str, winner.WinnerResult] = {}
    for ward_code, group in by_ward.items():
        all_zero = all(c.votes == 0 for c in group)
        if all_zero and len(group) > 1:
            results[ward_code] = winner.WinnerResult(None, False, (), "no_result")
            continue
        contest_map = {c.candidate_code: c for c in contest_by_ward.get(ward_code, ())}
        candidates = []
        for c in group:
            name = c.candidate_name
            if not has_latin(name):
                contest = contest_map.get(c.candidate_code)
                if contest is not None and contest.name_eng:
                    name = contest.name_eng
            candidates.append(
                WinnerCandidate(
                    candidate_id=str(c.candidate_code), name=name, party=c.party_name, votes=c.votes
                )
            )
        match = kept.get(ward_code)
        member_name = match.member.member_name if match else ""
        result = winner.derive_winner(
            candidates,
            member_name=member_name,
            member_party=match.member.party if match else "",
        )
        results[ward_code] = result
    return results


def _resolve_genders(
    spec: YearSpec,
    by_ward: Mapping[str, list[_Cand]],
    kept: Mapping[str, _TentativeMatch],
    winners: Mapping[str, winner.WinnerResult],
    pdf_sex: Mapping[tuple[str, int], str],
    orientation: gender.Orientation,
    contest_by_ward: Mapping[str, tuple[ContestCandidate, ...]],
    misaligned: frozenset[str],
    ward_names: Mapping[str, str],
) -> dict[tuple[str, int], gender.GenderResolution]:
    """One resolution per (ward, candidate code) -- the reserved-ward rule
    binds every candidate, so this covers all of them, winners and losers alike.

    The honorific is deliberately absent from this source list: it is a title
    the same feed that supplies ``candidate_title`` prints next to the name,
    and it is wrong
    often enough to be exactly the failure mode this rebuild exists to
    close -- it is what produced the honorific-fallback reservation bug this
    module fixes elsewhere. The PDF's own Sex field, once oriented, resolves
    99.96% (2015) / 97.14% (2020) of candidates on its own; the contesting-
    candidate feed and LSGD's own record for a verified winner cover most of
    the remainder. What neither reaches is left unresolved rather than
    guessed from a title.

    Source order is spec-driven, never a year literal: the contesting-
    candidate feed (2020 only) is the commission's own per-candidate
    statement and outranks everything. The PDF -- another SEC-published Sex
    field, oriented against the reserved-ward rule -- outranks LSGD, whose
    member-level gender is a portal field with its own error rate and is
    trusted only for a verified ward winner.
    """
    out: dict[tuple[str, int], gender.GenderResolution] = {}
    for ward_code, group in by_ward.items():
        match = kept.get(ward_code)
        # The gender rule must trust exactly the same reservation verdict
        # the output row displays (``_reservation_trusted``, including its
        # ward-level override), or a ward whose reservation is shown as ""
        # would still silently force every candidate there to "F" underneath.
        reserved = (
            match is not None
            and match.member.reservation in gender.WOMEN_RESERVED
            and _reservation_trusted(ward_code, misaligned)
        )
        result = winners.get(ward_code)
        winner_id = result.winner_id if result else None
        contest_map = {c.candidate_code: c for c in contest_by_ward.get(ward_code, ())}

        for cand in group:
            sources: list[GenderSource] = []
            if spec.has_contest_feed:
                contest = contest_map.get(cand.candidate_code)
                sec_sex = _SEX_MAP.get((contest.sex if contest else "").strip().lower(), "")
                sources.append(GenderSource("sec_sex", sec_sex))
            if spec.pdf_sex is PdfSex.MEASURE:
                raw = pdf_sex.get((ward_code, cand.votes), "")
                sources.append(GenderSource("pdf", gender.oriented_sex(raw, orientation)))
            lsgd_sex = ""
            if match is not None and match.person_ok and winner_id == str(cand.candidate_code):
                # Demographic fields are only taken where the person-level
                # signal held (name agrees, or ward+party do where names are
                # incomparable) -- a ward-level match alone is not enough,
                # or this ward's LSGD row ("ASWATHY PRADEEP") would have
                # overwritten CHITHRAKUMARI.S's gender with a stranger's.
                lsgd_sex = {"female": "F", "male": "M"}.get(match.member.gender.strip().lower(), "")
            sources.append(GenderSource("lsgd", lsgd_sex))

            out[(ward_code, cand.candidate_code)] = gender.resolve_gender(
                reserved=reserved, sources=sources
            )
    return out


def _pdf_sex_lookup(patch: Sequence[PatchRow]) -> dict[tuple[str, int], str]:
    """``(ward_code, votes) -> M/F/T``, ambiguous keys dropped rather than
    guessed -- two candidates polling identical votes make the join key
    unusable, and a wrong join is worse than a missing one.

    ``T`` is admitted alongside M and F. Filtering it out here discarded the
    one candidate in the 2025 report who declared it, before any of the
    precedence rules written to protect that declaration could see it -- and
    the women-reserved-ward rule then recorded them as F.
    """
    seen: dict[tuple[str, int], set[str]] = defaultdict(set)
    for row in patch:
        if row.sex in ("M", "F", "T"):
            seen[row.key].add(row.sex)
    return {key: next(iter(values)) for key, values in seen.items() if len(values) == 1}


def _pdf_age_lookup(patch: Sequence[PatchRow]) -> dict[tuple[str, int], str]:
    seen: dict[tuple[str, int], set[str]] = defaultdict(set)
    for row in patch:
        if row.age:
            seen[row.key].add(row.age)
    return {key: next(iter(values)) for key, values in seen.items() if len(values) == 1}


def build(spec: YearSpec, inputs: SecSpineInputs) -> BuildResult:
    """Assemble one SEC-spine year's candidates, wards and local bodies.

    Pure: every collection returned is new, and the only mutation is the
    running :class:`BuildReport`, never exposed until the build is done.
    """
    report = BuildReport()

    by_ward, invalid_by_ward = _assemble(inputs)
    report.candidates = sum(len(g) for g in by_ward.values())
    report.wards = len(by_ward)
    report.local_bodies = len({wc[:6] for wc in by_ward})
    report.invalid_vote_rows = sum(1 for v in invalid_by_ward.values() if v)

    sec_targets = _sec_targets(inputs.local_bodies)
    lsgd_by_key, lsgd_pool = _lsgd_pool(inputs.lsgd_members)
    targets = list({sec_targets[wc[:6]] for wc in by_ward if wc[:6] in sec_targets})
    pairing = pair_local_bodies(targets, lsgd_pool)
    report.lb_pairing_how = pairing.how

    fallback_matched = _fallback_pair_unresolved(pairing.unpaired, lsgd_pool, pairing.matched)
    report.lb_fallback_matched = tuple(sorted(fallback_matched))
    matched = dict(pairing.matched)
    matched.update(fallback_matched)
    report.lb_unpaired = tuple(key for key in pairing.unpaired if key not in fallback_matched)

    kept, rejected = _verify_and_gate(by_ward, sec_targets, matched, lsgd_by_key, inputs.ward_names)
    report.lb_gate_rejected = rejected

    pdf_sex = _pdf_sex_lookup(inputs.pdf_patch)
    pdf_age = _pdf_age_lookup(inputs.pdf_patch)
    orientation = _reservation_orientation(by_ward, kept, pdf_sex)
    report.reservation_orientation = orientation.verdict.value
    report.reservation_share_female = orientation.share_female
    misaligned = _misaligned_local_bodies(by_ward, kept, pdf_sex, orientation)
    report.lb_reservation_misaligned = tuple(sorted(misaligned))

    winners = _derive_winners(by_ward, kept, inputs.contest_by_ward)
    for ward_code, result in winners.items():
        if result.tie:
            report.vote_ties += 1
            if result.winner_id is None:
                report.vote_ties_unresolved = (*report.vote_ties_unresolved, ward_code)

    genders = _resolve_genders(
        spec,
        by_ward,
        kept,
        winners,
        pdf_sex,
        orientation,
        inputs.contest_by_ward,
        misaligned,
        inputs.ward_names,
    )

    # ---- local-body control: seats by each ward's winner's harmonised front
    lb_seats: dict[str, Counter[str]] = defaultdict(Counter)
    lb_head: dict[str, dict[str, str]] = {}
    winner_lookup: dict[str, _Cand] = {}
    for ward_code, group in by_ward.items():
        result = winners[ward_code]
        if result.winner_id is None:
            continue
        winning = next(c for c in group if str(c.candidate_code) == result.winner_id)
        winner_lookup[ward_code] = winning
        front = _harmonise_front(winning.party_group, spec.nda_label)
        if front:
            lb_seats[ward_code[:6]][front] += 1
        match = kept.get(ward_code)
        if match is not None and match.member.role in HEAD_ROLES:
            lb_head[ward_code[:6]] = {"role": match.member.role, "party_group": front}

    lb_control = {code: rollup.rollup(counts) for code, counts in lb_seats.items()}

    candidates: list[CandidateRow] = []
    wards: list[dict[str, str]] = []
    gendered = 0

    for ward_code in sorted(by_ward):
        group = by_ward[ward_code]
        lb_code = ward_code[:6]
        lb_info = inputs.local_bodies.get(lb_code)
        match = kept.get(ward_code)
        reservation = ""
        role = ""
        member_name = ""
        if match is not None:
            role = match.member.role
            member_name = match.member.member_name
            if _reservation_trusted(ward_code, misaligned):
                reservation = match.member.reservation
        ward_winner = winner_lookup.get(ward_code)
        result = winners[ward_code]
        control = lb_control.get(lb_code)
        head = lb_head.get(lb_code, {})
        contest_map = {c.candidate_code: c for c in inputs.contest_by_ward.get(ward_code, ())}

        for cand in group:
            resolution = genders[(ward_code, cand.candidate_code)]
            report.gender_source_counts[resolution.source or "(unresolved)"] += 1
            is_winner = (
                ward_winner is not None and cand.candidate_code == ward_winner.candidate_code
            )
            no_result = result.resolution == "no_result"
            status = "won" if is_winner else ("no result" if no_result else "lost")

            # LSGD's own English spelling wins for a verified winner -- the
            # reference pipeline runs the LSGD merge before the contest-feed
            # merge, and the contest feed only ever fills a name that is
            # still blank. Reversing the order (contest feed first) let the
            # feed's transliteration silently replace LSGD's own record for
            # thousands of winners even where LSGD was the better-verified
            # source.
            name_eng = ""
            if is_winner and match is not None and match.person_ok:
                # Same gate as ``lsgd_sex``: an LSGD row that matched the
                # ward but not the person should not lend its member's name
                # to a different candidate.
                name_eng = member_name
            if not name_eng:
                contest = contest_map.get(cand.candidate_code)
                if contest is not None and contest.name_eng:
                    name_eng = contest.name_eng

            candidates.append(
                conform(
                    {
                        "district_code": lb_info.district_code if lb_info else "",
                        "district_name": lb_info.district_name if lb_info else "",
                        "lb_type": lb_info.lb_type if lb_info else LB_FAMILY.get(lb_code[:1], ""),
                        "lb_code": lb_code,
                        "lb_name": lb_info.lb_name if lb_info else "",
                        "lb_name_mal": inputs.lb_names_mal.get(lb_code, ""),
                        "ward_code": ward_code,
                        "ward_no": int(ward_code[6:9]),
                        "ward_name": inputs.ward_names.get(ward_code, ""),
                        "ward_name_mal": inputs.ward_names_mal.get(ward_code, ""),
                        "party_name": cand.party_name,
                        "party_group": cand.party_group,
                        "party_front": _harmonise_front(cand.party_group, spec.nda_label),
                        "party_group_source": PARTY_GROUP_SOURCE,
                        "candidate_code": cand.candidate_code,
                        "candidate_title": cand.candidate_title,
                        "candidate_gender": resolution.gender,
                        "gender_source": resolution.source,
                        "candidate_age": pdf_age.get((ward_code, cand.votes), ""),
                        "candidate_name": cand.candidate_name,
                        "candidate_name_eng": name_eng,
                        "status": status,
                        "total_votes": cand.votes,
                        "invalid_votes": (
                            str(invalid_by_ward.get(ward_code, 0)) if spec.has_invalid_votes else ""
                        ),
                        "ward_reservation": reservation,
                        "candidate_role": role if is_winner else "",
                        "ward_winner_party": ward_winner.party_name if ward_winner else "",
                        "ward_winner_party_group": ward_winner.party_group if ward_winner else "",
                        "lb_ruling_front": control.ruling_front if control else "",
                        "lb_control_type": control.control_type if control else "",
                        "lb_head_party_group": head.get("party_group", ""),
                    }
                )
            )
            if resolution.gender:
                gendered += 1

        runner_up_candidates = sorted(
            (
                c
                for c in group
                if ward_winner is None or c.candidate_code != ward_winner.candidate_code
            ),
            key=lambda c: -c.votes,
        )
        runner_up: _Cand | None = runner_up_candidates[0] if runner_up_candidates else None
        valid_votes = sum(c.votes for c in group)

        wards.append(
            {
                "district_code": lb_info.district_code if lb_info else "",
                "district_name": lb_info.district_name if lb_info else "",
                "lb_type": lb_info.lb_type if lb_info else "",
                "lb_code": lb_code,
                "lb_name": lb_info.lb_name if lb_info else "",
                "lb_name_mal": inputs.lb_names_mal.get(lb_code, ""),
                "ward_code": ward_code,
                "ward_no": str(int(ward_code[6:9])),
                "ward_name": inputs.ward_names.get(ward_code, ""),
                "ward_name_mal": inputs.ward_names_mal.get(ward_code, ""),
                "n_candidates": str(len(group)),
                "valid_votes": str(valid_votes),
                "invalid_votes": str(invalid_by_ward.get(ward_code, 0)),
                "winner_name": ward_winner.candidate_name if ward_winner else "",
                "winner_party": ward_winner.party_name if ward_winner else "",
                "winner_party_group": ward_winner.party_group if ward_winner else "",
                "winner_votes": str(ward_winner.votes) if ward_winner else "",
                "runnerup_name": runner_up.candidate_name if runner_up else "",
                "runnerup_votes": str(runner_up.votes) if runner_up else "",
                "reservation": reservation,
                "winner_role": role if ward_winner else "",
                "lsgd_match": "ok" if match else "none",
                "lb_ruling_front": control.ruling_front if control else "",
                "lb_control_type": control.control_type if control else "",
            }
        )

    local_bodies: list[dict[str, str]] = []
    for lb_code in sorted({wc[:6] for wc in by_ward}):
        lb_info = inputs.local_bodies.get(lb_code)
        control = lb_control.get(lb_code) or rollup.rollup({})
        head = lb_head.get(lb_code, {})
        cross = rollup.head_cross_front(head.get("party_group", ""), control.largest_front)
        local_bodies.append(
            {
                "district_code": lb_info.district_code if lb_info else "",
                "district_name": lb_info.district_name if lb_info else "",
                "lb_type": lb_info.lb_type if lb_info else "",
                "lb_code": lb_code,
                "lb_name": lb_info.lb_name if lb_info else "",
                "lb_name_mal": inputs.lb_names_mal.get(lb_code, ""),
                "total_wards": str(control.total_seats),
                "lb_seats_udf": str(control.seats_by_front.get("UDF", 0)),
                "lb_seats_ldf": str(control.seats_by_front.get("LDF", 0)),
                "lb_seats_nda": str(control.seats_by_front.get("NDA", 0)),
                "lb_seats_oth": str(control.seats_by_front.get("OTH", 0)),
                "lb_majority_threshold": str(control.majority_threshold),
                "lb_largest_front": control.largest_front,
                "lb_largest_front_seats": str(control.largest_front_seats),
                "lb_ruling_front": control.ruling_front,
                "lb_control_type": control.control_type,
                "lb_head_role": head.get("role", ""),
                "lb_head_party_group": head.get("party_group", ""),
                "lb_head_cross_front": cross,
            }
        )

    report.gendered_rows = gendered
    report.notes = spec.expect.notes

    return BuildResult(
        candidates=tuple(candidates),
        wards=tuple(wards),
        local_bodies=tuple(local_bodies),
        report=report,
    )
