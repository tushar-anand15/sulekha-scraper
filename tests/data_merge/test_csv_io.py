"""CSV round-tripping, the read-then-write guard, and manifest stability."""

from __future__ import annotations

from pathlib import Path

import pytest

from data_merge.config import ENV_ROOT, resolve_paths
from data_merge.io.csv_io import CsvIO, InPlaceWriteError
from data_merge.io.manifest import Manifest, sha256_file
from data_merge.schema import SCHEMA, SchemaError, blank_row


def _candidate_row(**overrides: str) -> dict[str, str]:
    row = blank_row()
    row.update(
        district_code="D01001",
        district_name="THIRUVANANTHAPURAM",
        lb_type="Grama Panchayat",
        lb_code="G01001",
        lb_name="AMBOORI",
        ward_code="G01001001",
        ward_no="1",
        ward_name="MEENANKAL",
        party_name="INC",
        total_votes="1204",
    )
    row.update(overrides)
    return row


class TestRoundTrip:
    def test_every_column_survives_including_empty_ones(self, tmp_path: Path) -> None:
        rows = [_candidate_row(), _candidate_row(ward_no="2", total_votes="0")]
        written = CsvIO()
        assert written.write_candidates(tmp_path / "candidates_2015.csv", rows) == 2

        read_back = CsvIO().read_candidates(tmp_path / "candidates_2015.csv")
        assert read_back == rows

    def test_empty_cells_stay_empty_strings_and_are_never_none(self, tmp_path: Path) -> None:
        path = tmp_path / "c.csv"
        CsvIO().write_candidates(path, [_candidate_row()])
        row = CsvIO().read_candidates(path)[0]
        assert row["candidate_age"] == ""
        assert None not in row.values()

    def test_numeric_looking_values_are_not_coerced(self, tmp_path: Path) -> None:
        """A leading zero in a vote count is meaningful data, kept as written."""
        path = tmp_path / "c.csv"
        CsvIO().write_candidates(path, [_candidate_row(total_votes="007", ward_no="01")])
        row = CsvIO().read_candidates(path)[0]
        assert row["total_votes"] == "007"
        assert row["ward_no"] == "01"

    def test_a_row_missing_an_optional_column_writes_as_empty(self, tmp_path: Path) -> None:
        partial = {"lb_code": "G01001", "ward_code": "G01001001"}
        path = tmp_path / "c.csv"
        CsvIO().write_candidates(path, [partial])
        row = CsvIO().read_candidates(path)[0]
        assert list(row) == list(SCHEMA)
        assert row["candidate_age"] == ""

    def test_a_builder_inventing_a_column_fails_at_the_write(self, tmp_path: Path) -> None:
        with pytest.raises(SchemaError, match="outside the schema"):
            CsvIO().write_candidates(tmp_path / "c.csv", [{"candidate_phone": "999"}])

    def test_reading_a_file_whose_columns_are_not_canonical_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "wrong.csv"
        CsvIO().write(path, [{"a": "1", "b": "2"}], ["a", "b"])
        with pytest.raises(SchemaError, match="column set differs"):
            CsvIO().read_candidates(path)

    def test_malayalam_survives_the_round_trip(self, tmp_path: Path) -> None:
        name = "അനിഷ് സന്തോഷ്"
        path = tmp_path / "c.csv"
        CsvIO().write_candidates(path, [_candidate_row(candidate_name=name)])
        assert CsvIO().read_candidates(path)[0]["candidate_name"] == name


