"""HTTP client for the KSMART tile server.

Everything here was learned by probing the live server during planning (see the
plan's "Source recon" section), and every branch below exists because a naive
client got the wrong answer at least once:

* **No ``Referer`` header, no tiles.** ``kmapdev.ksmart.live`` returns 403 for every
  request that does not carry ``Referer: https://wardmap.ksmart.live/`` -- the tile
  server trusts the SPA's origin, not a token. A future maintainer hitting 403 needs
  the header named in the error, not a stack trace to reverse-engineer.
* **Gzip is intermittent and sometimes silent.** Roughly 1 tile in 44 arrives gzipped,
  and some of those carry no ``Content-Encoding`` header at all -- so ``requests``
  never auto-decompresses them and the raw bytes still start with the ``1f8b`` magic.
  A client that trusts the header hands compressed bytes to the MVT decoder, which
  raises, and if that raise is read as "empty tile" the tile silently vanishes. One
  such tile, found during planning, held 3,258 wards. So this module always sniffs
  the magic itself and never treats a decode failure as absence.
* **"Access denied" wears a 200.** A 13-byte ``Access denied`` body arrives with
  HTTP 200, indistinguishable from a real tile by status code alone. It must be
  rejected before it reaches the decoder, retried, and if it keeps happening past the
  retry budget the run has to stop rather than quietly record the tile as empty --
  emptiness may only ever come from an explicit 204.
* **This is a public government server, not a load-test target.** A full statewide
  descent is a five-figure request count, so the client enforces its own rate limit
  rather than trusting the caller to remember one.
"""

from __future__ import annotations

import gzip
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final

import requests
import structlog
from mapbox_vector_tile import decode as _decode_mvt
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

logger = structlog.get_logger(__name__)

#: Confirmed against the live server during planning: no file extension, no query
#: string, the layer/z/x/y path segments only.
BASE_URL: Final = "https://kmapdev.ksmart.live/tiles"

#: Without this exact value the server answers every request with 403.
REFERER: Final = "https://wardmap.ksmart.live/"

#: The gzip stream header. Sniffed on the raw body regardless of what (if anything)
#: ``Content-Encoding`` claims -- see the module docstring.
GZIP_MAGIC: Final = b"\x1f\x8b"

#: The exact body of a denied request, observed with HTTP 200 during planning.
ACCESS_DENIED_BODY: Final = b"Access denied"


class KsmartError(RuntimeError):
    """Base for every error this client raises."""


class RefererRequiredError(KsmartError):
    """A 403 -- almost certainly a missing or wrong ``Referer`` header."""

    def __init__(self, url: str) -> None:
        super().__init__(
            f"{url} returned 403. The KSMART tile server requires a "
            f"'Referer: {REFERER}' header and either did not receive one or received "
            "one it did not recognise."
        )


class AccessDeniedError(KsmartError):
    """The 13-byte 'Access denied' body persisted past the retry budget.

    Never a reason to record the tile as empty -- emptiness comes only from a 204.
    """


class TileServerError(KsmartError):
    """A 5xx response persisted past the retry budget."""


class TileDecodeError(KsmartError):
    """A tile body was neither gzip nor a decodable MVT tile.

    This must never be swallowed as "empty" -- an empty tile is a 204 with a
    zero-length body, nothing else. A decode failure means the bytes are bad (or the
    format changed) and the run should stop and say so.
    """


class UnexpectedStatusError(KsmartError):
    """Any HTTP status this client does not know how to classify."""


class TileStatus(Enum):
    """What a tile request resolved to, once retries are exhausted."""

    TILE = "tile"  #: 200 with a real body -- gzipped or not.
    EMPTY = "empty"  #: 204 with a zero-length body. The only legitimate emptiness.
    ABSENT = "absent"  #: 404 -- outside the server's z8-z16 range.


@dataclass(frozen=True, slots=True)
class TileResponse:
    """The outcome of one tile request.

    ``body`` carries the bytes exactly as received -- gzipped or not. Decompression
    is left to the reader (:func:`decode_tile_body`, or ``geo.build.stitch`` later);
    this client's job is to classify the response and hand back what the server sent.
    """

    status: TileStatus
    body: bytes = b""


class _TransientResponseError(Exception):
    """Raised internally to trigger a tenacity retry. Never escapes the client."""

    def __init__(self, kind: str, detail: str) -> None:
        self.kind = kind
        super().__init__(detail)


