"""Load the geo layers and the reference crosswalks into ``src_geo``.

Geometry lands as jsonb, not PostGIS. The site serves boundaries as GeoJSON
downloads and colours a map by ``lb_code``; none of that needs spatial
predicates. If a later feature needs real geometry operations, swap the image
for ``postgis/postgis`` and cast these columns -- the loader does not change.

Each layer's ``provenance`` block is kept whole. It records boundary vintage and
whether the polygons were delimited for that cycle, and the maps page is
required to state both.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from master.config import Paths
from master.db import Database

LAYER_DDL = """
CREATE TABLE src_geo.layer (
  layer text primary key,
  feature_count int not null,
  provenance jsonb
);
"""

LAYER_FEATURE_DDL = """
CREATE TABLE src_geo.layer_feature (
  layer text not null references src_geo.layer(layer),
  lb_code text,
  ward_code text,
  properties jsonb not null,
  geometry jsonb not null
);
"""


def fold_surplus_fields(rows: Sequence[Sequence[str]], columns: Sequence[str]) -> list[list[str]]:
    """Repair reference rows that carry an unquoted comma in their last column.

    Some override files were hand-edited and have a bare comma inside the
    free-text ``reason``, so the file cannot be streamed verbatim. Surplus
    fields are folded back into the last column rather than dropped: the reason
    is the whole value of a hand-recorded override, and truncating it silently
    would leave a row whose justification reads as a fragment.
    """
    fixed: list[list[str]] = []
    for row in rows:
        row = list(row)
        if len(row) > len(columns):
            row = row[: len(columns) - 1] + [",".join(row[len(columns) - 1 :])]
        fixed.append(row + [""] * (len(columns) - len(row)))
    return fixed


def feature_lines(layer: str, features: Sequence[dict[str, Any]]) -> list[str]:
    """One tab-separated COPY line per GeoJSON feature.

    Tab-separated so the payload never collides with the delimiter; JSON has no
    raw tabs or newlines once dumped compactly. ``\\N`` is Postgres' own NULL
    marker in this format, which is why a ward layer's absent ``lb_code`` does
    not arrive as the two-character string.
    """
    lines = []
    for feature in features:
        props = feature.get("properties") or {}
        lines.append(
            "\t".join(
                [
                    layer,
                    props.get("lb_code") or "\\N",
                    props.get("ward_code") or "\\N",
                    json.dumps(props, ensure_ascii=False),
                    json.dumps(feature["geometry"], ensure_ascii=False),
                ]
            )
        )
    return lines


def load_reference_csvs(db: Database, directory: Path) -> dict[str, int]:
    """Load every hand-maintained crosswalk beside the layers it describes."""
    loaded: dict[str, int] = {}
    for path in sorted(directory.glob("*.csv")):
        name = path.stem
        with path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        columns, body = rows[0], rows[1:]

        ddl = ",\n  ".join(f'"{c}" text' for c in columns)
        db.execute(f'DROP TABLE IF EXISTS src_geo."{name}";')
        db.execute(f'CREATE TABLE src_geo."{name}" (\n  {ddl}\n);')

        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerows(fold_surplus_fields(body, columns))
        db.copy_csv(f'src_geo."{name}"', columns, buf.getvalue().encode(), header=False)
        loaded[name] = int(db.scalar(f'SELECT count(*) FROM src_geo."{name}";'))
    return loaded


def load_layers(db: Database, directory: Path) -> dict[str, int]:
    db.execute("DROP TABLE IF EXISTS src_geo.layer_feature;")
    db.execute("DROP TABLE IF EXISTS src_geo.layer;")
    db.execute(LAYER_DDL)
    db.execute(LAYER_FEATURE_DDL)

    loaded: dict[str, int] = {}
    for path in sorted(directory.glob("*.geojson")):
        layer = path.stem
        doc = json.loads(path.read_text())
        features = doc["features"]
        provenance = doc.get("provenance")
        db.execute(
            "INSERT INTO src_geo.layer (layer, feature_count, provenance) VALUES (%s, %s, %s);",
            [
                layer,
                len(features),
                json.dumps(provenance, ensure_ascii=False) if provenance else None,
            ],
        )
        lines = feature_lines(layer, features)
        db.copy_text(
            "src_geo.layer_feature",
            ["layer", "lb_code", "ward_code", "properties", "geometry"],
            ("\n".join(lines) + "\n").encode(),
        )
        loaded[layer] = len(features)

    db.execute("CREATE INDEX ON src_geo.layer_feature (lb_code);")
    db.execute("CREATE INDEX ON src_geo.layer_feature (layer);")
    return loaded


def load(db: Database, paths: Paths) -> dict[str, int]:
    """Load the reference crosswalks and every emitted layer."""
    db.execute("CREATE SCHEMA IF NOT EXISTS src_geo;")
    loaded = load_reference_csvs(db, paths.geo_reference)
    loaded.update(load_layers(db, paths.geo_layers))
    return loaded
