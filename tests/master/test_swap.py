"""What a failed build leaves behind, and what a finished one records.

The claim being tested is the one the runbook makes: a build that fails writes
nothing. That used to be false for the spine -- ``core.local_body`` was dropped
and rewritten before the gate ran -- and the failure mode was not a build that
had to be re-run but a public site whose every join from a meeting or a project
to a body had disappeared.

An assertion in a docstring is not proof of that, so most of this file runs a
real build against a real Postgres: a miniature master database of four bodies,
built once so it succeeds, then broken and built again so it fails, with the
first build's tables compared byte for byte across the failure.

The scratch database is created and dropped by the fixture. Nothing here goes
anywhere near the built master database, which takes 25 minutes to make.
"""

from __future__ import annotations

import os

import pytest
from click.testing import CliRunner

from master.swap import DERIVED, REPLACED_SUFFIX, STAGING_SUFFIX, Target

# --- the rewrite, which needs no database -----------------------------------


def test_a_live_target_rewrites_nothing():
    """``Target()`` is what a reader gets, and it must not be a special case."""
    sql = "SELECT * FROM core.local_body JOIN finance.project USING (lb_key)"
    assert Target().sql(sql) == sql


def test_every_derived_schema_is_retargeted():
    live = "core.local_body finance.project meetings.meeting elections.candidate"
    assert Target("_build").sql(live) == (
        "core_build.local_body finance_build.project "
        "meetings_build.meeting elections_build.candidate"
    )


def test_the_source_schemas_keep_their_names():
    """``src_elections`` ends in ``elections``. Rewriting it would point the build
    at a schema that does not exist, which is the whole risk of doing this by
    text: the word boundary is what stops it."""
    sql = "FROM src_elections.local_bodies l JOIN core.local_body b ON b.lb_code = l.lb_code"
    assert Target("_build").sql(sql) == (
        "FROM src_elections.local_bodies l JOIN core_build.local_body b ON b.lb_code = l.lb_code"
    )


def test_sakarma_and_src_sulekha_are_left_alone():
    sql = "FROM sakarma.meeting_manifest m, src_sulekha.projects p, src_geo.layer"
    assert Target("_build").sql(sql) == sql


def test_the_financial_year_function_is_retargeted_with_its_schema():
    """``meetings.meeting`` calls it while it is still being built, so it has to
    resolve inside the staged schema rather than against the live one."""
    assert "core_build.fin_year(" in Target("_build").sql("core.fin_year(m.meeting_date)")


def test_the_real_schema_file_retargets_every_qualifier():
    """No ``core.``/``finance.``/``meetings.``/``elections.`` may survive the rewrite,
    or one table lands in the live schema in the middle of a build."""
    import re

    from master.cli import SCHEMA_SQL

    rewritten = Target("_build").sql(SCHEMA_SQL.read_text(encoding="utf-8"))
    assert not re.search(r"\b(core|finance|meetings|elections)\.", rewritten)
    assert "src_sulekha.projects" in rewritten
    assert "sakarma.meeting_manifest" in rewritten


def test_the_live_schemas_cannot_be_dropped_by_a_mistyped_target():
    """``discard`` drops schemas by name. A target with no suffix names the live
    ones, so it is refused rather than obeyed."""
    from master.swap import discard, swap

    with pytest.raises(ValueError):
        discard(None, Target())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        swap(None, Target())  # type: ignore[arg-type]


# --- a real build, on a scratch database ------------------------------------

SERVER = os.environ.get(
    "MASTER_DATABASE_URL", "postgresql://sambandh:sambandh@localhost:55432/sambandh"
)
SCRATCH = "master_swap_test"

