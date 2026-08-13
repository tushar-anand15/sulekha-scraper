"""The gate, and the difference between a failure and a limitation.

Two properties are asserted throughout. First, that a gate fails on exactly the
thing it names -- an unresolved body, an orphaned meeting, a repeated
``lb_code`` -- and names the offender, because a gate that says only "failed"
sends a reader back to the five-gigabyte database to find out what.

Second, that a limitation of the *source* never fails the build. Mattannur
Municipality has no election result because the SEC never published one; 205
bodies have no boundary because KSMART publishes none. Those numbers belong in
the report, which is where the site reads them from, and treating them as
failures would leave the build permanently red over something no code can fix.

The gate is pure by construction, so all of this is stated in a handful of rows.
The one test that needs the built database is marked ``integration`` and skips
when it is not there.
"""

from __future__ import annotations

import json
import os

import pytest

from master.config import resolve_paths
from master.crosswalk import resolve
from master.validate import Quality, Source, assess, read_source, write
from tests.master.test_crosswalk import GP, sakarma, spine, sulekha

# --- fixtures ---------------------------------------------------------------


def resolved(**kwargs) -> Quality:
    """One body, resolved on both sides, with whatever source counts are asked for."""
    result = resolve(
        [spine("G02001", en="Kottarakkara", ml="കൊട്ടാരക്കര", key=1)],
        [sakarma(1, "കൊട്ടാരക്കര")],
        [sulekha("Kottarakkara Grama Panchayat", sid="a")],
        [],
    )
    return assess(result, Source(**kwargs))


@pytest.fixture()
def clean() -> Quality:
    """A fixture where every side resolves and every source row joins."""
    return resolved(
        projects=(("a", "2023-2024", 357),),
        projects_total=357,
        meetings=(("1", "2023-2024", 42), ("1", "2024-2025", 58)),
        meetings_total=100,
        geometry=frozenset({"G02001"}),
    )


def gate_named(quality: Quality, fragment: str):
    return next(g for g in quality.gates if fragment in g.name)


# --- the happy path ---------------------------------------------------------


def test_a_fully_resolving_fixture_passes_every_gate(clean):
    assert [g.name for g in clean.gates if not g.ok] == []
    assert clean.ok
    assert clean.failures() == []


def test_a_passing_gate_still_carries_its_count(clean):
    """ "PASS" alone is not evidence; 443,235 meetings checked is."""
    meetings = gate_named(clean, "meeting_manifest")
    assert (meetings.checked, meetings.failed) == (100, 0)
    assert "100 checked, 0 failed" in meetings.render()


def test_the_report_and_the_manifest_are_written_only_once_asked(clean, tmp_path):
    paths = resolve_paths(tmp_path)
    assert not paths.out.exists()
    report, manifest = write(clean, paths)
    assert report.name == "quality_report.txt" and manifest.name == "manifest.json"
    assert "[PASS] every sakarma body resolves" in report.read_text(encoding="utf-8")
    assert json.loads(manifest.read_text(encoding="utf-8"))["ok"] is True


def test_two_runs_over_unchanged_sources_fingerprint_the_same(clean):
    """The stamp moves; nothing else may, or a diff of two manifests says nothing."""
    first, second = (
        clean.manifest(),
        resolved(
            projects=(("a", "2023-2024", 357),),
            projects_total=357,
            meetings=(("1", "2023-2024", 42), ("1", "2024-2025", 58)),
            meetings_total=100,
            geometry=frozenset({"G02001"}),
        ).manifest(),
    )
    assert first["fingerprint"] == second["fingerprint"]


# --- the gates --------------------------------------------------------------


def test_an_unresolved_sakarma_body_fails_the_gate_and_names_it():
    """A body with no lb_key drops its meetings from the join silently. Loudly, here."""
    result = resolve(
        [spine("G02001", ml="കൊട്ടാരക്കര")],
        [sakarma(1, "കൊട്ടാരക്കര"), sakarma(2, "വേങ്ങര", dist="9")],
        [],
        [],
    )
    quality = assess(result, Source())
    failed = gate_named(quality, "sakarma body resolves")
    assert not quality.ok
    assert (failed.checked, failed.failed) == (2, 1)
    assert "വേങ്ങര" in failed.detail
    assert any("വേങ്ങര" in problem for problem in quality.failures())


