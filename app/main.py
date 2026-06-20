"""
TinyAnim — FastAPI application (SaaS edition)
=============================================

Public, subscription-gated compressor for Lottie & SVG files.

* Passwordless auth (magic links) — see ``auth.py``.
* Stripe subscriptions (Free / Pro) — see ``billing.py`` & ``plans.py``.
* Usage gating: Free = 20 files / 30 days, ≤5 MB; Pro = unlimited, ≤50 MB, API.
* Hardened uploads: extension allow-list, content sniff, streamed size cap,
  in-memory TTL download store (uploads never hit disk).
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from pathlib import PurePosixPath

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import auth, billing, models
from .config import settings
from .database import get_session, init_db
from .models import User
from .optimizer import optimize_file
from .plans import PLAN_PRO, get_plan

logging.basicConfig(level=logging.INFO)

ALLOWED_EXTENSIONS = {
    ".json": "lottie",
    ".svg": "svg",
    # Raster images — re-encoded (lossy) to the smallest modern codec.
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".webp": "image",
    ".heic": "image",
    ".heif": "image",
    ".avif": "image",
    ".bmp": "image",
    ".tiff": "image",
}

# Magic-byte prefixes for image content sniffing.
_IMAGE_SIGNATURES = (
    b"\xff\xd8\xff",          # JPEG
    b"\x89PNG\r\n\x1a\n",     # PNG
    b"BM",                    # BMP
    b"II*\x00", b"MM\x00*",  # TIFF
)

_IMAGE_MEDIA_TYPES = {
    "webp": "image/webp",
    "avif": "image/avif",
    "png": "image/png",
    "jpeg": "image/jpeg",
}

_BASE_DIR = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(_BASE_DIR, "templates"))

app = FastAPI(title="TinyAnim", description="Lossless Lottie & SVG compression", version="2.0.0")

_static_dir = os.path.join(_BASE_DIR, "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


# --------------------------------------------------------------------------- #
# Bounded TTL download store (in-memory)
# --------------------------------------------------------------------------- #
class _DownloadStore:
    def __init__(self) -> None:
        self._items: dict[str, tuple[bytes, str, str, float]] = {}

    def _evict_expired(self) -> None:
        now = time.monotonic()
        for k in [k for k, v in self._items.items() if v[3] < now]:
            self._items.pop(k, None)
        while len(self._items) > settings.MAX_PENDING_DOWNLOADS:
            oldest = min(self._items, key=lambda k: self._items[k][3])
            self._items.pop(oldest, None)

    def put(self, data: bytes, filename: str, media_type: str) -> str:
        self._evict_expired()
        token = secrets.token_urlsafe(24)
        self._items[token] = (data, filename, media_type, time.monotonic() + settings.DOWNLOAD_TTL_SECONDS)
        return token

    def get(self, token: str) -> tuple[bytes, str, str] | None:
        self._evict_expired()
        item = self._items.get(token)
        return None if item is None else (item[0], item[1], item[2])


downloads = _DownloadStore()


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def _startup() -> None:
    init_db()


# --------------------------------------------------------------------------- #
# Upload helpers
# --------------------------------------------------------------------------- #
def _safe_extension(filename: str) -> str:
    name = PurePosixPath(filename or "").name
    ext = PurePosixPath(name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Only .json (Lottie) and .svg are accepted.",
        )
    return ext


async def _read_capped(upload: UploadFile, max_bytes: int) -> bytes:
    buffer = bytearray()
    while True:
        chunk = await upload.read(settings.CHUNK_SIZE)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds your plan limit of {max_bytes // (1024 * 1024)} MB.",
            )
    if not buffer:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    return bytes(buffer)


def _sniff_matches(kind: str, raw: bytes) -> bool:
    if kind == "lottie":
        head = raw[:512].lstrip()
        return head[:1] in (b"{", b"[")
    if kind == "svg":
        lowered = raw[:512].lstrip().lower()
        return lowered.startswith(b"<?xml") or lowered.startswith(b"<svg") or b"<svg" in raw[:2048].lower()
    if kind == "image":
        head = raw[:16]
        if head.startswith(_IMAGE_SIGNATURES):
            return True
        if head[:4] == b"RIFF" and raw[8:12] == b"WEBP":  # WebP
            return True
        if raw[4:8] == b"ftyp":  # HEIC / AVIF / other ISO-BMFF
            return True
        return False
    return False


def _output_filename(original: str, kind: str, output_format: str | None = None) -> str:
    stem = PurePosixPath(PurePosixPath(original or "file").name).stem or "file"
    if kind == "lottie":
        ext = ".json"
    elif kind == "svg":
        ext = ".svg"
    elif kind == "image":
        # Converted output (webp/avif) — or keep original ext if unchanged.
        ext = f".{output_format}" if output_format else PurePosixPath(original or "f.png").suffix or ".png"
    else:
        ext = PurePosixPath(original or "file").suffix or ""
    return f"{stem}.min{ext}"


def _resolve_api_user(request: Request, session: Session) -> User | None:
    """Resolve a Pro user from an ``Authorization: Bearer ta_...`` header."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    raw = header[7:].strip()
    if not raw.startswith("ta_"):
        return None
    return models.user_for_api_key(session, raw)


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    session: Session = Depends(get_session),
    user: User | None = Depends(auth.current_user),
) -> HTMLResponse:
    stats = models.get_stats(session)
    usage = None
    guest_remaining = None
    if user is not None:
        user.reset_usage_if_needed()
        session.commit()
        usage = {"count": user.usage_count, "limit": user.plan_obj.monthly_limit}
    else:
        guest_remaining = max(settings.ANON_FREE_LIMIT - auth.read_guest_count(request), 0)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "stats": stats,
            "user": user,
            "plan": user.plan_obj if user else get_plan(None),
            "usage": usage,
            "billing_enabled": settings.billing_enabled,
            "anon_limit": settings.ANON_FREE_LIMIT,
            "guest_remaining": guest_remaining,
            "anon_max_mb": settings.ANON_MAX_UPLOAD_BYTES // (1024 * 1024),
        },
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user: User | None = Depends(auth.current_user)) -> Response:
    if user is not None:
        return RedirectResponse("/account", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "sent": False, "dev_link": None})


