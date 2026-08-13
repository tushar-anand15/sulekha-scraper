"""Talking to the master database.

The prototype shelled out to ``docker exec … psql`` for every statement, which
worked but tied the build to one container name and made every result a screen
scrape. This is the same conversation over psycopg 3.

Two things are deliberately kept from the psql version rather than modernised
away:

*Reads still go through ``COPY … TO STDOUT WITH (FORMAT csv)``.* A native
``dict_row`` cursor would return typed Python objects, and the matching code
reads its keys as strings throughout -- a ``last_cycle`` arriving as ``None``
rather than ``''``, or ``district_ord`` as ``1`` rather than ``'1'``, changes
which bodies group together. CSV keeps every value a string, which is what the
cascade was written against.

The cost of that is real and worth naming: NULL and the empty string are the
same value here, so nothing read through ``query`` can tell "the source has no
answer" from "the column is blank". Where that distinction mattered -- whether
the SEC ever published a result for a body -- it is now carried as an explicit
flag rather than inferred from an empty ``first_cycle`` (see
``crosswalk.is_in_elections``). The protocol stays as it is: the app reads this
database through asyncpg, which types properly, so the only code affected is the
matching, and the matching is where changing it would do harm.

*The connection is autocommit.* Each psql invocation was its own transaction, so
a failed statement left everything before it committed. Wrapping the build in one
transaction would be tidier, but it would also change what a half-finished run
leaves behind, and this port is not the place to decide that.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg


class Database:
    """A thin, honest wrapper: no ORM, no query builder, no connection pool."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    @property
    def connection(self) -> psycopg.Connection:
        return self._conn

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        """Run one statement, or several separated by semicolons.

        Multiple statements are only legal when no parameters are bound, because
        psycopg falls back to the extended protocol as soon as they are. Several
        statements in one call is how the original grouped related DDL, so it is
        preserved rather than split.
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, params)  # type: ignore[arg-type]

    def scalar(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)  # type: ignore[arg-type]
            row = cur.fetchone()
        return None if row is None else row[0]

    def query(self, sql: str) -> list[dict[str, str]]:
        """Run a SELECT and return its rows as strings keyed by column name.

        Every value is a string and NULL is ``''``; see the module docstring for
        why that is the point rather than an oversight.
        """
        buf = io.BytesIO()
        with self._conn.cursor() as cur:
            with cur.copy(f"COPY ({sql}) TO STDOUT WITH (FORMAT csv, HEADER true)") as copy:
                for block in copy:
                    buf.write(block)
        text = buf.getvalue().decode("utf-8")
        return list(csv.DictReader(io.StringIO(text)))

    def copy_rows(self, table: str, cols: Sequence[str], rows: Sequence[Sequence[Any]]) -> int:
        """Load rows built in Python. ``None`` becomes NULL."""
        collist = ", ".join(cols)
        with self._conn.cursor() as cur:
            with cur.copy(f"COPY {table} ({collist}) FROM STDIN") as copy:
                for row in rows:
                    copy.write_row(row)
        return len(rows)

    def copy_csv(self, table: str, cols: Sequence[str], data: bytes, *, header: bool) -> None:
        """Load CSV bytes straight from a file, without parsing them in Python."""
        collist = ", ".join(f'"{c}"' for c in cols)
        opts = f"FORMAT csv, HEADER {'true' if header else 'false'}"
        with self._conn.cursor() as cur:
            with cur.copy(f"COPY {table} ({collist}) FROM STDIN WITH ({opts})") as copy:
                copy.write(data)

    def copy_text(self, table: str, cols: Sequence[str], data: bytes) -> None:
        """Load Postgres' own tab-separated text format, where ``\\N`` is NULL."""
        collist = ", ".join(cols)
        with self._conn.cursor() as cur:
            with cur.copy(f"COPY {table} ({collist}) FROM STDIN") as copy:
                copy.write(data)

    def run_script(self, path: Path) -> None:
        """Execute a .sql file as one call, the way ``psql -f`` would."""
        self.execute(path.read_text(encoding="utf-8"))


@contextmanager
def connect(url: str) -> Iterator[Database]:
    """Open the master database for the length of one build."""
    with psycopg.connect(url, autocommit=True) as conn:
        yield Database(conn)
