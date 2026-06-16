"""
TinyAnim — Database layer
=========================

Local development uses SQLite; production (Render) provides a ``DATABASE_URL``
pointing at Postgres. We normalize the legacy ``postgres://`` scheme that some
platforms still emit to the SQLAlchemy-friendly ``postgresql://`` form.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

_DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "..", "tinyanim.db")
_RAW_URL = os.environ.get("DATABASE_URL") or os.environ.get(
    "TINYANIM_DATABASE_URL", f"sqlite:///{os.path.abspath(_DEFAULT_PATH)}"
)

# Heroku/Render legacy scheme fix.
if _RAW_URL.startswith("postgres://"):
    _RAW_URL = _RAW_URL.replace("postgres://", "postgresql://", 1)

DATABASE_URL = _RAW_URL
_IS_SQLITE = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if _IS_SQLITE else {},
    pool_pre_ping=not _IS_SQLITE,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create tables on first boot. Idempotent."""
    from . import models  # noqa: F401

    models.Base.metadata.create_all(bind=engine)


@contextmanager
def session_scope() -> Iterator[Session]:
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
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
