"""The package must be unable to reach the network.

"No scraping" is a scope boundary. The caches and PDFs on disk are the sources
of record, and a stage that quietly re-fetched one would break the
reproducibility this rebuild exists to establish -- silently, because a fetch
that succeeds looks exactly like a cache hit.

So the boundary is enforced by assertion, not by documentation alone: nothing
under ``src/data_merge`` may import an HTTP client, at any depth.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "data_merge"

FORBIDDEN = {
    "requests",
    "httpx",
    "aiohttp",
    "urllib.request",
    "urllib3",
    "http.client",
    "socket",
    "ftplib",
    "telnetlib",
}

SOURCE_FILES = sorted(PACKAGE.rglob("*.py"))


def _imported_modules(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def test_the_package_has_source_files_to_check() -> None:
    """Guards the guard: an empty glob would make every check below vacuous."""
    assert len(SOURCE_FILES) >= 8


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: p.name)
def test_no_module_imports_an_http_client(path: Path) -> None:
    imported = _imported_modules(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    offending = {
        name
        for name in imported
        for banned in FORBIDDEN
        if name == banned or name.startswith(f"{banned}.")
    }
    assert not offending, f"{path.relative_to(PACKAGE)} imports {sorted(offending)}"


def test_importing_the_package_pulls_in_no_http_client() -> None:
    """Catches a transitive dependency dragging one in at import time."""
    import subprocess
    import sys

    probe = (
        "import sys; import data_merge, data_merge.cli, data_merge.sources; "
        f"banned = {sorted(FORBIDDEN)!r}; "
        "loaded = [m for m in banned if m in sys.modules]; "
        "print(','.join(loaded))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "", f"import pulled in {result.stdout.strip()}"