#: Four bodies, two of them in the same district, one Sakarma body per matching
#: name, one Sulekha year-row each, and a handful of projects and meetings. Just
#: enough for every gate to have something to count and for every table in
#: schema.sql to be non-empty.
FIXTURE = """
CREATE SCHEMA src_elections;
CREATE TABLE src_elections.local_bodies (
  lb_code text, district_code text, district_name text, lb_type text,
  lb_name text, lb_name_mal text, cycle int
);
INSERT INTO src_elections.local_bodies VALUES
  ('G02001','D02','KOLLAM','Grama Panchayat','Kottarakkara','കൊട്ടാരക്കര',2015),
  ('G02001','D02','KOLLAM','Grama Panchayat','Kottarakkara','കൊട്ടാരക്കര',2020),
  ('G02002','D02','KOLLAM','Grama Panchayat','Chavara','ചവറ',2020),
  ('G02003','D02','KOLLAM','Grama Panchayat','Perinad','പെരിനാട്',2020);

CREATE TABLE src_elections.candidates (lb_code text, candidate_name text, cycle int);
INSERT INTO src_elections.candidates VALUES ('G02001','A',2020), ('G02002','B',2020);
CREATE TABLE src_elections.wards (lb_code text, ward_no text, cycle int);
INSERT INTO src_elections.wards VALUES ('G02001','1',2020);

CREATE SCHEMA sakarma;
CREATE TABLE sakarma.lb (id int, district_id int, lb_type_id int, name_ml text);
INSERT INTO sakarma.lb VALUES (1,2,5,'കൊട്ടാരക്കര'), (2,2,5,'ചവറ'), (3,2,5,'പെരിനാട്');
CREATE TABLE sakarma.meeting_manifest (
  id int, lb_id int, meeting_date date, meeting_no_label text,
  meeting_type text, meeting_nature text, meeting_venue text, category text
);
INSERT INTO sakarma.meeting_manifest VALUES
  (1,1,'2023-05-04','1/2023','ഭരണസമിതി യോഗം','സാധാരണ യോഗം','Office','GB'),
  (2,1,'2024-02-11','2/2024','ധനകാര്യ സ്റ്റാൻഡിംഗ് കമ്മിറ്റി','അടിയന്തര യോഗം','Office','SC'),
  (3,2,'2023-09-19','3/2023','ഭരണസമിതി യോഗം','സാധാരണ യോഗം',NULL,'GB');
CREATE TABLE sakarma.meeting_artifact (
  id int, meeting_manifest_id int, artifact_type text, decision_index int,
  original_filename text, gcs_path text, byte_size int
);
INSERT INTO sakarma.meeting_artifact VALUES (1,1,'minutes',NULL,'m.pdf','gs://x/m.pdf',10);

CREATE SCHEMA src_sulekha;
CREATE TABLE src_sulekha.districts (
  id int, year_label text, district_index int, district_name text, lb_type_label text
);
INSERT INTO src_sulekha.districts VALUES (1,'2023-2024',2,'Kollam','Grama Panchayat');
CREATE TABLE src_sulekha.local_bodies (id uuid, district_id int, lb_index int, lb_name text);
INSERT INTO src_sulekha.local_bodies VALUES
  ('11111111-1111-1111-1111-111111111111',1,1,'Kottarakkara Grama Panchayat'),
  ('22222222-2222-2222-2222-222222222222',1,2,'Chavara Grama Panchayat'),
  ('33333333-3333-3333-3333-333333333333',1,3,'Perinad Grama Panchayat');
CREATE TABLE src_sulekha.projects (
  id int, local_body_id uuid, project_no text, project_name text,
  formulation text, expense text
);
INSERT INTO src_sulekha.projects VALUES
  (1,'11111111-1111-1111-1111-111111111111','1','ദൈനംദിന ചെലവുകൾ','100000','50000'),
  (2,'11111111-1111-1111-1111-111111111111','2','റോഡ്','200000','200000'),
  (3,'22222222-2222-2222-2222-222222222222','1','ദൈനംദിന ചെലവുകൾ','50000','0');
CREATE TABLE src_sulekha.pdfs (id int, project_id int, gcs_path text);
INSERT INTO src_sulekha.pdfs VALUES (1,1,'gs://x/1.pdf');

CREATE SCHEMA src_geo;
CREATE TABLE src_geo.ksmart_lb_crosswalk (lb_code text, ksmart_lb_code text);
INSERT INTO src_geo.ksmart_lb_crosswalk VALUES ('G02001','K1');
CREATE TABLE src_geo.ksmart_lb_overrides (lb_code text, ksmart_lb_code text);
CREATE TABLE src_geo.osm_lb_overrides (lb_code text, osm_code text);
"""

#: A Sakarma body no spine body can be, in a district that holds no other
#: unmatched pair, so elimination cannot rescue it. One failed gate, nothing else
#: different about the run.
BREAK_THE_GATE = "INSERT INTO sakarma.lb VALUES (9, 9, 5, 'വേങ്ങര');"


@pytest.fixture(scope="module")
def scratch():
    """A throwaway database holding a miniature version of the real sources."""
    psycopg = pytest.importorskip("psycopg")
    try:
        admin = psycopg.connect(SERVER, connect_timeout=3, autocommit=True)
    except Exception as exc:  # pragma: no cover - depends on the machine
        pytest.skip(f"no Postgres at {SERVER}: {exc}")
    url = SERVER.rsplit("/", 1)[0] + "/" + SCRATCH
    with admin:
        admin.execute(f"DROP DATABASE IF EXISTS {SCRATCH} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {SCRATCH}")
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(FIXTURE)
        yield url
    finally:
        with psycopg.connect(SERVER, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {SCRATCH} WITH (FORCE)")


def run_build(url, root):
    """``master --root … --database-url … build --skip-load``, as a user would."""
    from master.cli import main

    return CliRunner().invoke(
        main, ["--root", str(root), "--database-url", url, "build", "--skip-load"]
    )


def snapshot(conn):
    """Every derived table, its row count and a digest of its contents.

    A count alone would not notice a spine rewritten with different surrogate
    keys, which is exactly what the old build did to a database whose gate then
    failed.
    """
    tables = conn.execute(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_schema = ANY(%s) ORDER BY 1, 2",
        [list(DERIVED)],
    ).fetchall()
    out = {}
    for schema, table in tables:
        digest = conn.execute(
            f"SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) "
            f"FROM {schema}.{table} t"
        ).fetchone()
        out[f"{schema}.{table}"] = digest
    return out


@pytest.fixture(scope="module")
def built(scratch, tmp_path_factory):
    """One successful build, and the state of the database straight afterwards."""
    import psycopg

    result = run_build(scratch, tmp_path_factory.mktemp("root"))
    assert result.exit_code == 0, result.output
    with psycopg.connect(scratch, autocommit=True) as conn:
        return scratch, result, snapshot(conn)


@pytest.mark.integration
def test_a_build_leaves_the_derived_schemas_populated(built):
    _, result, state = built
    assert state["core.local_body"][0] == 3
    assert state["finance.project"][0] == 3
    assert state["meetings.meeting"][0] == 3
    assert state["elections.candidate"][0] == 2
    assert "gates passed" in result.output


@pytest.mark.integration
def test_no_staging_schema_survives_a_finished_build(built):
    """Otherwise every build leaves several gigabytes of the last one behind."""
    import psycopg

    url, _, _ = built
    with psycopg.connect(url, autocommit=True) as conn:
        left = conn.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname LIKE %s OR nspname LIKE %s",
            [f"%{STAGING_SUFFIX}", f"%{REPLACED_SUFFIX}"],
        ).fetchall()
    assert left == []


