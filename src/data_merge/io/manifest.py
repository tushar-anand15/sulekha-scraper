"""Run manifest: what went in, what came out, which code produced it.

Two runs over unchanged inputs must be provably identical. The manifest is how
that is proved -- it records a SHA-256 per raw input, a row count per output,
and the package version, so a diff of two manifests answers "did anything
actually change?" without re-reading gigabytes of CSV.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

_CHUNK = 1 << 20


def sha256_file(path: str | Path) -> str:
    """SHA-256 of a file, read in chunks -- the caches run to hundreds of MB."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One input file, identified by content rather than by path alone."""

    path: str
    sha256: str
    bytes: int


@dataclass
class Manifest:
    """Accumulated across a build, written once at the end."""

    year: int
    version: str
    inputs: list[ManifestEntry] = field(default_factory=list)
    outputs: dict[str, int] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def add_input(self, path: str | Path) -> ManifestEntry:
        """Hash an input and record it. Symlinks are followed to the real bytes."""
        resolved = Path(path).resolve()
        entry = ManifestEntry(
            path=resolved.name,
            sha256=sha256_file(resolved),
            bytes=resolved.stat().st_size,
        )
        self.inputs.append(entry)
        return entry

    def add_output(self, path: str | Path, rows: int) -> None:
        self.outputs[Path(path).name] = rows

    def fingerprint(self) -> str:
        """A stable digest of inputs, outputs and counts.

        Excludes the wall-clock stamp so two runs over unchanged inputs compare
        equal -- which is the property worth asserting.
        """
        body = {
            "year": self.year,
            "version": self.version,
            "inputs": sorted((e.path, e.sha256) for e in self.inputs),
            "outputs": dict(sorted(self.outputs.items())),
            "counts": dict(sorted(self.counts.items())),
        }
        return hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "year": self.year,
            "version": self.version,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "fingerprint": self.fingerprint(),
            "inputs": [asdict(e) for e in self.inputs],
            "outputs": dict(sorted(self.outputs.items())),
            "counts": dict(sorted(self.counts.items())),
            "notes": list(self.notes),
        }

    def write(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return target