@app.post("/auth/request", response_class=HTMLResponse)
def auth_request(request: Request, email: str = Form(...)) -> HTMLResponse:
    try:
        normalized = auth.normalize_email(email)
    except ValueError:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "sent": False, "error": "Please enter a valid email address.", "dev_link": None},
        )

    token = auth.make_magic_token(normalized)
    url = auth.magic_link_url(token)
    emailed = auth.send_magic_link(normalized, url)

    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "sent": True,
            "email": normalized,
            # In dev mode (no email provider) we surface the link so the flow
            # is testable end-to-end without an inbox.
            "dev_link": None if emailed else url,
        },
    )


@app.get("/auth/verify")
def auth_verify(token: str, session: Session = Depends(get_session)) -> Response:
    email = auth.verify_magic_token(token)
    if not email:
        raise HTTPException(status_code=400, detail="This sign-in link is invalid or expired.")
    user = auth.login_user(email, session)
    resp = RedirectResponse("/account", status_code=303)
    resp.set_cookie(
        settings.SESSION_COOKIE,
        auth.make_session_token(user),
        max_age=settings.SESSION_TTL_SECONDS,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
    )
    return resp


@app.post("/auth/logout")
def auth_logout() -> Response:
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(settings.SESSION_COOKIE)
    return resp


@app.get("/account", response_class=HTMLResponse)
def account_page(
    request: Request,
    session: Session = Depends(get_session),
    user: User | None = Depends(auth.current_user),
) -> Response:
    if user is None:
        return RedirectResponse("/login", status_code=303)
    user.reset_usage_if_needed()
    session.commit()
    keys = sorted(user.api_keys, key=lambda k: k.created_at, reverse=True)
    return templates.TemplateResponse(
        "account.html",
        {
            "request": request,
            "user": user,
            "plan": user.plan_obj,
            "usage": {"count": user.usage_count, "limit": user.plan_obj.monthly_limit},
            "api_keys": keys,
            "new_key": request.query_params.get("new_key"),
            "billing_enabled": settings.billing_enabled,
            "checkout": request.query_params.get("checkout"),
        },
    )


# --------------------------------------------------------------------------- #
# Billing
# --------------------------------------------------------------------------- #
@app.post("/billing/checkout")
def billing_checkout(
    session: Session = Depends(get_session),
    user: User | None = Depends(auth.current_user),
) -> Response:
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not settings.billing_enabled:
        raise HTTPException(status_code=503, detail="Billing is not configured yet.")
    url = billing.create_checkout_session(session, user)
    return RedirectResponse(url, status_code=303)


@app.post("/billing/portal")
def billing_portal(
    session: Session = Depends(get_session),
    user: User | None = Depends(auth.current_user),
) -> Response:
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not settings.billing_enabled or not user.stripe_customer_id:
        return RedirectResponse("/account", status_code=303)
    url = billing.create_portal_session(session, user)
    return RedirectResponse(url, status_code=303)


@app.post("/billing/webhook")
async def billing_webhook(request: Request, session: Session = Depends(get_session)) -> JSONResponse:
    payload = await request.body()
    sig = request.headers.get("stripe-signature")
    try:
        event = billing.verify_and_parse(payload, sig)
    except Exception as exc:  # signature / parse failure
        raise HTTPException(status_code=400, detail=f"Invalid webhook: {exc}") from exc
    billing.handle_event(session, event)
    return JSONResponse({"received": True})


