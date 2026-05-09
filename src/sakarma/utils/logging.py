"""Logging configuration for SAKARMA.

Mirrors ``src/sulekha/utils/logging.py`` but binds ``service="sakarma"`` into
every log entry so the two tenants are distinguishable in shared log streams.
"""

import logging
import sys
from typing import Any

import structlog

from sakarma.config import settings


def setup_logging() -> None:
    """Configure structlog for SAKARMA."""
    json_output = settings.log_format == "json"

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if json_output:
        processors = shared_processors + [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
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

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.log_level),
    )

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("google").setLevel(logging.WARNING)
    logging.getLogger("celery").setLevel(logging.INFO)

    # Bind service identity into every log line so sulekha and sakarma logs
    # are distinguishable in a shared stream.
    structlog.contextvars.bind_contextvars(service="sakarma")


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a configured logger instance."""
    return structlog.get_logger(name)
