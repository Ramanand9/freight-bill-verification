"""
database.py
===========
Owns the SQLAlchemy engine, the session factory, and the declarative Base.

WHY THIS EXISTS
---------------
Every other module needs database access, but none of them should know *how*
the connection is built. Isolating engine/session creation here means:
  * we configure SQLite-specific args (check_same_thread) in exactly one place,
  * swapping to Postgres later touches only config.database_url,
  * FastAPI routes get sessions through one dependency (`get_db`) that always
    closes them, even on error.

WHAT TO PAY ATTENTION TO
------------------------
`get_db()` is a generator dependency. FastAPI calls it per-request, yields a
session to the route, and runs the `finally` block to close it afterward.
That lifecycle (open -> use -> always close) is the thing to internalise.
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

# SQLite + a multithreaded server (uvicorn) needs check_same_thread=False.
# This branch keeps that quirk out of the Postgres path.
_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

engine = create_engine(settings.database_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    """Declarative base every ORM model inherits from."""


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yields a session and guarantees it is closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables. Safe to call repeatedly (no-op if they exist)."""
    # Import models for side effects so they register on Base.metadata.
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
