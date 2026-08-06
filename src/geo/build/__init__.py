"""The offline half: reads the cache, produces layers, never fetches.

No module reachable from here may import an HTTP client at any depth. That is not a
convention -- ``tests/geo/test_no_network.py`` walks the import graph and fails the
build if one appears.
"""

from __future__ import annotations
