"""Logging configuration for Sulekha service.

Provides structured logging with JSON output for production
and colored console output for development.
"""

import logging
import sys
from typing import Any

import structlog

from sulekha.config import settings


def setup_logging() -> None:
    """Configure structlog for the application.

    Sets up structured logging with either JSON or console output
    based on the LOG_FORMAT setting.
    """
    # Determine if we want JSON or console output
    json_output = settings.log_format == "json"

    # Shared processors for all outputs
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if json_output:
        # JSON output for production
        processors = shared_processors + [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        # Colored console output for development
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(colors=True),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Configure standard library logging
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.log_level),
    )

    # Set log levels for noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("google").setLevel(logging.WARNING)
    logging.getLogger("celery").setLevel(logging.INFO)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a configured logger instance.

    Args:
        name: Logger name (usually __name__)

    Returns:
        Configured structlog logger
    """
    return structlog.get_logger(name)


class RequestLogger:
    """Context manager for logging HTTP requests with timing."""

    def __init__(self, method: str, url: str, **context: Any):
        self.method = method
        self.url = url
        self.context = context
        self.logger = structlog.get_logger(__name__)
        self.start_time: float = 0

    def __enter__(self) -> "RequestLogger":
        import time

        self.start_time = time.time()
        self.logger.debug(
            "HTTP request starting",
            method=self.method,
            url=self.url,
            **self.context,
        )
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        import time

        duration_ms = (time.time() - self.start_time) * 1000

        if exc_type is not None:
            self.logger.error(
                "HTTP request failed",
                method=self.method,
                url=self.url,
                duration_ms=round(duration_ms, 2),
                error=str(exc_val),
                **self.context,
            )
        else:
            self.logger.debug(
                "HTTP request completed",
                method=self.method,
                url=self.url,
                duration_ms=round(duration_ms, 2),
                **self.context,
            )
