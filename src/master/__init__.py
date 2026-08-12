"""The master database build: one Postgres keyed on a single local body id.

Four systems identify a Kerala local body four incompatible ways -- elections by
``lb_code``, Sakarma by an internal integer, Sulekha by a (district, body) index
that is re-issued every financial year, and the geometry by KSMART and OSM codes.
Nothing joins until something reconciles them, so this package's real product is
``core.local_body`` and the two crosswalk tables hanging off it; the rest is
loading and rollups.

Three halves, in the order the build runs them:

``master.load``       reads ``data/final`` and ``data/reference`` into the ``src_*``
                      schemas, verbatim and untyped. A source table that refuses a
                      row because a vote count is blank has lost data the CSV was
                      willing to carry.
``master.crosswalk``  reconciles the four, recording for every row which rule
                      produced it so the weak matches stay visible.
``master.schema``     the derived schema and rollups, in ``schema.sql``.

Everything outside ``src_*`` can be dropped and rebuilt; nothing in ``src_*`` is
ever edited.
"""

from __future__ import annotations

__version__ = "0.1.0"
