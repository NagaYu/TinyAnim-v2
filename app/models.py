"""
TinyAnim — ORM models
=====================
"""

from __future__ import annotations

import datetime as _dt

from sqlalchemy import BigInteger, DateTime, Integer, String, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class Base(DeclarativeBase):
    pass


class Optimization(Base):
    """One row per successfully optimized file."""

    __tablename__ = "optimizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    original_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    optimized_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    saved_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Counter(Base):
    """Singleton row holding lifetime aggregate stats (fast to read)."""

    __tablename__ = "counters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    total_files: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_original_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_saved_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


# --------------------------------------------------------------------------- #
# Persistence helpers
# --------------------------------------------------------------------------- #
def _get_or_create_counter(session: Session) -> Counter:
    counter = session.get(Counter, 1)
    if counter is None:
        counter = Counter(id=1, total_files=0, total_original_bytes=0, total_saved_bytes=0)
        session.add(counter)
        session.flush()
    return counter


def record_optimization(
    session: Session,
    *,
    file_kind: str,
    original_filename: str,
    original_size: int,
    optimized_size: int,
) -> Optimization:
    """Persist an optimization and update the lifetime aggregate counter."""
    saved = max(original_size - optimized_size, 0)

    row = Optimization(
        file_kind=file_kind,
        original_filename=original_filename,
        original_size=original_size,
        optimized_size=optimized_size,
        saved_bytes=saved,
    )
    session.add(row)

    counter = _get_or_create_counter(session)
    counter.total_files += 1
    counter.total_original_bytes += original_size
    counter.total_saved_bytes += saved

    session.commit()
    session.refresh(row)
    return row


def get_stats(session: Session) -> dict[str, int | float]:
    """Return lifetime aggregate statistics for the landing page."""
    counter = _get_or_create_counter(session)
    session.commit()

    avg_reduction = 0.0
    if counter.total_original_bytes > 0:
        avg_reduction = round(
            counter.total_saved_bytes / counter.total_original_bytes * 100, 1
        )

    return {
        "total_files": counter.total_files,
        "total_saved_bytes": counter.total_saved_bytes,
        "total_original_bytes": counter.total_original_bytes,
        "avg_reduction_percent": avg_reduction,
    }


def recent_optimizations(session: Session, limit: int = 10) -> list[Optimization]:
    stmt = select(Optimization).order_by(Optimization.created_at.desc()).limit(limit)
    return list(session.scalars(stmt).all())
