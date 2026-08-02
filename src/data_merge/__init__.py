"""Kerala local-body elections: offline reconstruction of four cycles.

Rebuilds ``candidates_<year>.csv`` for 2010, 2015, 2020 and 2025 from the raw
sources on disk -- SEC ajax caches, LSGD and WIYR member caches, and the SEC's
own candidate reports -- onto one 31-column schema. No code path in this
package makes a network call; the caches are the sources of record.

Layering, strictly one-directional::

    sources -> parsers -> transform -> years -> validate -> io

``transform`` is year-agnostic and does no I/O. ``years`` assembles and returns
new records; only ``io`` writes.
"""

from __future__ import annotations

__version__ = "0.1.0"
