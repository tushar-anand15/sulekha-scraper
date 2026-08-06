"""``geo.build`` must be unable to reach the network.

The cached tiles and release assets on disk are the sources of record. A build stage
that quietly re-fetched one would break reproducibility *silently*, because a fetch
that succeeds looks exactly like a cache hit -- the failure only surfaces later, when
the upstream server has changed and yesterday's output no longer reproduces.

So the boundary is enforced by assertion rather than documentation, the same way
``tests/data_merge/test_no_network.py`` does it. Note the scope difference: there the
whole package is forbidden the network, here only ``build/``. ``geo.fetch`` exists
precisely to make the requests.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "geo"
BUILD = SRC / "build"

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

BUILD_FILES = sorted(BUILD.rglob("*.py"))


def _imported_modules(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def _offending(modules: set[str]) -> set[str]:
    """A forbidden import, or anything nested beneath one."""
    return {
        m for m in modules if any(m == bad or m.startswith(bad + ".") for bad in FORBIDDEN)
    }


def test_build_half_is_not_empty() -> None:
    """Guards the guard: an empty glob would make every assertion below vacuous."""
    assert BUILD_FILES, f"no Python files found under {BUILD}"


@pytest.mark.parametrize("path", BUILD_FILES, ids=lambda p: p.name)
def test_build_module_imports_no_http_client(path: Path) -> None:
    offending = _offending(_imported_modules(ast.parse(path.read_text(encoding="utf-8"))))
    assert not offending, (
        f"{path.relative_to(SRC)} imports {sorted(offending)}. "
        "geo.build must read the cache, never the network -- put the fetch in geo.fetch."
    )


def test_detects_a_forbidden_import(tmp_path: Path) -> None:
    """The check fails when it should.

    Asserted against a fixture rather than by editing real source, so the test proves
    the detector works without anyone having to introduce a real violation.
    """
    fixture = tmp_path / "leaky.py"
    fixture.write_text("import requests\n", encoding="utf-8")
    assert _offending(_imported_modules(ast.parse(fixture.read_text()))) == {"requests"}


def test_detects_a_nested_forbidden_import(tmp_path: Path) -> None:
    fixture = tmp_path / "leaky.py"
    fixture.write_text("from urllib.request import urlopen\n", encoding="utf-8")
    assert _offending(_imported_modules(ast.parse(fixture.read_text()))) == {"urllib.request"}


def test_fetch_half_is_allowed_the_network() -> None:
    """The split is only meaningful if the other side really is unrestricted."""
    assert (SRC / "fetch").is_dir()
