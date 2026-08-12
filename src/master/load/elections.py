"""Load the ``data_merge`` election CSVs into ``src_elections``.

Every column lands as text. Typing belongs in the modelled layer, not the
source layer -- a source table that refuses a row because a vote count is
blank has lost data the CSV was willing to carry.

2010 carries different columns from 2015/2020/2025 for wards and local bodies,
so each table is the union of every year's columns and each cycle is copied with
its own explicit column list.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from master.config import CYCLES, Paths
from master.db import Database

TABLES: Final = ("candidates", "wards", "local_bodies")


def header(path: Path) -> list[str]:
    """The CSV's own column names, BOM stripped."""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return next(csv.reader(fh))


def union_columns(headers: Sequence[Sequence[str]]) -> list[str]:
    """Every column any cycle carries, in first-seen order.

    Order is preserved rather than sorted so the table reads the way the CSVs
    do, and so adding a cycle appends columns instead of reshuffling them.
    """
    union: list[str] = []
    for cols in headers:
        for col in cols:
            if col not in union:
                union.append(col)
    return union


def cycle_paths(paths: Paths, table: str) -> dict[int, Path]:
    return {year: paths.cycle(year) / f"{table}_{year}.csv" for year in CYCLES}


def load(db: Database, paths: Paths) -> dict[str, int]:
    """Load all three tables for all four cycles. Returns rows per table."""
    db.execute("CREATE SCHEMA IF NOT EXISTS src_elections;")
    loaded: dict[str, int] = {}

    for table in TABLES:
        files = cycle_paths(paths, table)
        missing = [str(p) for p in files.values() if not p.exists()]
        if missing:
            raise FileNotFoundError(f"{table}: missing {missing}")
        headers = {year: header(path) for year, path in files.items()}
        union = union_columns([headers[year] for year in CYCLES])

        # `cycle` goes last and starts null, because a COPY cannot share a
        # statement with anything else: each cycle is copied, then stamped.
        cols_ddl = ",\n  ".join(f'"{c}" text' for c in union)
        db.execute(f'DROP TABLE IF EXISTS src_elections."{table}";')
        db.execute(f'CREATE TABLE src_elections."{table}" (\n  {cols_ddl},\n  cycle int\n);')

        for year in CYCLES:
            db.copy_csv(
                f'src_elections."{table}"',
                headers[year],
                files[year].read_bytes(),
                header=True,
            )
            db.execute(
                f'UPDATE src_elections."{table}" SET cycle = %s WHERE cycle IS NULL;', [year]
            )

        db.execute(f'ALTER TABLE src_elections."{table}" ALTER COLUMN cycle SET NOT NULL;')
        loaded[table] = int(db.scalar(f'SELECT count(*) FROM src_elections."{table}";'))

    return loaded
