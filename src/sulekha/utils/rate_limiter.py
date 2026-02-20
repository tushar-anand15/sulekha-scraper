"""Global rate limiter using Redis for distributed concurrency control.

This module provides a Redis-based semaphore that limits the total number of
concurrent HTTP requests across all Celery workers, preventing resource
exhaustion that can block SSH and other services.
"""

import time
import uuid
from contextlib import contextmanager
from typing import Optional

import redis
import structlog

logger = structlog.get_logger(__name__)


class GlobalRateLimiter:
    """Redis-based distributed semaphore for rate limiting.

    Uses a Redis list as a semaphore with N tokens. Workers acquire a token
    (BLPOP) before making a request and release it (RPUSH) when done.

    Example:
        limiter = GlobalRateLimiter(redis_url="redis://localhost:6379/0", max_concurrent=5)
        
        with limiter.acquire():
            # Only 5 requests can be here concurrently across all workers
            response = requests.get(url)
    """

    def __init__(
        self,
        redis_url: str,
        max_concurrent: int = 5,
        acquire_timeout: int = 300,
        token_ttl: int = 600,
    ):
        """Initialize the rate limiter.

        Args:
            redis_url: Redis connection URL
            max_concurrent: Maximum concurrent requests allowed globally
            acquire_timeout: Max seconds to wait for a slot (0 = block forever)
            token_ttl: TTL for held tokens (auto-release if worker crashes)
        """
        self.redis = redis.from_url(redis_url)
        self.max_concurrent = max_concurrent
        self.acquire_timeout = acquire_timeout
        self.token_ttl = token_ttl

        # Redis keys
        self.semaphore_key = "sulekha:rate_limit:semaphore"
        self.held_tokens_key = "sulekha:rate_limit:held"
        self.init_lock_key = "sulekha:rate_limit:init_lock"

        # Ensure semaphore is initialized
        self._ensure_initialized()

    def _ensure_initialized(self) -> None:
        """Initialize the semaphore if not already done.

        Uses a distributed lock to ensure only one worker initializes.
        """
        # Try to acquire init lock (NX = only if not exists)
        lock_acquired = self.redis.set(
            self.init_lock_key, "1", nx=True, ex=60
        )

        if lock_acquired:
            # We got the lock, check if semaphore needs initialization
            current_len = self.redis.llen(self.semaphore_key)
            
            if current_len < self.max_concurrent:
                # Clear and reinitialize
                self.redis.delete(self.semaphore_key)
                
                # Add N tokens to the semaphore
                tokens = [f"token:{i}" for i in range(self.max_concurrent)]
                if tokens:
                    self.redis.rpush(self.semaphore_key, *tokens)
                
                logger.info(
                    "Initialized rate limiter semaphore",
                    max_concurrent=self.max_concurrent,
                )

            # Release init lock
            self.redis.delete(self.init_lock_key)

    def _cleanup_stale_tokens(self) -> None:
        """Clean up tokens held by crashed workers.

        Checks the held tokens set and releases any that have expired.
        """
        now = time.time()
        stale_threshold = now - self.token_ttl

        # Get all held tokens with their timestamps
        held = self.redis.hgetall(self.held_tokens_key)
        
        for token_id, timestamp in held.items():
            try:
                ts = float(timestamp)
                if ts < stale_threshold:
                    # Token is stale, release it
                    self.redis.hdel(self.held_tokens_key, token_id)
                    self.redis.rpush(self.semaphore_key, token_id.decode() if isinstance(token_id, bytes) else token_id)
                    logger.warning(
                        "Released stale token",
                        token_id=token_id,
                        held_for_seconds=now - ts,
                    )
            except (ValueError, TypeError):
                pass

    @contextmanager
    def acquire(self):
        """Acquire a slot from the semaphore.

        Blocks until a slot is available or timeout is reached.

        Yields:
            None

        Raises:
            TimeoutError: If acquire_timeout is reached without getting a slot
        """
        # Periodically clean up stale tokens
        if time.time() % 60 < 1:  # ~once per minute per worker
            self._cleanup_stale_tokens()

        # Generate unique ID for this acquisition
        holder_id = f"holder:{uuid.uuid4().hex[:8]}"

        # Block until we get a token
        logger.debug("Waiting for rate limit slot", holder_id=holder_id)
        
        result = self.redis.blpop(
            self.semaphore_key,
            timeout=self.acquire_timeout if self.acquire_timeout > 0 else 0,
        )

        if result is None:
            raise TimeoutError(
                f"Could not acquire rate limit slot within {self.acquire_timeout}s"
            )

        token = result[1]
        token_str = token.decode() if isinstance(token, bytes) else token

        # Track that we're holding this token
        self.redis.hset(self.held_tokens_key, token_str, str(time.time()))

        logger.debug(
            "Acquired rate limit slot",
            holder_id=holder_id,
            token=token_str,
        )

        try:
            yield
        finally:
            # Release the token back to the semaphore
            self.redis.hdel(self.held_tokens_key, token_str)
            self.redis.rpush(self.semaphore_key, token_str)
            
            logger.debug(
                "Released rate limit slot",
                holder_id=holder_id,
                token=token_str,
            )

    def get_stats(self) -> dict:
        """Get current rate limiter statistics.

        Returns:
            Dictionary with available slots, held count, etc.
        """
        available = self.redis.llen(self.semaphore_key)
        held = self.redis.hlen(self.held_tokens_key)
        
        return {
            "max_concurrent": self.max_concurrent,
            "available_slots": available,
            "held_slots": held,
        }

    def reset(self) -> None:
        """Reset the semaphore (useful for testing or recovery).

        Clears all state and reinitializes with max_concurrent tokens.
        """
        self.redis.delete(self.semaphore_key)
        self.redis.delete(self.held_tokens_key)
        self.redis.delete(self.init_lock_key)
        self._ensure_initialized()
        logger.info("Rate limiter reset", max_concurrent=self.max_concurrent)


class NoOpRateLimiter:
    """No-op rate limiter for when rate limiting is disabled."""

    @contextmanager
    def acquire(self):
        yield

    def get_stats(self) -> dict:
        return {"enabled": False}

    def reset(self) -> None:
        pass


# Global rate limiter instance (lazily initialized)
_rate_limiter: Optional[GlobalRateLimiter] = None


def get_rate_limiter() -> GlobalRateLimiter | NoOpRateLimiter:
    """Get the global rate limiter instance.

    Returns:
        GlobalRateLimiter if enabled, NoOpRateLimiter if disabled
    """
    global _rate_limiter

    from sulekha.config import settings

    if not settings.rate_limit_enabled:
        return NoOpRateLimiter()

    if _rate_limiter is None:
        _rate_limiter = GlobalRateLimiter(
            redis_url=settings.redis_url,
            max_concurrent=settings.rate_limit_max_concurrent,
        )

    return _rate_limiter
