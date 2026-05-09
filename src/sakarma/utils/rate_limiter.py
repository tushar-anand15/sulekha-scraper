"""Global rate limiter using Redis for distributed concurrency control.

Mirrors the Redis-backed semaphore pattern from ``src/sulekha/utils/rate_limiter.py``
but namespaces the keys under ``sakarma:`` so the two tenants share one Redis
instance without interfering with each other's slots. They hit different
domains and may want different concurrency tuning.
"""

import time
import uuid
from contextlib import contextmanager
from typing import Optional

import redis
import structlog

logger = structlog.get_logger(__name__)


class GlobalRateLimiter:
    """Redis-based distributed semaphore for SAKARMA rate limiting.

    Uses a Redis list as a semaphore with N tokens. Workers acquire a token
    (BLPOP) before making a request and release it (RPUSH) when done.

    Example:
        limiter = GlobalRateLimiter(redis_url="redis://localhost:6379/0", max_concurrent=4)

        with limiter.acquire():
            # Only N requests can be here concurrently across all SAKARMA workers
            response = requests.get(url)
    """

    def __init__(
        self,
        redis_url: str,
        max_concurrent: int = 4,
        acquire_timeout: int = 300,
        token_ttl: int = 600,
        namespace: str = "sakarma",
    ):
        """Initialize the SAKARMA rate limiter.

        Args:
            redis_url: Redis connection URL
            max_concurrent: Maximum concurrent requests allowed globally for SAKARMA
            acquire_timeout: Max seconds to wait for a slot (0 = block forever)
            token_ttl: TTL for held tokens (auto-release if worker crashes)
            namespace: Redis key prefix; defaults to "sakarma" so we don't collide
                with sulekha's semaphore.
        """
        self.redis = redis.from_url(redis_url)
        self.max_concurrent = max_concurrent
        self.acquire_timeout = acquire_timeout
        self.token_ttl = token_ttl

        self.semaphore_key = f"{namespace}:rate_limit:semaphore"
        self.held_tokens_key = f"{namespace}:rate_limit:held"
        self.init_lock_key = f"{namespace}:rate_limit:init_lock"

        self._ensure_initialized()

    def _ensure_initialized(self) -> None:
        """Initialize the semaphore if not already done."""
        lock_acquired = self.redis.set(self.init_lock_key, "1", nx=True, ex=60)

        if lock_acquired:
            current_len = self.redis.llen(self.semaphore_key)

            if current_len < self.max_concurrent:
                self.redis.delete(self.semaphore_key)
                tokens = [f"token:{i}" for i in range(self.max_concurrent)]
                if tokens:
                    self.redis.rpush(self.semaphore_key, *tokens)

                logger.info(
                    "Initialized SAKARMA rate limiter semaphore",
                    max_concurrent=self.max_concurrent,
                    semaphore_key=self.semaphore_key,
                )

            self.redis.delete(self.init_lock_key)

    def _cleanup_stale_tokens(self) -> None:
        """Clean up tokens held by crashed workers."""
        now = time.time()
        stale_threshold = now - self.token_ttl

        held = self.redis.hgetall(self.held_tokens_key)

        for token_id, timestamp in held.items():
            try:
                ts = float(timestamp)
                if ts < stale_threshold:
                    self.redis.hdel(self.held_tokens_key, token_id)
                    self.redis.rpush(
                        self.semaphore_key,
                        token_id.decode() if isinstance(token_id, bytes) else token_id,
                    )
                    logger.warning(
                        "Released stale SAKARMA rate-limiter token",
                        token_id=token_id,
                        held_for_seconds=now - ts,
                    )
            except (ValueError, TypeError):
                pass

    @contextmanager
    def acquire(self):
        """Acquire a slot from the semaphore."""
        if time.time() % 60 < 1:
            self._cleanup_stale_tokens()

        holder_id = f"holder:{uuid.uuid4().hex[:8]}"

        logger.debug("Waiting for SAKARMA rate-limit slot", holder_id=holder_id)

        result = self.redis.blpop(
            self.semaphore_key,
            timeout=self.acquire_timeout if self.acquire_timeout > 0 else 0,
        )

        if result is None:
            raise TimeoutError(
                f"Could not acquire SAKARMA rate-limit slot within {self.acquire_timeout}s"
            )

        token = result[1]
        token_str = token.decode() if isinstance(token, bytes) else token

        self.redis.hset(self.held_tokens_key, token_str, str(time.time()))

        logger.debug(
            "Acquired SAKARMA rate-limit slot",
            holder_id=holder_id,
            token=token_str,
        )

        try:
            yield
        finally:
            self.redis.hdel(self.held_tokens_key, token_str)
            self.redis.rpush(self.semaphore_key, token_str)
            logger.debug(
                "Released SAKARMA rate-limit slot",
                holder_id=holder_id,
                token=token_str,
            )

    def get_stats(self) -> dict:
        """Get current rate limiter statistics."""
        available = self.redis.llen(self.semaphore_key)
        held = self.redis.hlen(self.held_tokens_key)

        return {
            "max_concurrent": self.max_concurrent,
            "available_slots": available,
            "held_slots": held,
        }

    def reset(self) -> None:
        """Reset the semaphore (useful for testing or recovery)."""
        self.redis.delete(self.semaphore_key)
        self.redis.delete(self.held_tokens_key)
        self.redis.delete(self.init_lock_key)
        self._ensure_initialized()
        logger.info(
            "SAKARMA rate limiter reset",
            max_concurrent=self.max_concurrent,
        )


class NoOpRateLimiter:
    """No-op rate limiter for when rate limiting is disabled."""

    @contextmanager
    def acquire(self):
        yield

    def get_stats(self) -> dict:
        return {"enabled": False}

    def reset(self) -> None:
        pass


_rate_limiter: Optional[GlobalRateLimiter] = None


def get_rate_limiter() -> GlobalRateLimiter | NoOpRateLimiter:
    """Get the SAKARMA global rate limiter instance."""
    global _rate_limiter

    from sakarma.config import settings

    if not settings.rate_limit_enabled:
        return NoOpRateLimiter()

    if _rate_limiter is None:
        _rate_limiter = GlobalRateLimiter(
            redis_url=settings.redis_url,
            max_concurrent=settings.rate_limit_max_concurrent,
        )

    return _rate_limiter


def reset_rate_limiter() -> None:
    """Reset the cached rate limiter (used by tests)."""
    global _rate_limiter
    _rate_limiter = None