def test_a_failing_gate_stops_the_run_before_anything_is_written(tmp_path):
    """The CLI's gate raises. Nothing downstream of it -- not the derived schema,
    not the report -- has a chance to write a half-resolved build to disk."""
    from master.cli import _gate

    paths = resolve_paths(tmp_path)
    result = resolve([spine("G02001", ml="കൊട്ടാരക്കര")], [sakarma(1, "വേങ്ങര", dist="9")], [], [])

    with pytest.raises(SystemExit):
        _gate(assess(result, Source()))

    assert not paths.out.exists()


def test_an_unresolved_sulekha_year_row_fails_the_gate():
    result = resolve(
        [spine("G02001", en="Kottarakkara")],
        [],
        [sulekha("Kottarakkara"), sulekha("Vengara", sid="b", li=2, dist="9")],
        [],
    )
    failed = gate_named(assess(result, Source()), "sulekha year-row resolves")
    assert (failed.checked, failed.failed) == (2, 1)
    assert "Vengara" in failed.detail


def test_a_project_row_that_joins_to_no_lb_key_fails_the_gate(clean):
    """The finance join is (sulekha_lb_id, year_label); a row outside it is lost."""
    quality = resolved(
        projects=(("a", "2023-2024", 357), ("ghost", "2023-2024", 12)),
        projects_total=369,
    )
    failed = gate_named(quality, "projects row")
    assert (failed.checked, failed.failed) == (369, 12)
    assert "ghost" in failed.detail


def test_a_project_row_that_never_reaches_a_local_body_is_counted_too():
    """An orphan never appears in the grouped aggregate at all, so the total is
    compared as well as the join -- otherwise it would be invisible to the gate."""
    quality = resolved(projects=(("a", "2023-2024", 357),), projects_total=400)
    assert gate_named(quality, "projects row").failed == 43


def test_a_meeting_whose_body_did_not_resolve_fails_the_gate():
    quality = resolved(meetings=(("1", "2023-2024", 42), ("99", "2023-2024", 7)), meetings_total=49)
    failed = gate_named(quality, "meeting_manifest")
    assert (failed.checked, failed.failed) == (49, 7)
    assert "99" in failed.detail


def test_two_bodies_sharing_an_lb_code_fail_the_uniqueness_gate():
    """lb_code is the natural key: two rows carrying it means every join doubles."""
    result = resolve(
        [
            spine("G02001", en="Kottarakkara", key=1),
            spine("G02001", en="Kottarakkara East", key=2),
        ],
        [],
        [],
        [],
    )
    failed = gate_named(assess(result, Source()), "share an lb_code")
    assert failed.failed == 1
    assert "G02001" in failed.detail


def test_a_body_with_no_district_fails_the_gate():
    body = spine("G02001", en="Kottarakkara")
    body["district_name"] = ""
    failed = gate_named(assess(resolve([body], [], [], []), Source()), "district and a tier")
    assert (failed.checked, failed.failed) == (1, 1)
    assert "G02001" in failed.detail


def test_a_body_with_no_tier_fails_the_same_gate():
    body = spine("G02001", en="Kottarakkara")
    body["lb_type"] = ""
    assert gate_named(assess(resolve([body], [], [], []), Source()), "district and a tier").failed


def test_an_override_problem_fails_the_run_without_belonging_to_a_gate():
    """A typo'd override is not a count of rows; it is one line a human must fix."""
    typo = {
        "source": "sakarma",
        "district_name": "KOLLAM",
        "lb_type": GP,
        "source_name": "കിഴക്കേക്കല്ലട",
        "lb_code": "G02999",
        "reason": "typo",
    }
    quality = assess(resolve([spine("G02046", ml="ഈസ്റ്റ് കല്ലട")], [], [], [typo]), Source())
    assert not quality.ok
    assert any("G02999" in problem for problem in quality.failures())
    assert all(gate.ok for gate in quality.gates)


# --- coverage, which is not failure -----------------------------------------


