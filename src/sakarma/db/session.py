"""Database session management for the SAKARMA scraper.

The SAKARMA engine is intentionally a separate instance from sulekha's, even
when both DSNs point at the same Postgres cluster, so connection pools,
session bindings, and metadata stay cleanly partitioned per tenant.
"""

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from sakarma.config import settings
from sakarma.db.models import SakarmaBase

# Create a SAKARMA-dedicated engine. Even when settings.database_url is
# identical to sulekha's, we want a distinct engine/pool here.
engine = create_engine(
    settings.database_url,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    echo=settings.log_level == "DEBUG",
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    """Create the ``sakarma`` schema and all tables (dev/test convenience)."""
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS sakarma"))
    SakarmaBase.metadata.create_all(bind=engine)


def drop_db() -> None:
    """Drop all SAKARMA tables (dev/test convenience). Use with caution."""
    SakarmaBase.metadata.drop_all(bind=engine)


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Yield a SAKARMA DB session that auto-commits on success, rolls back on error."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session_dependency() -> Generator[Session, None, None]:
    """FastAPI/Celery-style dependency yielding a SAKARMA session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
