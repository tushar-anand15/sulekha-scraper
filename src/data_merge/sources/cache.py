"""The SQLite response caches.

All four kinds -- SEC trend, LSGD, WIYR, contesting-candidates -- share one
table::

    resp(key TEXT PRIMARY KEY, json TEXT NOT NULL, fetched_at REAL NOT NULL)

They differ only in how the key is spelled and what the value decodes to:

* SEC/WIYR/contest keys are ``<endpoint>|<k=v&...>`` with params sorted, and
  values decode to **dicts** -- the ajax response body.
* LSGD keys are ``GET|<url>`` and values decode to **strings** -- raw HTML.

So one reader covers all of them, and it does not assume which shape it will
get. Opened read-only: these files are the sources of record and a build must
not be able to write to them even by accident.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any


class CacheError(RuntimeError):
    """A cache could not be read, or one of its values would not decode."""


@dataclass(frozen=True, slots=True)
class CachedResponse:
    """One cached response, with its decoded body."""

    key: str
    value: Any
    fetched_at: float

    @property
    def endpoint(self) -> str:
        """The part of the key before the first ``|``: an endpoint, or ``GET``."""
        return self.key.split("|", 1)[0]


class ResponseCache:
    """Read-only reader over one cache file.

    Usable as a context manager; the connection is otherwise closed on
    :meth:`close`.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise CacheError(f"no cache at {self.path}")
        try:
            self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise CacheError(f"cannot open {self.path} read-only: {exc}") from exc
        self._require_table()

    def _require_table(self) -> None:
        row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='resp'"
        ).fetchone()
        if row is None:
            raise CacheError(f"{self.path} has no 'resp' table -- is it a response cache?")

    def __enter__(self) -> ResponseCache:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    # -- counting and key access -------------------------------------------

    def count(self, prefix: str | None = None) -> int:
        """Rows in the cache, optionally restricted to a key prefix."""
        if prefix is None:
            return int(self._conn.execute("SELECT COUNT(*) FROM resp").fetchone()[0])
        return int(
            self._conn.execute(
                "SELECT COUNT(*) FROM resp WHERE key LIKE ? ESCAPE '\\'",
                (_like_prefix(prefix),),
            ).fetchone()[0]
        )

    def endpoints(self) -> dict[str, int]:
        """Row count per endpoint -- the quickest sanity check on a cache."""
        rows = self._conn.execute(
            "SELECT substr(key, 1, instr(key, '|') - 1) AS endpoint, COUNT(*) "
            "FROM resp GROUP BY endpoint ORDER BY 2 DESC"
        ).fetchall()
        return {str(endpoint): int(n) for endpoint, n in rows}

    def keys(self, prefix: str | None = None) -> Iterator[str]:
        """Keys in insertion-independent (sorted) order, optionally by prefix."""
        if prefix is None:
            cursor = self._conn.execute("SELECT key FROM resp ORDER BY key")
        else:
            cursor = self._conn.execute(
                "SELECT key FROM resp WHERE key LIKE ? ESCAPE '\\' ORDER BY key",
                (_like_prefix(prefix),),
            )
        for (key,) in cursor:
            yield str(key)

    # -- value access -------------------------------------------------------

    def get(self, key: str) -> Any | None:
        """The decoded value for ``key``, or ``None`` when it is not cached.

        A missing key is an ordinary outcome -- an interrupted scrape simply
        never cached it -- so this returns ``None``. A value that fails to
        decode signals real corruption, and raises.
        """
        row = self._conn.execute("SELECT json FROM resp WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        return self._decode(key, row[0])

    def require(self, key: str) -> Any:
        """Like :meth:`get`, but a missing key is an error."""
        value = self.get(key)
        if value is None:
            raise CacheError(f"{self.path.name}: no cached response for {key!r}")
        return value

    def items(self, prefix: str | None = None) -> Iterator[CachedResponse]:
        """Every cached response, decoded, optionally restricted by key prefix.

        Streams row by row: the LSGD caches hold ~23,000 HTML pages and run to
        hundreds of megabytes, so materialising them is not an option.
        """
        if prefix is None:
            cursor = self._conn.execute("SELECT key, json, fetched_at FROM resp ORDER BY key")
        else:
            cursor = self._conn.execute(
                "SELECT key, json, fetched_at FROM resp "
                "WHERE key LIKE ? ESCAPE '\\' ORDER BY key",
                (_like_prefix(prefix),),
            )
        for key, payload, fetched_at in cursor:
            yield CachedResponse(
                key=str(key), value=self._decode(str(key), payload), fetched_at=float(fetched_at)
            )

    def _decode(self, key: str, payload: str) -> Any:
        try:
            return json.loads(payload)
        except (json.JSONDecodeError, TypeError) as exc:
            raise CacheError(f"{self.path.name}: value for {key!r} will not decode: {exc}") from exc


def _like_prefix(prefix: str) -> str:
    """Escape LIKE wildcards so a key prefix means what it says.

    Endpoint keys contain no ``%`` or ``_`` today, but LSGD keys are URLs and
    ``_`` is a single-character wildcard -- ``GET|...district_`` would match far
    more than intended.
    """
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"