# --------------------------------------------------------------------------- #
# API keys
# --------------------------------------------------------------------------- #
@app.post("/api/keys")
def create_key(
    session: Session = Depends(get_session),
    user: User | None = Depends(auth.current_user),
) -> Response:
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not user.plan_obj.allow_api:
        raise HTTPException(status_code=402, detail="API access requires the Pro plan.")
    raw = models.create_api_key(session, user)
    return RedirectResponse(f"/account?new_key={raw}", status_code=303)


# --------------------------------------------------------------------------- #
# Optimize / download / stats
# --------------------------------------------------------------------------- #
@app.post("/api/optimize")
async def api_optimize(
    request: Request,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> JSONResponse:
    # Resolve the caller: API key (Pro) → session cookie (web) → anonymous guest.
    api_user = _resolve_api_user(request, session)
    if api_user is not None:
        user = api_user
        if not user.plan_obj.allow_api:
            raise HTTPException(status_code=402, detail="API access requires the Pro plan.")
    else:
        user = auth.current_user(request, session)

    # --- anonymous guest path (no account) ------------------------------- #
    guest_count: int | None = None
    if user is None:
        guest_count = auth.read_guest_count(request)
        if guest_count >= settings.ANON_FREE_LIMIT:
            raise HTTPException(
                status_code=402,
                detail=(
                    f"You've used your {settings.ANON_FREE_LIMIT} free tries. "
                    "Sign in free for 20 files / month."
                ),
            )
        max_bytes = settings.ANON_MAX_UPLOAD_BYTES
        plan = get_plan(None)
    else:
        plan = user.plan_obj
        # Usage gate (rolling 30-day window) for limited plans.
        user.reset_usage_if_needed()
        if plan.monthly_limit is not None and user.usage_count >= plan.monthly_limit:
            session.commit()
            raise HTTPException(
                status_code=402,
                detail=f"You've used all {plan.monthly_limit} optimizations this month. Upgrade to Pro for unlimited.",
            )
        session.commit()
        max_bytes = plan.max_upload_bytes

    ext = _safe_extension(file.filename or "")
    kind = ALLOWED_EXTENSIONS[ext]
    raw = await _read_capped(file, max_bytes)

    if not _sniff_matches(kind, raw):
        raise HTTPException(status_code=400, detail="File content does not match its extension.")

    try:
        result = optimize_file(raw, kind)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if result.optimized_size >= result.original_size:
        result.data = raw
        result.optimized_size = result.original_size

    models.record_optimization(
        session,
        file_kind=kind,
        original_filename=PurePosixPath(file.filename or "file").name,
        original_size=result.original_size,
        optimized_size=result.optimized_size,
        user=user,
    )

    if kind == "lottie":
        media_type = "application/json"
    elif kind == "svg":
        media_type = "image/svg+xml"
    else:  # image
        media_type = _IMAGE_MEDIA_TYPES.get(result.output_format or "", "application/octet-stream")
    out_name = _output_filename(file.filename or "file", kind, result.output_format)
    token = downloads.put(result.data, out_name, media_type)

    # Remaining quota: guest trial vs. signed-in limited plan vs. unlimited.
    if user is None:
        plan_key = "guest"
        remaining = max(settings.ANON_FREE_LIMIT - (guest_count + 1), 0)
    else:
        plan_key = plan.key
        remaining = (
            max(plan.monthly_limit - user.usage_count, 0)
            if plan.monthly_limit is not None
            else None
        )

    response = JSONResponse(
        {
            "kind": kind,
            "original_filename": PurePosixPath(file.filename or "file").name,
            "output_filename": out_name,
            "original_size": result.original_size,
            "optimized_size": result.optimized_size,
            "saved_bytes": result.saved_bytes,
            "reduction_percent": result.reduction_percent,
            "download_token": token,
            "plan": plan_key,
            "remaining": remaining,
            "output_format": result.output_format,
        }
    )

    # Persist the guest's incremented trial count in a signed cookie.
    if user is None:
        response.set_cookie(
            settings.ANON_COOKIE,
            auth.make_guest_cookie(guest_count + 1),
            max_age=settings.ANON_PERIOD_SECONDS,
            httponly=True,
            secure=settings.COOKIE_SECURE,
            samesite="lax",
        )
    return response


@app.get("/api/download/{token}")
def api_download(token: str) -> Response:
    item = downloads.get(token)
    if item is None:
        raise HTTPException(status_code=404, detail="Download expired or not found.")
    data, filename, media_type = item
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(data)),
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/stats")
def api_stats(session: Session = Depends(get_session)) -> JSONResponse:
    return JSONResponse(models.get_stats(session))


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
