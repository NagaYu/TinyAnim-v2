"""
TinyAnim — passwordless auth (magic links) + sessions
======================================================

Flow
----
1. User submits an email → we sign a short-lived token and email a link.
2. User clicks the link → token verified → a signed session cookie is set.
3. Subsequent requests carry the cookie → ``current_user`` resolves the User.

No passwords are ever stored. Tokens and the session cookie are both signed
with ``settings.SECRET_KEY`` via itsdangerous, so nothing is stored server-side
for the magic-link step (stateless, replay-bounded by the TTL).

Email delivery uses Resend when ``RESEND_API_KEY`` is configured; otherwise we
fall back to "dev mode" (link logged + returned in the response).
"""

from __future__ import annotations

import logging

import httpx
from email_validator import EmailNotValidError, validate_email
from fastapi import Depends, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from .config import settings
from .database import get_session
from .models import User, get_or_create_user

log = logging.getLogger("tinyanim.auth")

_MAGIC_SALT = "tinyanim-magic-link"
_SESSION_SALT = "tinyanim-session"

_serializer = URLSafeTimedSerializer(settings.SECRET_KEY)


# --------------------------------------------------------------------------- #
# Email normalization
# --------------------------------------------------------------------------- #
def normalize_email(raw: str) -> str:
    """Validate and normalize an email address, raising ValueError if invalid."""
    try:
        result = validate_email(raw, check_deliverability=False)
    except EmailNotValidError as exc:
        raise ValueError(str(exc)) from exc
    return result.normalized.lower()


# --------------------------------------------------------------------------- #
# Magic link tokens
# --------------------------------------------------------------------------- #
def make_magic_token(email: str) -> str:
    return _serializer.dumps(email, salt=_MAGIC_SALT)


def verify_magic_token(token: str) -> str | None:
    try:
        return _serializer.loads(
            token, salt=_MAGIC_SALT, max_age=settings.MAGIC_LINK_TTL_SECONDS
        )
    except (BadSignature, SignatureExpired):
        return None


def magic_link_url(token: str) -> str:
    return f"{settings.BASE_URL}/auth/verify?token={token}"


# --------------------------------------------------------------------------- #
# Session cookie
# --------------------------------------------------------------------------- #
def make_session_token(user: User) -> str:
    return _serializer.dumps({"uid": user.id, "email": user.email}, salt=_SESSION_SALT)


def read_session_token(token: str) -> dict | None:
    try:
        return _serializer.loads(
            token, salt=_SESSION_SALT, max_age=settings.SESSION_TTL_SECONDS
        )
    except (BadSignature, SignatureExpired):
        return None


# --------------------------------------------------------------------------- #
# Email delivery
# --------------------------------------------------------------------------- #
def send_magic_link(email: str, url: str) -> bool:
    """Send the magic link. Returns True if actually emailed, False in dev mode."""
    if not settings.email_enabled:
        log.warning("DEV EMAIL — magic link for %s: %s", email, url)
        return False

    html = f"""
      <div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;max-width:480px">
        <h2 style="margin:0 0 12px">Sign in to TinyAnim</h2>
        <p style="color:#555">Click the button below to sign in. This link expires in
        {settings.MAGIC_LINK_TTL_SECONDS // 60} minutes.</p>
        <p><a href="{url}" style="display:inline-block;background:#6d5efc;color:#fff;
        padding:12px 22px;border-radius:10px;text-decoration:none;font-weight:600">
        Sign in</a></p>
        <p style="color:#999;font-size:12px">If you didn't request this, ignore this email.</p>
      </div>
    """
    try:
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
            json={
                "from": settings.EMAIL_FROM,
                "to": [email],
                "subject": "Your TinyAnim sign-in link",
                "html": html,
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        log.error("Failed to send magic link to %s: %s", email, exc)
        # Surface as dev-mode fallback rather than hard-failing the request.
        log.warning("FALLBACK — magic link for %s: %s", email, url)
        return False


# --------------------------------------------------------------------------- #
# FastAPI dependencies
# --------------------------------------------------------------------------- #
def current_user(
    request: Request, session: Session = Depends(get_session)
) -> User | None:
    """Resolve the logged-in user from the session cookie, or None."""
    token = request.cookies.get(settings.SESSION_COOKIE)
    if not token:
        return None
    data = read_session_token(token)
    if not data:
        return None
    user = session.get(User, data.get("uid"))
    return user


def login_user(email: str, session: Session) -> User:
    """Resolve-or-create the user for a verified email."""
    return get_or_create_user(session, email)
