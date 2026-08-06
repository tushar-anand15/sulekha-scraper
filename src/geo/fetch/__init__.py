"""The online half: acquires bytes and caches them, and does nothing else.

Everything here is allowed to touch the network. Nothing here interprets geometry --
that belongs to ``geo.build``, which must be able to run offline.
"""

from __future__ import annotations
