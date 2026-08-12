"""Loading ``data/final`` and ``data/reference`` into the ``src_*`` schemas.

Everything here lands as text or jsonb, verbatim, and is never edited again.
Typing belongs in the modelled layer: a source table that refuses a row because
a vote count is blank has lost data the CSV was willing to carry.
"""

from __future__ import annotations
