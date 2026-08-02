"""Year-agnostic domain logic: matching, party, gender, winner, rollup.

Every function here is pure -- records in, records or a verdict out. No file
access, no cache access, no ``YearSpec``, and no branching on ``year``. That is
what makes this the one layer testable with a handful of rows and no fixtures:
everything a test needs is an argument.

Where a caller must supply something year-specific (a front table, a member
name to break a tie), it comes in as a parameter. Nothing in this package may
hardcode a year's alliances, thresholds tuned to one cycle, or a per-year
constant standing in for a measurement -- see ``gender.py``'s orientation
measurement for the rule this is most tempting to break.
"""

from __future__ import annotations
