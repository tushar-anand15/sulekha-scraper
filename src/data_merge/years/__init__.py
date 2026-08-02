"""Per-year builders.

Each module here wires one cycle's :class:`~data_merge.spec.YearSpec` to the
shared assembly it needs. ``base.py`` holds the SEC-spine assembly that 2015,
2020 and (eventually) 2025 share; a PDF-spine year such as 2010 does not use
it, because there is no SEC-spine assembly to share.
"""

from __future__ import annotations
