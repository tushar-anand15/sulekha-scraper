"""Tests for sakarma.utils.rate_limiter."""

import pytest

pytest_plugins: list[str] = []


@pytest.mark.integration
def test_namespace_isolates_sakarma_from_sulekha(monkeypatch):
    """Sakarma's semaphore key is prefixed and does not collide with sulekha's."""
    monkeypatch.setenv("SAKARMA_REDIS_URL", "redis://localhost:6379/2")
    monkeypatch.setenv("SAKARMA_RATE_LIMIT_MAX_CONCURRENT", "2")

    from sakarma.utils.rate_limiter import GlobalRateLimiter, reset_rate_limiter

    reset_rate_limiter()
    limiter = GlobalRateLimiter(
        redis_url="redis://localhost:6379/2",
        max_concurrent=2,
        namespace="sakarma",
    )
    try:
        assert limiter.semaphore_key.startswith("sakarma:")
        assert limiter.semaphore_key != "sulekha:rate_limit:semaphore"

        stats = limiter.get_stats()
        assert stats["max_concurrent"] == 2
        assert stats["available_slots"] >= 0
    finally:
        limiter.reset()
        # Best-effort cleanup
        limiter.redis.delete(limiter.semaphore_key)
        limiter.redis.delete(limiter.held_tokens_key)


def test_no_op_rate_limiter_yields_immediately():
    """NoOpRateLimiter is a context manager that does nothing."""
    from sakarma.utils.rate_limiter import NoOpRateLimiter

    limiter = NoOpRateLimiter()
    with limiter.acquire():
        pass  # no exceptions, returns control immediately

    assert limiter.get_stats() == {"enabled": False}


def test_get_rate_limiter_returns_noop_when_disabled(monkeypatch):
    """When SAKARMA_RATE_LIMIT_ENABLED=false, get_rate_limiter returns NoOpRateLimiter."""
    monkeypatch.setenv("SAKARMA_RATE_LIMIT_ENABLED", "false")

    # Reset cached settings + limiter
    from sakarma import config as cfg

    cfg.get_settings.cache_clear()  # type: ignore[attr-defined]

    from sakarma.utils.rate_limiter import (
        NoOpRateLimiter,
        get_rate_limiter,
        reset_rate_limiter,
    )

    reset_rate_limiter()
    rl = get_rate_limiter()
    assert isinstance(rl, NoOpRateLimiter)
