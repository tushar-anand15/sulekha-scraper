"""Building ``core.local_body`` -- the spine every other table hangs off.

Four sources identify a local body four different ways:

  elections  ``lb_code``                        (the spine)
  sakarma    ``lb.id``, Malayalam name only
  sulekha    ``(district_index, lb_index)`` per year, English name only
  geo        KSMART / OSM codes, already crosswalked to ``lb_code``

The spine is ``lb_code``, because it is the only identifier that is stable, that
someone has already verified against ward names, and that the geometry is
already keyed on.

Matching runs a three-step cascade inside each (district, body type) group, and
records which step produced each match so the weak ones stay visible:

  ``exact``        normalised name equality
  ``elimination``  one unmatched body left on each side of the group -- forced
  ``similarity``   greedy best-first on SequenceMatcher ratio, above a threshold
  ``override``     hand-recorded in ``crosswalk_overrides.csv``, with a reason

Elimination matters more than fuzzy matching here. Kerala's districts hold 40-80
Grama Panchayats each; once the exact pass has taken most of a group, the
survivors are often forced with no guesswork at all. Transliteration and
Malayalam spelling drift make raw similarity scores unreliable -- 'Oorakam'
against 'Uragam' scores worse than two genuinely different neighbours.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Final

from master.config import DISTRICTS, LB_TYPES, TIER_BY_PREFIX, Paths
from master.db import Database
from master.normalise import nm_en, nm_en_body, nm_ml
from master.swap import Target

#: Below this SequenceMatcher ratio a pairing is left unmatched rather than
#: guessed at. Two genuinely different neighbouring panchayats routinely score
#: in the fifties, so the floor is what stops the fuzzy pass inventing matches.
SIM_FLOOR: Final = 0.62

#: The one part of the spine that does not come from the elections build. Each
#: endpoint is a district dropdown, and the pair is (code column, name column).
SEC_REGISTRY: Final[dict[str, tuple[str, str]]] = {
    "detailed_results_urban_ajax.php": ("UrbanCd", "UrbanNameEng"),
    "detailed_results_grama_ajax.php": ("GramaCd", "GramaNameEng"),
    "detailed_results_block_ajax.php": ("BlockCd", "BlockNameEng"),
    "detailed_results_dist_ajax.php": ("DistCd", "DistNameEng"),
}

#: Every body the elections build knows about, as one SELECT. Used twice: to
#: populate the spine during a build, and to reconstruct it in memory during a
#: validate, so the two cannot drift apart.
SPINE_SELECT: Final = """
SELECT lb_code,
       (substr(district_code,2,2))::int AS district_ord,
       max(district_name)               AS district_name,
       max(lb_type)                     AS lb_type,
       max(lb_name)                     AS lb_name_en,
       max(lb_name_mal)                 AS lb_name_ml,
       min(cycle)                       AS first_cycle,
       max(cycle)                       AS last_cycle
FROM src_elections.local_bodies
GROUP BY lb_code, substr(district_code,2,2)
"""

#: One name per district ordinal, as the elections build spells it. Read on its
#: own rather than derived from the spine rows so a registry-only body can be
#: given a district name even when its district contributed no elections row.
DISTRICT_SELECT: Final = """
SELECT (substr(district_code,2,2))::int AS district_ord,
       max(district_name)               AS district_name