def test_a_body_with_no_election_result_passes_and_is_counted_in_coverage():
    """Mattannur Municipality: in the SEC's registry, in no result feed, and with
    fourteen years of projects and 366 meetings to show regardless."""
    mattannur = spine("M13057", en="Mattannur", lb_type="Municipality", first="", last="", key=2)
    result = resolve([spine("G02001", en="Kottarakkara"), mattannur], [], [], [])
    quality = assess(result, Source())

    assert quality.ok
    assert quality.counts["bodies_without_election_result"] == 1
    assert quality.no_election_result == ("Mattannur (M13057)",)
    assert "Mattannur (M13057)" in quality.render()
    assert any("in_elections = false" in note for note in quality.notes)


def test_bodies_without_geometry_are_reported_rather_than_gated():
    """205 of 1,238 have no boundary. That is KSMART's coverage, not a build error."""
    quality = resolved(geometry=frozenset())
    assert quality.ok
    assert quality.counts["bodies_with_geometry"] == 0
    assert "bodies with geometry" in quality.render()


def test_bodies_with_meetings_are_counted_from_the_matches(clean):
    assert clean.counts["bodies_with_meetings"] == 1
    assert clean.counts["bodies"] == 1


def test_meetings_are_reported_per_financial_year(clean):
    assert clean.meetings_per_year == (("2023-2024", 42), ("2024-2025", 58))
    assert "2024-2025" in clean.render()
    assert clean.manifest()["meetings_per_year"]["2023-2024"] == 42


def test_match_methods_are_reported_for_both_sides(clean):
    """So a reviewer sees how many bodies rest on fuzzy matching rather than exact."""
    assert clean.sakarma_methods == {"exact": 1}
    assert clean.sulekha_methods == {"exact": 1}
    assert "resting on a similarity score" in clean.render()
    assert clean.manifest()["match_methods"]["sakarma_bodies"] == {"exact": 1}


def test_a_similarity_match_is_shown_as_a_share_of_the_side_it_came_from():
    result = resolve(
        [
            spine("G02001", ml="കൊട്ടാരക്കര", key=1),
            spine("G02002", ml="ചവറ", key=2),
            spine("G02003", ml="പേരൂർ", key=3),
        ],
        [sakarma(1, "കൊട്ടാരക്കര"), sakarma(2, "ചവറാ"), sakarma(3, "വേങ്ങര")],
        [],
        [],
    )
    quality = assess(result, Source())
    assert quality.sakarma_methods == {"exact": 1, "similarity": 1, "elimination": 1}
    assert "1 of 3 (33.3%)" in quality.render()


# --- against the built database ---------------------------------------------

LIVE = os.environ.get(
    "MASTER_DATABASE_URL", "postgresql://sambandh:sambandh@localhost:55432/sambandh"
)


@pytest.fixture()
def live_db():
    """The built master database, or a skip. Read-only: this fixture writes nothing."""
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(LIVE, connect_timeout=3)
    except Exception as exc:  # pragma: no cover - depends on the machine
        pytest.skip(f"no master database at {LIVE}: {exc}")
    from master.db import Database

    with conn:
        db = Database(conn)
        if not db.scalar("SELECT to_regclass('meetings.lb_year_summary')"):
            pytest.skip("master database is not built yet")
        yield db


@pytest.mark.integration
def test_the_report_agrees_with_the_tables_it_describes(live_db):
    """The report's body count is ``core.local_body``, and its meetings per year
    are ``meetings.lb_year_summary``. Two numbers a reader will compare, so the
    build compares them first."""
    from master.crosswalk import plan

    quality = assess(plan(live_db, resolve_paths()), read_source(live_db))

    assert quality.counts["bodies"] == live_db.scalar("SELECT count(*) FROM core.local_body")
    per_year = {
        row["year_label"]: int(row["meetings"])
        for row in live_db.query(
            "SELECT year_label, sum(meetings)::int AS meetings "
            "FROM meetings.lb_year_summary GROUP BY 1"
        )
    }
    assert dict(quality.meetings_per_year) == per_year


@pytest.mark.integration
def test_the_live_build_passes_every_gate(live_db):
    from master.crosswalk import plan

    quality = assess(plan(live_db, resolve_paths()), read_source(live_db))
    assert quality.failures() == []
    assert quality.counts["bodies_with_geometry"] == live_db.scalar(
        "SELECT count(*) FROM core.local_body WHERE ksmart_lb_code IS NOT NULL"
    )
