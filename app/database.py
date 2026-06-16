"""
TinyAnim — Database layer (SQLite via SQLAlchemy)
=================================================

A single, lightweight SQLite file backs the application. It stores a running
log of every optimization plus a denormalized global counter so the landing
page can render lifetime savings without scanning the whole table.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

# Allow overriding the location (e.g. for tests) via env var.
_DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "..", "tinyanim.db")
DATABASE_URL = os.environ.get(
    "TINYANIM_DATABASE_URL", f"sqlite:///{os.path.abspath(_DEFAULT_PATH)}"
)

# check_same_thread=False is required because FastAPI may touch a session from
# a threadpool worker different from the one that created the engine.
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create tables on first boot. Idempotent."""
    from . import models  # noqa: F401  (ensure models are imported/registered)

    models.Base.metadata.create_all(bind=engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional scope around a series of operations."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