@pytest.mark.integration
def test_the_match_table_is_not_left_in_core(built):
    """``core._sk_map`` staged one UPDATE and was never dropped, so every built
    database carried a copy of the match list that nothing read."""
    import psycopg

    url, _, _ = built
    with psycopg.connect(url, autocommit=True) as conn:
        assert conn.execute("SELECT to_regclass('core._sk_map')").fetchone()[0] is None


@pytest.mark.integration
def test_coverage_carries_the_election_flag(built):
    """Both /api/bodies and the Mattannur case need it, and it was only on
    ``core.local_body``, so every body lookup had to join for it."""
    import psycopg

    url, _, _ = built
    with psycopg.connect(url, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT lb_code, in_elections, has_meetings, has_geometry "
            "FROM core.lb_coverage ORDER BY lb_code"
        ).fetchall()
    assert rows == [
        ("G02001", True, True, True),
        ("G02002", True, True, False),
        ("G02003", True, True, False),
    ]


@pytest.mark.integration
def test_the_build_records_its_own_provenance(built):
    """R9: the API reads the date the data has, rather than being told one."""
    import psycopg

    from master.config import DATASET_NAME, SOURCE_DUMPS

    url, _, _ = built
    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute(
            "SELECT dataset, built_at, source_dumps, bodies, projects, meetings, candidates "
            "FROM core.build_manifest"
        ).fetchall()
    assert len(row) == 1
    dataset, built_at, dumps, bodies, projects, meetings, candidates = row[0]
    assert dataset == DATASET_NAME
    assert built_at is not None
    assert dumps == list(SOURCE_DUMPS)
    assert (bodies, projects, meetings, candidates) == (3, 3, 3, 2)


@pytest.mark.integration
def test_a_failed_gate_leaves_every_pre_existing_table_untouched(built, tmp_path):
    """The claim the runbook makes, run rather than asserted.

    A second build over sources that no longer resolve. Every derived table --
    contents, not just counts -- must be exactly what the first build left, and
    the recorded build date must still be the date of the data that is there.
    """
    import psycopg

    url, _, before = built
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(BREAK_THE_GATE)
        stamp_before = conn.execute("SELECT built_at FROM core.build_manifest").fetchone()[0]

    failed = run_build(url, tmp_path)

    assert failed.exit_code == 1
    assert "FAIL" in failed.output
    assert "വേങ്ങര" in failed.output

    with psycopg.connect(url, autocommit=True) as conn:
        after = snapshot(conn)
        stamp_after = conn.execute("SELECT built_at FROM core.build_manifest").fetchone()[0]
        staged = conn.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname LIKE %s", [f"%{STAGING_SUFFIX}"]
        ).fetchall()

    assert after == before
    assert stamp_after == stamp_before
    assert staged == [], "a failed build must not leave its scratch schemas behind"


@pytest.mark.integration
def test_a_failed_build_writes_no_report(built, tmp_path):
    """The report describes a database. A failed build made none, so there is
    nothing for it to describe."""
    url, _, _ = built
    run_build(url, tmp_path)
    assert not (tmp_path / "data" / "final" / "master").exists()


@pytest.mark.integration
def test_the_database_still_answers_after_the_failed_build(built, tmp_path):
    """The point of all of it: the site is still serving the previous build."""
    import psycopg

    url, _, _ = built
    with psycopg.connect(url, autocommit=True) as conn:
        answer = conn.execute(
            "SELECT c.lb_name_en, s.projects, m.meetings "
            "FROM core.lb_coverage c "
            "JOIN finance.lb_year_summary s ON s.lb_key = c.lb_key "
            "JOIN meetings.lb_year_summary m ON m.lb_key = c.lb_key "
            "WHERE c.lb_code = 'G02001' AND s.year_label = '2023-2024' "
            "  AND m.year_label = '2023-2024'"
        ).fetchone()
    # Two projects, and two meetings: 4 May 2023 and 11 February 2024 are the
    # same financial year, which is the rule core.fin_year exists to hold.
    assert answer == ("Kottarakkara", 2, 2)
