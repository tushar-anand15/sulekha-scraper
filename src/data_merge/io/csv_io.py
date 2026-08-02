"""Canonical CSV read and write, with the in-place-mutation trap closed.

The pipeline this replaces had three merge stages that each read
``candidates_<year>.csv``, added columns, and wrote it back to the same path.
Running one twice enriched already-enriched data and silently corrupted front
counts and gender sources. Nothing in the code said so; you had to know.

:class:`CsvIO` makes that unrepresentable. It remembers every path it has read
during a run and refuses to write to one of them. The guard is structural, so
"enrich the file in place" fails loudly the first time rather than producing
plausible wrong numbers.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from pathlib import Path

from data_merge.schema import SCHEMA, CandidateRow, check_columns, conform

ENCODING = "utf-8-sig"
"""Matches the shipped files exactly -- they carry a BOM, and a golden-file
comparison that differs only in byte order mark is a false alarm nobody needs."""


class InPlaceWriteError(RuntimeError):
    """A stage tried to write to a path it had already read this run."""


class CsvIO:
    """Reader/writer for one run.

    One instance per build. Sharing an instance across runs would let a stale
    read from an earlier run block a legitimate write.
    """

    def __init__(self) -> None:
        self._read: set[Path] = set()
        self._written: set[Path] = set()

    @property
    def paths_read(self) -> frozenset[Path]:
        return frozenset(self._read)

    @property
    def paths_written(self) -> frozenset[Path]:
        return frozenset(self._written)

    def read(self, path: str | Path) -> list[dict[str, str]]:
        """Read a CSV to a list of string dicts, recording the path.

        Values stay strings throughout -- no numeric inference, so a vote count
        of ``007`` and an empty age survive the round trip unchanged.
        """
        resolved = Path(path).resolve()
        with resolved.open(encoding=ENCODING, newline="") as fh:
            rows = [dict(r) for r in csv.DictReader(fh)]
        self._read.add(resolved)
        return rows

    def read_candidates(self, path: str | Path) -> list[CandidateRow]:
        """Read a canonical candidate file, asserting its columns."""
        resolved = Path(path).resolve()
        with resolved.open(encoding=ENCODING, newline="") as fh:
            reader = csv.DictReader(fh)
            check_columns(list(reader.fieldnames or []), origin=str(resolved))
            rows = [dict(r) for r in reader]
        self._read.add(resolved)
        return rows

    def write(
        self,
        path: str | Path,
        rows: Iterable[dict[str, str]],
        fieldnames: Sequence[str],
    ) -> int:
        """Write rows and return the count.

        Raises :class:`InPlaceWriteError` if this run already read ``path``.
        """
        resolved = Path(path).resolve()
        if resolved in self._read:
            raise InPlaceWriteError(
                f"refusing to write {resolved}: this run already read it. "
                "Stages must write new outputs, never enrich their own inputs."
            )
        resolved.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with resolved.open("w", encoding=ENCODING, newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(fieldnames), extrasaction="raise")
            writer.writeheader()
            for row in rows:
                writer.writerow({k: row.get(k, "") for k in fieldnames})
                count += 1
        self._written.add(resolved)
        return count

    def write_candidates(self, path: str | Path, rows: Iterable[CandidateRow]) -> int:
        """Write a canonical 31-column candidate file.

        Each row is conformed on the way out, so a builder that omits an
        optional column writes an empty cell rather than a missing one, and a
        builder that invents a column fails here rather than downstream.
        """
        return self.write(path, (conform(dict(r)) for r in rows), SCHEMA)
