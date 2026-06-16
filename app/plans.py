"""
TinyAnim — plan definitions & gating logic
=========================================

The product gates on *usage count* (per rolling 30-day period) and *file size*.
Keep this module free of web/DB imports so the rules are easy to read and test.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

PLAN_FREE = "free"
PLAN_PRO = "pro"

_MB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Plan:
    key: str
    name: str
    monthly_limit: int | None  # None == unlimited
    max_upload_bytes: int
    allow_batch: bool
    allow_api: bool

    @property
    def max_upload_mb(self) -> int:
        return self.max_upload_bytes // _MB


PLANS: dict[str, Plan] = {
    PLAN_FREE: Plan(
        key=PLAN_FREE,
        name="Free",
        monthly_limit=20,
        max_upload_bytes=5 * _MB,
        allow_batch=False,
        allow_api=False,
    ),
    PLAN_PRO: Plan(
        key=PLAN_PRO,
        name="Pro",
        monthly_limit=None,
        max_upload_bytes=50 * _MB,
        allow_batch=True,
        allow_api=True,
    ),
}

PERIOD_DAYS = 30


def get_plan(key: str | None) -> Plan:
    return PLANS.get(key or PLAN_FREE, PLANS[PLAN_FREE])


def period_expired(period_start: _dt.datetime | None, now: _dt.datetime) -> bool:
    """True when the current usage window is older than ``PERIOD_DAYS``."""
    if period_start is None:
        return True
    # Normalize naive/aware mismatch by comparing on UTC timestamps.
    if period_start.tzinfo is None:
        period_start = period_start.replace(tzinfo=_dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)
    return (now - period_start) >= _dt.timedelta(days=PERIOD_DAYS)
