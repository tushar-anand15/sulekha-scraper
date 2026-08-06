"""Fetch and cache the opendatakerala ``lsg-kerala-data`` release GeoJSON.

This is the network half of Unit 7 (see the plan's "Source recon" section). The
release is a single ~6.1 MB file -- 1,034 ``admin_level=8`` polygons covering
every Grama Panchayat, Municipality and Corporation, snapshotted from
OpenStreetMap in November 2020. It is not a delimitation dataset and carries no
ward boundaries; :mod:`geo.build.dissolve` is what turns it into Block and
District Panchayats.

Licence: the data is OpenStreetMap content redistributed by opendatakerala
under the Open Database License (ODbL). Anything built from it downstream
inherits ODbL's attribution and share-alike obligations -- see
``docs/geo_runbook.md`` for how that gets discharged in emitted layers. This
module does not concern itself with that; it only fetches and caches the file.

Cache design -- record once, verify forever: the first successful download
writes both the payload and a ``.sha256`` sidecar next to it. Every later call
recomputes the file's hash and compares it to that sidecar rather than trusting
the file's mere existence. Without this, a partially-written file from a killed
process, or bytes flipped by a flaky disk, would sit in the cache looking like
a normal input -- and a build reading it would not fail, it would just silently
work on wrong data. A mismatch here is treated as loud and fatal rather than as
grounds to quietly re-fetch, because a fetch that succeeds is indistinguishable
from a cache hit and would defeat the whole point of caching (see
``geo.config``'s module docstring on the same trade-off for tiles).

An optional ``expected_sha256`` lets a caller pin the release to a known-good
digest -- useful once someone has eyeballed a specific snapshot -- and is
checked both against a fresh download and against whatever is already cached.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

import requests

from geo.config import Paths

#: The GeoJSON export in the repo's data/ tree. Shapefile and KML siblings exist
#: at the same path with different extensions but are not used here.
RELEASE_URL: Final = (
    "https://raw.githubusercontent.com/opendatakerala/lsg-kerala-data/"
    "main/data/kerala_lsg_data.geojson"
)

FILENAME: Final = "kerala_lsg_data.geojson"

#: Read/write in chunks so hashing a 6 MB file (or a much larger future one)
#: never requires holding two copies of it in memory at once.
_CHUNK_SIZE: Final = 1 << 20


class ChecksumMismatchError(RuntimeError):
    """A file's SHA-256 does not match what was recorded or expected.

    Raised instead of logging-and-continuing: a checksum only earns its keep if
    disagreement stops the run. Callers that want to recover (e.g. by deleting
    the cache and re-fetching) can catch this explicitly.
    """


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_release(
    paths: Paths,
    *,
    session: requests.Session | None = None,
    expected_sha256: str | None = None,
    force: bool = False,
    timeout: float = 60.0,
) -> Path:
    """Return the path to the cached release GeoJSON, downloading it if needed.

    On a cache hit (the file and its ``.sha256`` sidecar both exist and
    ``force`` is not set), the file is re-hashed and compared to the sidecar --
    a mismatch raises :class:`ChecksumMismatchError` rather than silently
    re-downloading, since that would hide the corruption it was meant to catch.

    On a cache miss, the file is downloaded, hashed, and only then written to
    disk together with its sidecar -- so a checksum failure at download time
    (caught before the write) never leaves a half-trusted file in the cache.

    If ``expected_sha256`` is given, it is checked against the file's hash in
    both cases -- a pin any caller can use once a specific snapshot has been
    reviewed.
    """
    dest = paths.releases / FILENAME
    checksum_path = dest.with_name(dest.name + ".sha256")

    if dest.exists() and checksum_path.exists() and not force:
        recorded = checksum_path.read_text(encoding="utf-8").strip()
        actual = sha256_file(dest)
        if actual != recorded:
            raise ChecksumMismatchError(
                f"{dest} has drifted from its recorded checksum "
                f"(recorded {recorded}, actual {actual}) -- the cache is likely "
                "corrupt; delete it and re-fetch rather than trust it."
            )
        if expected_sha256 is not None and actual != expected_sha256:
            raise ChecksumMismatchError(
                f"{dest}: cached checksum {actual} does not match the expected "
                f"{expected_sha256}"
            )
        return dest

    http = session or requests.Session()
    response = http.get(RELEASE_URL, timeout=timeout)
    response.raise_for_status()
    content = response.content
    actual = sha256_bytes(content)
    if expected_sha256 is not None and actual != expected_sha256:
        raise ChecksumMismatchError(
            f"downloaded {RELEASE_URL}: checksum {actual} does not match the "
            f"expected {expected_sha256} -- refusing to cache it. A silently "
            "cached wrong file is indistinguishable from a correct one until "
            "something downstream breaks on it."
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    checksum_path.write_text(actual + "\n", encoding="utf-8")
    return dest