class TestInPlaceWriteGuard:
    """The regression test for the class of bug that motivated this rebuild.

    Three merge stages used to read ``candidates_<year>.csv``, add columns and
    write it back. Running one twice enriched already-enriched data.
    """

    def test_writing_to_a_path_read_this_run_raises_and_names_the_path(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "candidates_2015.csv"
        CsvIO().write_candidates(path, [_candidate_row()])

        io = CsvIO()
        rows = io.read_candidates(path)
        with pytest.raises(InPlaceWriteError) as excinfo:
            io.write_candidates(path, rows)
        assert str(path) in str(excinfo.value)

    def test_the_guard_survives_a_path_spelled_differently(self, tmp_path: Path) -> None:
        """``out/../out/c.csv`` and ``out/c.csv`` are the same file."""
        (tmp_path / "out").mkdir()
        path = tmp_path / "out" / "c.csv"
        CsvIO().write_candidates(path, [_candidate_row()])

        io = CsvIO()
        io.read_candidates(path)
        with pytest.raises(InPlaceWriteError):
            io.write_candidates(tmp_path / "out" / ".." / "out" / "c.csv", [_candidate_row()])

    def test_writing_a_different_path_is_allowed(self, tmp_path: Path) -> None:
        source = tmp_path / "in.csv"
        CsvIO().write_candidates(source, [_candidate_row()])

        io = CsvIO()
        rows = io.read_candidates(source)
        assert io.write_candidates(tmp_path / "out.csv", rows) == 1

    def test_a_fresh_run_may_write_what_an_earlier_run_read(self, tmp_path: Path) -> None:
        path = tmp_path / "c.csv"
        CsvIO().write_candidates(path, [_candidate_row()])
        CsvIO().read_candidates(path)
        CsvIO().write_candidates(path, [_candidate_row()])

    def test_paths_read_and_written_are_reported(self, tmp_path: Path) -> None:
        io = CsvIO()
        io.write_candidates(tmp_path / "a.csv", [_candidate_row()])
        io.read_candidates(tmp_path / "a.csv")
        assert io.paths_written == frozenset({(tmp_path / "a.csv").resolve()})
        assert io.paths_read == frozenset({(tmp_path / "a.csv").resolve()})


class TestManifest:
    def test_hashing_is_stable_across_two_runs_on_unchanged_inputs(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "raw.sqlite"
        source.write_bytes(b"pretend cache")

        def build() -> Manifest:
            manifest = Manifest(year=2015, version="0.1.0")
            manifest.add_input(source)
            manifest.add_output(tmp_path / "candidates_2015.csv", 75_251)
            manifest.counts["wards"] = 21_863
            return manifest

        assert build().fingerprint() == build().fingerprint()

    def test_a_changed_input_changes_the_fingerprint(self, tmp_path: Path) -> None:
        source = tmp_path / "raw.sqlite"
        source.write_bytes(b"pretend cache")
        first = Manifest(year=2015, version="0.1.0")
        first.add_input(source)

        source.write_bytes(b"pretend cache, corrected")
        second = Manifest(year=2015, version="0.1.0")
        second.add_input(source)

        assert first.fingerprint() != second.fingerprint()

    def test_a_changed_row_count_changes_the_fingerprint(self, tmp_path: Path) -> None:
        def build(rows: int) -> Manifest:
            manifest = Manifest(year=2015, version="0.1.0")
            manifest.add_output(tmp_path / "candidates_2015.csv", rows)
            return manifest

        assert build(75_251).fingerprint() != build(75_250).fingerprint()

    def test_the_written_manifest_records_inputs_outputs_and_fingerprint(
        self, tmp_path: Path
    ) -> None:
        import json

        source = tmp_path / "raw.sqlite"
        source.write_bytes(b"x" * 2048)
        manifest = Manifest(year=2010, version="0.1.0")
        manifest.add_input(source)
        manifest.add_output(tmp_path / "candidates_2010.csv", 70_523)
        manifest.notes.append("Mattannur absent from the SEC report")

        written = manifest.write(tmp_path / "manifest_2010.json")
        payload = json.loads(written.read_text(encoding="utf-8"))

        assert payload["year"] == 2010
        assert payload["outputs"] == {"candidates_2010.csv": 70_523}
        assert payload["inputs"][0]["sha256"] == sha256_file(source)
        assert payload["inputs"][0]["bytes"] == 2048
        assert payload["fingerprint"] == manifest.fingerprint()
        assert payload["notes"] == ["Mattannur absent from the SEC report"]

    def test_symlinked_inputs_hash_their_real_bytes(self, tmp_path: Path) -> None:
        """The caches are symlinked into ``data/raw``; the manifest must see through."""
        real = tmp_path / "real.sqlite"
        real.write_bytes(b"cache bytes")
        link = tmp_path / "link.sqlite"
        link.symlink_to(real)

        manifest = Manifest(year=2015, version="0.1.0")
        entry = manifest.add_input(link)
        assert entry.sha256 == sha256_file(real)


class TestPathResolution:
    def test_the_default_root_is_the_repo_data_directory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENV_ROOT, raising=False)
        assert resolve_paths().root.name == "data"

    def test_an_explicit_root_beats_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_ROOT, str(tmp_path / "from-env"))
        assert resolve_paths(tmp_path / "explicit").root == (tmp_path / "explicit").resolve()

    def test_the_environment_beats_the_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENV_ROOT, str(tmp_path))
        assert resolve_paths().root == tmp_path.resolve()

    def test_output_paths_are_namespaced_by_year(self, tmp_path: Path) -> None:
        paths = resolve_paths(tmp_path)
        assert paths.candidates_csv(2010) == tmp_path.resolve() / "final/2010/candidates_2010.csv"
        assert paths.manifest_json(2015) == tmp_path.resolve() / "final/2015/manifest_2015.json"

    def test_missing_inputs_are_listed_for_an_empty_root(self, tmp_path: Path) -> None:
        assert resolve_paths(tmp_path).missing_inputs()