def decode_tile_body(raw: bytes) -> dict[str, Any]:
    """Decode a tile body into its MVT layers, decompressing first if needed.

    Sniffs the ``1f8b`` magic on ``raw`` itself rather than trusting a
    ``Content-Encoding`` header -- see the module docstring for why that distinction
    is load-bearing here. Raises :class:`TileDecodeError` for anything that is
    neither gzip nor a valid tile, so a corrupt or reshaped response fails loudly
    instead of looking like an empty tile.
    """
    body = raw
    if body[:2] == GZIP_MAGIC:
        try:
            body = gzip.decompress(body)
        except OSError as exc:
            raise TileDecodeError(
                f"body starts with the gzip magic but will not decompress: {exc}"
            ) from exc
    try:
        return _decode_mvt(body)
    except Exception as exc:  # noqa: BLE001 -- any decode failure is a hard error here
        raise TileDecodeError(f"body is neither gzip nor a valid MVT tile: {exc}") from exc


@dataclass
class _RateLimiter:
    """A minimum interval between requests, shared across every calling thread."""

    min_interval: float = 0.1
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _last: float = field(default=0.0, repr=False, compare=False)

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._last + self.min_interval - now
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


class KsmartClient:
    """A ``requests.Session`` wrapper that knows this one server's failure modes.

    Bounded concurrency is the caller's concern (``geo.fetch.ksmart`` runs several
    of these calls at once from a thread pool); this class supplies the piece that
    concurrency alone cannot: a shared rate limit and per-request retry budget.
    """

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        rate_limit: float = 0.1,
        max_attempts: int = 4,
        retry_wait: float = 1.0,
        timeout: float = 15.0,
    ) -> None:
        self.session = session or requests.Session()
        self.session.headers["Referer"] = REFERER
        self._rate_limiter = _RateLimiter(rate_limit)
        self._max_attempts = max_attempts
        self._retry_wait = retry_wait
        self._timeout = timeout

    def fetch_tile(self, layer: str, z: int, x: int, y: int) -> TileResponse:
        """Fetch and classify one tile, retrying transient failures.

        Returns a terminal :class:`TileResponse` (``TILE``, ``EMPTY`` or ``ABSENT``),
        or raises once the retry budget for a transient failure is spent. Never
        returns a response built from an ``Access denied`` body or an undecodable one
        -- those are errors, not tiles.
        """
        url = f"{BASE_URL}/{layer}/{z}/{x}/{y}"

        @retry(
            retry=retry_if_exception_type((_TransientResponseError, requests.RequestException)),
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_fixed(self._retry_wait),
            reraise=True,
        )
        def _attempt() -> TileResponse:
            self._rate_limiter.wait()
            resp = self.session.get(url, timeout=self._timeout)
            return self._classify(url, resp)

        try:
            return _attempt()
        except _TransientResponseError as exc:
            if exc.kind == "access_denied":
                raise AccessDeniedError(
                    f"{url}: 'Access denied' persisted past the retry budget "
                    f"({self._max_attempts} attempts). Refusing to record this as an "
                    "empty tile -- failing the run instead."
                ) from exc
            raise TileServerError(
                f"{url}: server errors persisted past the retry budget "
                f"({self._max_attempts} attempts)."
            ) from exc

    def _classify(self, url: str, resp: requests.Response) -> TileResponse:
        if resp.status_code == 403:
            raise RefererRequiredError(url)
        if resp.status_code == 404:
            return TileResponse(TileStatus.ABSENT)
        if resp.status_code >= 500:
            raise _TransientResponseError("server_error", f"{url}: HTTP {resp.status_code}")
        if resp.status_code not in (200, 204):
            raise UnexpectedStatusError(f"{url}: unexpected HTTP {resp.status_code}")

        body = resp.content
        if resp.status_code == 204 or not body:
            return TileResponse(TileStatus.EMPTY)
        if body == ACCESS_DENIED_BODY:
            raise _TransientResponseError("access_denied", f"{url}: 'Access denied' body")

        # Validate before accepting. A tile that fails to decode must raise here,
        # not get cached and discovered broken later -- see TileDecodeError.
        decode_tile_body(body)
        return TileResponse(TileStatus.TILE, body)
