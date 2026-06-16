"""
TinyAnim — ORM models & persistence helpers
===========================================
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import secrets

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship

from .plans import PLAN_FREE, get_plan, period_expired


class Base(DeclarativeBase):
    pass


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


# --------------------------------------------------------------------------- #
# User / auth
# --------------------------------------------------------------------------- #
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)

    plan: Mapped[str] = mapped_column(String(16), default=PLAN_FREE, nullable=False)

    # Stripe linkage
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64))
    subscription_status: Mapped[str | None] = mapped_column(String(32))

    # Rolling usage window
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    usage_period_start: Mapped[_dt.datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="user")

    # -- usage helpers --------------------------------------------------- #
    def reset_usage_if_needed(self, now: _dt.datetime | None = None) -> None:
        now = now or _utcnow()
        if period_expired(self.usage_period_start, now):
            self.usage_count = 0
            self.usage_period_start = now

    @property
    def plan_obj(self):  # noqa: ANN201
        return get_plan(self.plan)


class ApiKey(Base):
    """A hashed API key for Pro programmatic access. Plaintext is shown once."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="api_keys")


class Optimization(Base):
    __tablename__ = "optimizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    file_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    original_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    optimized_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    saved_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Counter(Base):
    __tablename__ = "counters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    total_files: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_original_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_saved_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


# --------------------------------------------------------------------------- #
# Helpers — users
# --------------------------------------------------------------------------- #
def get_or_create_user(session: Session, email: str) -> User:
    email = email.strip().lower()
    user = session.scalar(select(User).where(User.email == email))
    if user is None:
        user = User(email=email, plan=PLAN_FREE)
        session.add(user)
        session.commit()
        session.refresh(user)
    return user


# --------------------------------------------------------------------------- #
# Helpers — API keys
# --------------------------------------------------------------------------- #
def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_api_key(session: Session, user: User) -> str:
    """Create and persist a new API key, returning the *plaintext* once."""
    raw = "ta_" + secrets.token_urlsafe(32)
    key = ApiKey(user_id=user.id, key_hash=_hash_key(raw), prefix=raw[:10])
    session.add(key)
    session.commit()
    return raw


def user_for_api_key(session: Session, raw: str) -> User | None:
    key = session.scalar(select(ApiKey).where(ApiKey.key_hash == _hash_key(raw)))
    if key is None:
        return None
    return session.get(User, key.user_id)


# --------------------------------------------------------------------------- #
# Helpers — stats / recording
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
    user: User | None = None,
) -> Optimization:
    saved = max(original_size - optimized_size, 0)

    row = Optimization(
        user_id=user.id if user else None,
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

    if user is not None:
        user.reset_usage_if_needed()
        user.usage_count += 1

    session.commit()
    session.refresh(row)
    return row


def get_stats(session: Session) -> dict[str, int | float]:
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
