"""Boundary geometry for the Kerala LSG election dataset.

Two halves, deliberately separated:

``geo.fetch``   may make network requests, and does nothing else -- it writes bytes
                to the cache under ``data/raw/geo`` and stops.
``geo.build``   may not make network requests, ever. It reads that cache plus
                ``data_merge``'s CSV outputs and produces the emitted layers.

``tests/geo/test_no_network.py`` enforces the second half by walking the import graph,
because a build that quietly re-fetched would look exactly like a build that hit the
cache -- right up until the upstream server changed and yesterday's output stopped
being reproducible.
"""

from __future__ import annotations

__version__ = "0.1.0"
