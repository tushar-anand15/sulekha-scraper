"""Database session management for Sulekha service.

Provides SQLAlchemy session factory and utilities for database operations.
"""

from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from sulekha.config import settings
from sulekha.db.models import Base

# Create engine with connection pooling
engine = create_engine(
    settings.database_url,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,  # Verify connections before using
    echo=settings.log_level == "DEBUG",
)

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    """Initialize the database by creating all tables.

    Note: In production, use Alembic migrations instead of this.
    """
    Base.metadata.create_all(bind=engine)


def drop_db() -> None:
    """Drop all tables. Use with caution!"""
    Base.metadata.drop_all(bind=engine)


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Get a database session with automatic cleanup.

    Usage:
        with get_session() as session:
            districts = session.query(District).all()

    Yields:
        SQLAlchemy Session instance
    """
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
    """Dependency for getting database sessions (e.g., for FastAPI).

    Yields:
        SQLAlchemy Session instance
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