FROM src_elections.local_bodies
GROUP BY 1
"""

LOCAL_BODY_DDL: Final = """
CREATE TABLE {core}.local_body (
  lb_key           serial primary key,
  lb_code          text not null unique,
  district_ord     int  not null,
  district_name    text not null,
  lb_type          text not null,
  lb_name_en       text not null,
  lb_name_ml       text,
  -- Null for a body that exists but has never appeared in an election
  -- result. Mattannur Municipality (M13057) is one: the SEC's registry
  -- names it, its results feed never has, and it still has fourteen
  -- years of projects and 366 meetings to show.
  first_cycle      int,
  last_cycle       int,
  in_elections     boolean not null default true,
  sakarma_lb_id    int  unique,
  sakarma_match    text,
  sakarma_score    numeric,
  ksmart_lb_code   text,
  osm_code         text
);
"""

LB_SULEKHA_YEAR_DDL: Final = """
CREATE TABLE {core}.lb_sulekha_year (
  lb_key            int not null references {core}.local_body(lb_key),
  year_label        text not null,
  sulekha_lb_id     uuid not null,
  district_index    int not null,
  lb_index          int not null,
  method            text not null,
  score             numeric,
  primary key (year_label, sulekha_lb_id)
);
"""

Row = dict[str, Any]
Match = tuple[Row, Row, str, float]


# --------------------------------------------------------------------- inputs


def sec_registry_bodies(cache: Path) -> dict[str, tuple[int, str, str]]:
    """Every body the SEC's district dropdowns name, from the 2020 cache.

    This is the SEC's registry, not its results feed -- it lists bodies that
    returned no result, which is exactly what makes it useful here.
    """
    if not cache.exists():
        return {}
    found: dict[str, tuple[int, str, str]] = {}
    con = sqlite3.connect(cache)
    try:
        rows = con.execute("SELECT key, json FROM resp WHERE key LIKE 'detailed_results%'")
        for key, blob in rows:
            endpoint = key.split("|", 1)[0]
            if endpoint not in SEC_REGISTRY:
                continue
            code_key, name_key = SEC_REGISTRY[endpoint]
            try:
                payload = json.loads(blob)
                if isinstance(payload, str):
                    payload = json.loads(payload)
                records = payload.get("data") or []
            except Exception:
                # A cache entry that is not JSON is a failed scrape, not a body.
                continue
            for record in records:
                code = (record.get(code_key) or "").strip()
                if not code or code[0] not in TIER_BY_PREFIX:
                    continue
                found[code] = (
                    int(code[1:3]),
                    TIER_BY_PREFIX[code[0]],
                    (record.get(name_key) or "").strip(),
                )
    finally:
        con.close()
    return found


def load_overrides(path: Path) -> list[Row]:
    """Hand-recorded matches the cascade cannot reach, each with a reason.

    Mirrors the pattern ``data/reference/geo/*_overrides.csv`` already uses: a
    small reviewed file, not a threshold tweak that silently moves other rows.
    """
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def unknown_override_codes(overrides: Iterable[Row], known: Iterable[str]) -> list[str]:
    """Override rows naming an ``lb_code`` that does not exist.

    An override is a human's decision recorded once, so a typo in one is worse
    than no override at all: the cascade carries on, produces some other pairing,
    and the file looks like it is doing something. Reported, never swallowed.
    """
    codes = set(known)
    return sorted(
        {o["lb_code"] for o in overrides if o.get("lb_code") and o["lb_code"] not in codes}
    )


# ------------------------------------------------------------------- matching


def district_ordinal(value: Any) -> str:
    """A district ordinal reduced to one spelling, so ``'02'`` and ``2`` compare equal."""
    try:
        return str(int(str(value).strip()))
    except (TypeError, ValueError):
        return ""


def _override_applies(lrow: Row, override: Row, target: Row | None, namefield: str) -> bool:
    """Whether one override row describes this body -- district, tier and name.

    All three, not two. Kerala repeats body names across districts often enough
    that tier and name alone are not a key: there is a Perur Grama Panchayat in
    Palakkad and another in Ernakulam, and an override written for one of them
    must not fire on the other. The district comes from the body the override
    names, which is the one place it is recorded rather than asserted.

    ``target`` is ``None`` when the named ``lb_code`` is not on this side of the
    build. The row still matches on tier and name so the caller can report the
    code as unreachable; an override that quietly does nothing is the failure
    this file exists to prevent.
    """
    if lrow["lb_type"] != override["lb_type"]:
        return False
    if target is not None and district_ordinal(lrow.get("dist_ord")) != district_ordinal(
        target.get("dist_ord")
    ):
        return False
    name = lrow.get(namefield)
    return nm_ml(name) == nm_ml(override["source_name"]) or nm_en(name) == nm_en(
        override["source_name"]
    )


def apply_overrides(
    left: Sequence[Row],
    right: Sequence[Row],
    overrides: Sequence[Row],
    source: str,
    namefield: str,
) -> tuple[list[Match], list[Row], list[str]]:
    """Pull overridden rows out of ``left`` and pair them directly.

    Returns the forced matches, the rows still needing the cascade, and any
    override that named a body this side of the build cannot see.
    """
    by_code = {r["lb_code"]: r for r in right}
    wanted = [o for o in overrides if o["source"] == source]
    forced: list[Match] = []
    rest: list[Row] = []
    missing: list[str] = []
    for lrow in left:
        hit = next(
            (o for o in wanted if _override_applies(lrow, o, by_code.get(o["lb_code"]), namefield)),
            None,
        )
        if hit and hit["lb_code"] in by_code:
            forced.append((lrow, by_code[hit["lb_code"]], "override", 1.0))
        else:
            if hit:
                missing.append(hit["lb_code"])
            rest.append(lrow)
    return forced, rest, sorted(set(missing))


def override_district_mismatches(overrides: Iterable[Row], spine: Iterable[Row]) -> list[str]:
    """Overrides whose stated district is not the district of the body they name.

    The district column is what discriminates two same-named bodies, so a wrong
    one is not a cosmetic error: it means the row was written against a body the
    author was not looking at. Reported rather than corrected here, because only
    a human knows which half of the row is the typo.
    """
    by_code = {r["lb_code"]: r.get("district_name") or "" for r in spine}
    mismatched = []
    for o in overrides:
        actual = by_code.get(o.get("lb_code", ""))
        stated = o.get("district_name") or ""
        if actual and nm_en(stated) != nm_en(actual):
            mismatched.append(
                f"override for {o['lb_code']} says district {stated!r}, "
                f"but {o['lb_code']} is in {actual!r}"
            )
    return sorted(set(mismatched))


def cascade(
    left: Sequence[Row], right: Sequence[Row], lname: str, rname: str
) -> tuple[list[Match], list[Row]]:
    """Match ``left`` onto ``right`` within (district, type) groups.

    Returns (matches as (left, right, method, score), unmatched left rows).
    """
    lg: dict[tuple[Any, Any], list[Row]] = defaultdict(list)
    rg: dict[tuple[Any, Any], list[Row]] = defaultdict(list)
    for r in left:
        lg[(r["dist_ord"], r["lb_type"])].append(r)
    for r in right:
        rg[(r["dist_ord"], r["lb_type"])].append(r)

    matches: list[Match] = []
    unmatched: list[Row] = []

    for key, lrows in lg.items():
        rrows = list(rg.get(key, []))
        taken_r: set[int] = set()
        open_l: list[Row] = []

        by_name: dict[str, list[int]] = defaultdict(list)
        for i, rrow in enumerate(rrows):
            if rrow[rname]:
                by_name[rrow[rname]].append(i)

        for lrow in lrows:
            # A name that hits two candidates is ambiguous, not exact: fall
            # through and let elimination or similarity decide.
            cands = [i for i in by_name.get(lrow[lname], []) if i not in taken_r]
            if len(cands) == 1:
                taken_r.add(cands[0])
                matches.append((lrow, rrows[cands[0]], "exact", 1.0))
            else:
                open_l.append(lrow)

        free_r = [i for i in range(len(rrows)) if i not in taken_r]

        if len(open_l) == 1 and len(free_r) == 1:
            matches.append((open_l[0], rrows[free_r[0]], "elimination", 1.0))
            open_l, free_r = [], []

        if open_l and free_r:
            pairs: list[tuple[float, Row, int]] = []
            for lrow in open_l:
                for i in free_r:
                    if not lrow[lname] or not rrows[i][rname]:
                        continue
                    pairs.append(
                        (SequenceMatcher(None, lrow[lname], rrows[i][rname]).ratio(), lrow, i)
                    )
            pairs.sort(key=lambda p: -p[0])
            used_l: set[int] = set()
            used_r: set[int] = set()
            for score, lrow, i in pairs:
                if score < SIM_FLOOR or id(lrow) in used_l or i in used_r:
                    continue
                used_l.add(id(lrow))
                used_r.add(i)
                matches.append((lrow, rrows[i], "similarity", round(score, 4)))
            rest_l = [lrow for lrow in open_l if id(lrow) not in used_l]
            rest_r = [i for i in free_r if i not in used_r]
            # Elimination again: the fuzzy pass can leave exactly one survivor
            # on each side, and that pair is forced for the same reason as above.
            if len(rest_l) == 1 and len(rest_r) == 1:
                matches.append((rest_l[0], rrows[rest_r[0]], "elimination", 1.0))
            else:
                unmatched.extend(rest_l)
        else:
            unmatched.extend(open_l)

    return matches, unmatched


def sakarma_pool(spine: Sequence[Row]) -> list[Row]:
    """The bodies Sakarma could plausibly be talking about.

    Sakarma's meetings begin in 2016, so a body whose last election cycle was
    2010 cannot have any. Without this guard, elimination forces a wrong pair
    whenever a group has a body on one side that the other side lacks --
    Sakarma's Mattannur Municipality was landing on the defunct Kannur
    Municipality (M13052, last contested 2010) purely to balance the group.
    A body with no election history at all (``last_cycle`` null) stays eligible:
    it is exactly the case this guard must not exclude.
    """
    return [r for r in spine if not r["last_cycle"] or int(r["last_cycle"]) >= 2015]


# --------------------------------------------------------------------- result


@dataclass(frozen=True, slots=True)
class CrosswalkResult:
    """What one crosswalk run produced, and everything it could not resolve.

    Whether that is good enough to write is not decided here: ``master.validate``
    turns this into gates, coverage and a report. Keeping the judgement out of
    this module is what stops two definitions of "resolved" drifting apart.
    """

    bodies: int
    registry_only: int
    sakarma_total: int
    sakarma_matches: tuple[Match, ...]
    sakarma_unmatched: tuple[Row, ...]
    sulekha_total: int
    sulekha_rows: tuple[list[Any], ...]
    sulekha_unmatched: tuple[Row, ...]
    per_year: tuple[tuple[str, int, int], ...]
    problems: tuple[str, ...] = ()
    methods: Counter[str] = field(default_factory=Counter)
    year_methods: Counter[str] = field(default_factory=Counter)
    #: The spine this run resolved against, carried so the gate can ask its own
    #: questions of it -- duplicate codes, missing districts -- without a second
    #: read that could disagree with the one the matching used.
    spine: tuple[Row, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        return {
            "bodies": self.bodies,
            "registry-only bodies": self.registry_only,
            "sakarma bodies matched": len(self.sakarma_matches),
            "sakarma bodies total": self.sakarma_total,
            "sulekha year-rows matched": len(self.sulekha_rows),
            "sulekha year-rows total": self.sulekha_total,
        }


# ----------------------------------------------------------------- the resolve


def _prepare_spine(rows: Iterable[Row]) -> list[Row]:
    """Add the comparison keys the cascade groups and matches on."""
    prepared = []
    for r in rows:
        # A validate has no surrogate key to offer: the table does not exist yet.
        r.setdefault("lb_key", None)
        r["dist_ord"] = str(int(r["district_ord"]))
        r["k_ml"] = nm_ml(r.get("lb_name_ml"))
        r["k_en"] = nm_en(r.get("lb_name_en"))
        prepared.append(r)
    return prepared


def is_in_elections(row: Row) -> bool:
    """Whether the SEC ever published a result for this body.

    Both real callers state it outright -- ``plan_spine`` because it knows which
    rows came from the results feed and which from the registry, ``read_spine``
    because it selects the column. It is inferred from the cycles only for a row
    built by hand, in a test.

    That distinction matters because ``db.query`` returns every value as a
    string, so a NULL ``first_cycle`` and an empty one read identically: a body
    with no result and a body whose cycles simply failed to load would look the
    same to anything that guesses. The flag is the fact; the cycles are evidence.
    """
    raw = row.get("in_elections")
    if isinstance(raw, bool):
        return raw
    if raw in (None, ""):
        return bool(row.get("first_cycle") or row.get("last_cycle"))
    return str(raw).strip().lower() in {"t", "true", "1", "yes"}


def resolve(
    spine: Sequence[Row],
    sakarma: Sequence[Row],
    sulekha: Sequence[Row],
    overrides: Sequence[Row],
    *,
    registry_only: int = 0,
) -> CrosswalkResult:
    """Run both cascades. Pure: no database, no files, no output.

    Keeping this side-effect free is what lets ``validate`` gate a build without
    creating a single table, and what lets the awkward cases be tested on six
    rows instead of a five-gigabyte restore.
    """
    problems = unknown_override_codes(overrides, (r["lb_code"] for r in spine))
    reported = [f"override names unknown lb_code {code}" for code in problems]
    reported += override_district_mismatches(overrides, spine)

    # ---- sakarma ----------------------------------------------------------
    pool = sakarma_pool(spine)
    sk_forced, sk_rest, sk_missing = apply_overrides(sakarma, pool, overrides, "sakarma", "name_ml")
    sk_matches, sk_unmatched = cascade(sk_rest, pool, "k_ml", "k_ml")
    sk_matches += sk_forced
    reported += [
        f"sakarma override names lb_code {code}, which is outside the sakarma candidate pool"
        for code in sk_missing
        if code not in problems
    ]

    # ---- sulekha, per year ------------------------------------------------
    rows_out: list[list[Any]] = []
    su_unmatched: list[Row] = []
    per_year: list[tuple[str, int, int]] = []
    year_methods: Counter[str] = Counter()

    by_year: dict[str, list[Row]] = defaultdict(list)
    for r in sulekha:
        if r["dist_ord"] is not None:
            by_year[r["year_label"]].append(r)

    for year, rows in sorted(by_year.items()):
        forced, rest, su_missing = apply_overrides(rows, spine, overrides, "sulekha", "k_en")
        matched, unmatched = cascade(rest, spine, "k_en", "k_en")
        matched += forced
        rows_out += [
            [rr["lb_key"], year, lr["sulekha_lb_id"], lr["district_index"], lr["lb_index"], m, s]
            for lr, rr, m, s in matched
        ]
        year_methods.update(meth for _, _, meth, _ in matched)
        su_unmatched += unmatched
        per_year.append((year, len(matched), len(unmatched)))
        reported += [
            f"{year}: sulekha override names unknown lb_code {code}"
            for code in su_missing
            if code not in problems
        ]

    return CrosswalkResult(
        bodies=len(spine),
        registry_only=registry_only,
        sakarma_total=len(sakarma),
        sakarma_matches=tuple(sk_matches),
        sakarma_unmatched=tuple(sk_unmatched),
        sulekha_total=sum(len(rows) for rows in by_year.values()),
        sulekha_rows=tuple(rows_out),
        sulekha_unmatched=tuple(su_unmatched),
        per_year=tuple(per_year),
        problems=tuple(dict.fromkeys(reported)),
        methods=Counter(m for _, _, m, _ in sk_matches),
        year_methods=year_methods,
        spine=tuple(spine),
    )


# ------------------------------------------------------------------ the build


def read_sakarma(db: Database) -> list[Row]:
    rows = db.query("SELECT id, district_id::text AS dist_ord, lb_type_id, name_ml FROM sakarma.lb")
    for r in rows:
        r["lb_type"] = LB_TYPES[int(r["lb_type_id"])]
        r["k_ml"] = nm_ml(r["name_ml"])
    return rows


def read_sulekha(db: Database, spine: Sequence[Row]) -> tuple[list[Row], list[str]]:
    """Sulekha's per-year local body list, keyed to our district ordinals.

    Sulekha names its districts and never numbers them, so the join back to the
    spine goes through the district name. A name that fails to resolve takes
    every body in that district with it, so it is returned rather than printed.
    """
    rows = db.query(
        """
        SELECT lb.id::text AS sulekha_lb_id, d.year_label, d.district_index,
               lb.lb_index, d.district_name, d.lb_type_label, lb.lb_name
        FROM src_sulekha.local_bodies lb JOIN src_sulekha.districts d ON d.id = lb.district_id
        """
    )
    dist_ord_by_name = {nm_en(r["district_name"]): r["dist_ord"] for r in spine}
    for r in rows:
        r["lb_type"] = r["lb_type_label"]
        r["dist_ord"] = dist_ord_by_name.get(nm_en(r["district_name"]))
        r["k_en"] = nm_en_body(r["lb_name"])
    unresolved = sorted({r["district_name"] for r in rows if r["dist_ord"] is None})
    return rows, unresolved


def read_spine(db: Database, target: Target = Target()) -> list[Row]:
    """The spine as it exists in ``core.local_body``, with its surrogate keys."""
    return _prepare_spine(
        db.query(
            "SELECT lb_key, lb_code, district_ord, district_name, lb_type, lb_name_en, "
            f"lb_name_ml, first_cycle, last_cycle, in_elections FROM {target.core}.local_body"
        )
    )


def district_names(db: Database) -> dict[int, str]:
    """One name per district ordinal, as the elections build spells it."""
    return {
        int(r["district_ord"]): r["district_name"]
        for r in db.query(DISTRICT_SELECT)
        if r["district_name"]
    }


def district_name_for(dist_ord: int, elections: Mapping[int, str]) -> str:
    """The district a registry-only body belongs to, never an empty answer.

    The elections build is preferred wherever it has a row, so every district is
    spelled one way in ``core.local_body``. ``DISTRICTS`` answers the case it
    cannot: a district whose only body in the SEC's registry returned no result
    contributes no elections row to copy a name from. The insert used to fill
    the column with ``max(district_name) … WHERE district_ord = N``, which is
    NULL exactly then -- and the column is NOT NULL, so the build would fail on
    the one body it was added to rescue.
    """
    name = elections.get(dist_ord) or DISTRICTS.get(dist_ord)
    if not name:
        raise ValueError(
            f"lb_code carries district ordinal {dist_ord}, which is not one of "
            f"Kerala's fourteen districts {sorted(DISTRICTS)}"
        )
    return name


def plan_spine(db: Database, paths: Paths) -> tuple[list[Row], int]:
    """The spine as it *would* be, read straight from the sources.

    Used by ``validate``, which must gate the crosswalk without creating a
    table. Same SELECT and same registry pass as the build, so a validate that
    passes and a build that fails would be a bug in one of two callers, not in
    two divergent definitions of what a local body is.
    """
    rows = db.query(SPINE_SELECT)
    known = {r["lb_code"] for r in rows}
    elections_districts = {
        int(r["district_ord"]): r["district_name"] for r in rows if r["district_name"]
    }
    # Every row of SPINE_SELECT is a published result, by definition of the table
    # it comes from. Stating it here rather than inferring it later is what lets
    # the report read a flag instead of guessing from an empty cycle.
    for r in rows:
        r["in_elections"] = True
    extra = 0
    registry = sorted(sec_registry_bodies(paths.sec_registry_cache).items())
    for code, (dist_ord, tier, name) in registry:
        if code in known:
            continue
        rows.append(
            {
                "lb_key": None,
                "lb_code": code,
                "district_ord": str(dist_ord),
                "district_name": district_name_for(dist_ord, elections_districts),
                "lb_type": tier,
                "lb_name_en": name,
                "lb_name_ml": "",
                "first_cycle": "",
                "last_cycle": "",
                "in_elections": False,
            }
        )
        extra += 1
    return _prepare_spine(rows), extra


def write_spine(db: Database, paths: Paths, target: Target = Target()) -> int:
    """Create ``core.local_body`` in ``target`` and fill it from the elections build.

    Faithful copy of a source, not a judgement: every row is one ``lb_code`` the
    elections build already emitted, or one body the SEC's registry names. It
    still writes into the staging schema rather than over the live one, because
    a gate that fails after this point used to leave the site with a spine and
    no crosswalk -- correct rows, joined to nothing.
    """
    db.execute(f"CREATE SCHEMA IF NOT EXISTS {target.core};")
    db.execute(
        f"DROP TABLE IF EXISTS {target.core}.lb_sulekha_year, {target.core}.local_body CASCADE;"
    )
    db.execute(LOCAL_BODY_DDL.format(core=target.core))
    db.execute(
        f"INSERT INTO {target.core}.local_body "
        "(lb_code, district_ord, district_name, lb_type, lb_name_en, lb_name_ml, "
        " first_cycle, last_cycle)" + SPINE_SELECT
    )

    # The elections build only knows bodies that returned a result. The SEC's
    # own registry endpoint lists every body it recognises, result or not, so
    # anything there but not in the spine is a real body with no election data
    # -- add it rather than lose its finances and meetings.
    #
    # The district name is resolved in Python rather than by a subquery over the
    # rows just inserted. That subquery returned NULL for a district whose only
    # registry body returned no result, and district_name is NOT NULL: the one
    # case this insert exists for was also the one case that could break it.
    added = 0
    elections_districts = district_names(db)
    registry = sorted(sec_registry_bodies(paths.sec_registry_cache).items())
    for code, (dist_ord, tier, name) in registry:
        added += (
            db.scalar(
                f"""
                INSERT INTO {target.core}.local_body
                  (lb_code, district_ord, district_name, lb_type, lb_name_en, in_elections)
                SELECT %s, %s, %s, %s, %s, false
                WHERE NOT EXISTS (SELECT 1 FROM {target.core}.local_body WHERE lb_code = %s)
                RETURNING 1
                """,
                [
                    code,
                    dist_ord,
                    district_name_for(dist_ord, elections_districts),
                    tier,
                    name,
                    code,
                ],
            )
            or 0
        )
    return added


def link_geometry(db: Database, target: Target = Target()) -> None:
    """Carry the geometry crosswalk's codes onto the spine.

    Overrides are applied after the automatic crosswalk, so a hand-recorded
    pairing wins -- the same precedence ``geo.build.crosswalk`` uses.
    """
    db.execute(
        f"""
        UPDATE {target.core}.local_body b SET ksmart_lb_code = x.ksmart_lb_code
        FROM src_geo.ksmart_lb_crosswalk x WHERE x.lb_code = b.lb_code;
        UPDATE {target.core}.local_body b SET ksmart_lb_code = o.ksmart_lb_code
        FROM src_geo.ksmart_lb_overrides o WHERE o.lb_code = b.lb_code;
        UPDATE {target.core}.local_body b SET osm_code = o.osm_code
        FROM src_geo.osm_lb_overrides o WHERE o.lb_code = b.lb_code;
        """
    )


def write_matches(db: Database, result: CrosswalkResult, target: Target = Target()) -> None:
    """Write both crosswalk tables. Only reached once the gate has passed.

    The Sakarma side arrives as a list of pairs and is applied as one UPDATE, so
    it needs somewhere to put the pairs first. That staging table is a TEMP
    table rather than ``core._sk_map``: it exists for the length of one
    statement, and the version that lived in ``core`` was never dropped, so
    every built database carried a copy of the match list under a name beginning
    with an underscore that nothing read.
    """
    db.execute("DROP TABLE IF EXISTS _sk_map;")
    db.execute(
        "CREATE TEMP TABLE _sk_map (sakarma_lb_id int, lb_key int, method text, score numeric);"
    )
    db.copy_rows(
        "_sk_map",
        ["sakarma_lb_id", "lb_key", "method", "score"],
        [[int(sk["id"]), int(lb["lb_key"]), m, s] for sk, lb, m, s in result.sakarma_matches],
    )
    db.execute(
        f"""
        UPDATE {target.core}.local_body b
           SET sakarma_lb_id = m.sakarma_lb_id, sakarma_match = m.method, sakarma_score = m.score
        FROM _sk_map m WHERE m.lb_key = b.lb_key;
        """
    )
    db.execute("DROP TABLE _sk_map;")

    db.execute(LB_SULEKHA_YEAR_DDL.format(core=target.core))
    db.copy_rows(
        f"{target.core}.lb_sulekha_year",
        ["lb_key", "year_label", "sulekha_lb_id", "district_index", "lb_index", "method", "score"],
        [
            [int(lb_key), year, sid, int(di), int(li), meth, score]
            for lb_key, year, sid, di, li, meth, score in result.sulekha_rows
        ],
    )


def plan(db: Database, paths: Paths) -> CrosswalkResult:
    """Resolve the crosswalk against the live sources, writing nothing.

    What ``master validate`` runs. The result is handed to ``master.validate``,
    which decides whether it would have been fit to write.
    """
    spine, registry_only = plan_spine(db, paths)
    sakarma = read_sakarma(db)
    sulekha, unresolved_districts = read_sulekha(db, spine)
    result = resolve(
        spine,
        sakarma,
        sulekha,
        load_overrides(paths.overrides),
        registry_only=registry_only,
    )
    return _with_district_problems(result, unresolved_districts)


def prepare(db: Database, paths: Paths, target: Target = Target()) -> CrosswalkResult:
    """Create the spine in ``target`` and resolve the crosswalk against it.

    Writes no match: the judgements -- which Sakarma body is which, which
    Sulekha year-row is which -- are held in memory until the caller has gated
    them and called ``write_matches``. The spine itself is written, because the
    surrogate keys the matches carry have to come from somewhere, but into the
    staging schema, so a failed gate leaves the live spine exactly as it was.
    """
    registry_only = write_spine(db, paths, target)
    link_geometry(db, target)

    spine = read_spine(db, target)
    sakarma = read_sakarma(db)
    sulekha, unresolved_districts = read_sulekha(db, spine)
    result = _with_district_problems(
        resolve(
            spine,
            sakarma,
            sulekha,
            load_overrides(paths.overrides),
            registry_only=registry_only,
        ),
        unresolved_districts,
    )
    return result


def _with_district_problems(result: CrosswalkResult, unresolved: Sequence[str]) -> CrosswalkResult:
    """A district that fails to resolve disqualifies every body inside it at once."""
    if not unresolved:
        return result
    return replace(
        result,
        problems=result.problems + (f"sulekha district names not resolved: {sorted(unresolved)}",),
    )
