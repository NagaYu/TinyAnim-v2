"""
TinyAnim — central configuration
================================

All runtime configuration is sourced from environment variables so the same
image runs locally (SQLite, dev email) and in production (Postgres, Stripe live
keys, real email provider) with zero code changes.
"""

from __future__ import annotations

import os
import secrets


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    # -- security --------------------------------------------------------- #
    # A stable secret is required in production for cookie/token signing.
    # In dev we fall back to an ephemeral one (sessions reset on restart).
    SECRET_KEY: str = os.environ.get("TINYANIM_SECRET_KEY") or secrets.token_urlsafe(48)

    # -- environment ------------------------------------------------------ #
    ENV: str = os.environ.get("TINYANIM_ENV", "development")
    BASE_URL: str = os.environ.get("TINYANIM_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

    @property
    def is_production(self) -> bool:
        return self.ENV.lower() == "production"

    # -- uploads ---------------------------------------------------------- #
    CHUNK_SIZE: int = 64 * 1024
    DOWNLOAD_TTL_SECONDS: int = 600
    MAX_PENDING_DOWNLOADS: int = 512

    # -- auth ------------------------------------------------------------- #
    MAGIC_LINK_TTL_SECONDS: int = int(os.environ.get("TINYANIM_MAGIC_LINK_TTL", 900))  # 15 min
    SESSION_TTL_SECONDS: int = int(os.environ.get("TINYANIM_SESSION_TTL", 60 * 60 * 24 * 30))  # 30 d
    SESSION_COOKIE: str = "tinyanim_session"
    COOKIE_SECURE: bool = _bool("TINYANIM_COOKIE_SECURE", default=False)

    # -- email (magic links) --------------------------------------------- #
    # If RESEND_API_KEY is unset we run in "dev email" mode: the magic link is
    # written to the server log and surfaced in the API response instead of
    # being emailed. Set this (plus EMAIL_FROM) for real delivery.
    RESEND_API_KEY: str | None = os.environ.get("RESEND_API_KEY")
    EMAIL_FROM: str = os.environ.get("TINYANIM_EMAIL_FROM", "TinyAnim <onboarding@resend.dev>")

    @property
    def email_enabled(self) -> bool:
        return bool(self.RESEND_API_KEY)

    # -- billing (Stripe) ------------------------------------------------- #
    STRIPE_SECRET_KEY: str | None = os.environ.get("STRIPE_SECRET_KEY")
    STRIPE_PUBLISHABLE_KEY: str | None = os.environ.get("STRIPE_PUBLISHABLE_KEY")
    STRIPE_PRICE_ID: str | None = os.environ.get("STRIPE_PRICE_ID")
    STRIPE_WEBHOOK_SECRET: str | None = os.environ.get("STRIPE_WEBHOOK_SECRET")

    @property
    def billing_enabled(self) -> bool:
        return bool(self.STRIPE_SECRET_KEY and self.STRIPE_PRICE_ID)


settings = Settings()
