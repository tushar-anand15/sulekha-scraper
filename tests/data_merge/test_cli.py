"""The command line.

The behaviour worth pinning is the refusal: a build whose expectations fail
must write nothing at all. A half-written year on disk looks like a successful
run to everything downstream, which is worse than no year.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from data_merge import cli
from data_merge.spec import spec_for


class _FakeBuilt:
    def __init__(self, candidates: tuple[dict[str, str], ...]) -> None:
        self.candidates = candidates
        self.wards: tuple[dict[str, str], ...] = ()
        self.local_bodies: tuple[dict[str, str], ...] = ()
        self.report: Any = None


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestDescribeCommands:
    def test_years_lists_all_four_cycles(self, runner: CliRunner) -> None:
        result = runner.invoke(cli.main, ["years"])
        assert result.exit_code == 0
        for year in (2010, 2015, 2020, 2025):
            assert str(year) in result.output

    def test_years_marks_a_cycle_with_no_builder(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "_builders", dict)
        result = runner.invoke(cli.main, ["years"])
        assert result.output.count("[builder not implemented]") == 4

    def test_paths_reports_missing_inputs_and_exits_nonzero(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        result = runner.invoke(cli.main, ["--root", str(tmp_path), "paths"])
        assert result.exit_code == 1
        assert "MISSING" in result.output


class TestYearSelection:
    def test_neither_year_nor_all_is_a_usage_error(self, runner: CliRunner) -> None:
        result = runner.invoke(cli.main, ["validate"])
        assert result.exit_code != 0
        assert "--year" in result.output

    def test_an_unknown_year_names_what_is_available(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "_builders", lambda: {2010: object()})
        result = runner.invoke(cli.main, ["validate", "--year", "1999"])
        assert result.exit_code != 0
        assert "1999" in result.output
        assert "2010" in result.output


class TestBuildRefusesToWriteOnFailure:
    """The load-bearing CLI behaviour."""

    def test_a_failing_gate_writes_nothing(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # One row where the spec expects 70,524: the gate cannot pass.
        built = _FakeBuilt(({"ward_code": "G01001001", "lb_code": "G01001"},))
        monkeypatch.setattr(cli, "_builders", lambda: {2010: lambda *a, **k: built})

        result = runner.invoke(cli.main, ["--root", str(tmp_path), "build", "--year", "2010"])

        assert result.exit_code != 0
        assert "nothing written" in result.output
        assert not (tmp_path / "final").exists(), "a failed build must leave no output"

    def test_skip_validation_is_documented_as_diagnosis_only(self, runner: CliRunner) -> None:
        result = runner.invoke(cli.main, ["build", "--help"])
        assert "diagnosis only" in result.output


@pytest.mark.integration
class TestRealBuild:
    """Against the real 2010 sources. Slow; excluded from the default sweep."""

    def test_validate_passes_for_2010(self, runner: CliRunner) -> None:
        result = runner.invoke(cli.main, ["validate", "--year", "2010"])
        assert result.exit_code == 0, result.output
        assert "checks passed" in result.output

    def test_build_writes_every_output_and_a_manifest(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        from data_merge.config import resolve_paths

        source = resolve_paths()
        # Build into a scratch root, leaving the real data/final untouched.
        (tmp_path / "raw").symlink_to(source.raw)
        (tmp_path / "reference").symlink_to(source.reference)
        (tmp_path / "interim").symlink_to(source.root / "interim")

        result = runner.invoke(cli.main, ["--root", str(tmp_path), "build", "--year", "2010"])
        assert result.exit_code == 0, result.output

        year_dir = tmp_path / "final" / "2010"
        assert (year_dir / "candidates_2010.csv").exists()
        assert (year_dir / "wards_2010.csv").exists()
        assert (year_dir / "local_bodies_2010.csv").exists()
        assert (year_dir / "manifest_2010.json").exists()
        assert (year_dir / "data_quality_report_2010.txt").exists()

    def test_two_runs_produce_identical_manifests(self, tmp_path: Path) -> None:
        """Same inputs, same code, same numbers -- provable without re-reading
        gigabytes of CSV."""
        import json

        from data_merge.config import resolve_paths
        from data_merge.years import y2010

        paths = resolve_paths()
        built = y2010.build_year(paths, pdf_cache_dir=paths.root / "interim" / "pdf_text")
        spec = spec_for(2010)

        first = cli._write_year(
            cli.resolve_paths(tmp_path / "a"), spec, built, checks_text="x"
        )
        second = cli._write_year(
            cli.resolve_paths(tmp_path / "b"), spec, built, checks_text="x"
        )
        assert first == second

        def fingerprint(root: Path) -> str:
            payload = json.loads((root / "final/2010/manifest_2010.json").read_text())
            return str(payload["fingerprint"])

        assert fingerprint(tmp_path / "a") == fingerprint(tmp_path / "b")
