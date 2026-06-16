"""
TinyAnim — FastAPI application
==============================

Endpoints
---------
GET  /                 Landing page (drag & drop UI + lifetime stats).
POST /api/optimize     Optimize a single uploaded Lottie/SVG file, returns JSON
                       describing the result plus a one-time download token.
GET  /api/download/{t} Download a previously optimized payload.
GET  /api/stats        Lifetime aggregate statistics (JSON).
GET  /healthz          Liveness probe.

Security / robustness
---------------------
* Strict extension allow-list (``.json`` / ``.svg``).
* Content-sniffing: the bytes must actually look like Lottie JSON or SVG.
* Hard upload-size ceiling enforced *while streaming* so a malicious large
  upload can never be buffered fully into memory.
* Optimized payloads are held in a small bounded in-memory TTL cache keyed by
  an unguessable token — never written to disk.
"""

from __future__ import annotations

import os
import secrets
import time
from pathlib import PurePosixPath

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import models
from .database import get_session, init_db
from .optimizer import optimize_file

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
MAX_UPLOAD_BYTES = int(os.environ.get("TINYANIM_MAX_UPLOAD_BYTES", 15 * 1024 * 1024))  # 15 MB
CHUNK_SIZE = 64 * 1024
DOWNLOAD_TTL_SECONDS = 600  # tokens expire after 10 minutes
MAX_PENDING_DOWNLOADS = 256  # bound the cache to avoid unbounded growth

ALLOWED_EXTENSIONS = {".json": "lottie", ".svg": "svg"}

_BASE_DIR = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(_BASE_DIR, "templates"))

app = FastAPI(title="TinyAnim", description="Lossless Lottie & SVG compression", version="1.0.0")

_static_dir = os.path.join(_BASE_DIR, "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


# --------------------------------------------------------------------------- #
# Bounded TTL download store (in-memory, never hits disk)
# --------------------------------------------------------------------------- #
class _DownloadStore:
    def __init__(self) -> None:
        self._items: dict[str, tuple[bytes, str, str, float]] = {}

    def _evict_expired(self) -> None:
        now = time.monotonic()
        expired = [k for k, (_, _, _, exp) in self._items.items() if exp < now]
        for k in expired:
            self._items.pop(k, None)
        # Hard cap: drop oldest if we somehow exceed the bound.
        while len(self._items) > MAX_PENDING_DOWNLOADS:
            oldest = min(self._items, key=lambda k: self._items[k][3])
            self._items.pop(oldest, None)

    def put(self, data: bytes, filename: str, media_type: str) -> str:
        self._evict_expired()
        token = secrets.token_urlsafe(24)
        self._items[token] = (
            data,
            filename,
            media_type,
            time.monotonic() + DOWNLOAD_TTL_SECONDS,
        )
        return token

    def get(self, token: str) -> tuple[bytes, str, str] | None:
        self._evict_expired()
        item = self._items.get(token)
        if item is None:
            return None
        data, filename, media_type, _ = item
        return data, filename, media_type


downloads = _DownloadStore()


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
@app.on_event("startup")
def _startup() -> None:
    init_db()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _safe_extension(filename: str) -> str:
    """Validate and return the lowercased extension, or raise 400."""
    name = PurePosixPath(filename or "").name  # strip any path components
    ext = PurePosixPath(name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Only .json (Lottie) and .svg are accepted.",
        )
    return ext


async def _read_capped(upload: UploadFile) -> bytes:
    """Read an upload into memory while enforcing ``MAX_UPLOAD_BYTES``.

    Streaming in chunks lets us abort *before* a hostile upload is fully
    buffered, preventing memory exhaustion.
    """
    buffer = bytearray()
    while True:
        chunk = await upload.read(CHUNK_SIZE)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.",
            )
    if not buffer:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    return bytes(buffer)


def _sniff_matches(kind: str, raw: bytes) -> bool:
    """Cheap content sniff so the bytes match the claimed extension."""
    head = raw[:512].lstrip()
    if kind == "lottie":
        return head[:1] in (b"{", b"[")
    if kind == "svg":
        lowered = head.lower()
        return lowered.startswith(b"<?xml") or lowered.startswith(b"<svg") or b"<svg" in raw[:2048].lower()
    return False


def _output_filename(original: str, kind: str) -> str:
    stem = PurePosixPath(PurePosixPath(original or "file").name).stem or "file"
    ext = ".json" if kind == "lottie" else ".svg"
    return f"{stem}.min{ext}"


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    stats = models.get_stats(session)
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "stats": stats, "max_mb": MAX_UPLOAD_BYTES // (1024 * 1024)},
    )


@app.get("/api/stats")
def api_stats(session: Session = Depends(get_session)) -> JSONResponse:
    return JSONResponse(models.get_stats(session))


@app.post("/api/optimize")
async def api_optimize(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> JSONResponse:
    ext = _safe_extension(file.filename or "")
    kind = ALLOWED_EXTENSIONS[ext]

    raw = await _read_capped(file)

    if not _sniff_matches(kind, raw):
        raise HTTPException(
            status_code=400,
            detail="File content does not match its extension.",
        )

    try:
        result = optimize_file(raw, kind)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Never serve an output larger than the input.
    if result.optimized_size >= result.original_size:
        result.data = raw
        result.optimized_size = result.original_size

    models.record_optimization(
        session,
        file_kind=kind,
        original_filename=PurePosixPath(file.filename or "file").name,
        original_size=result.original_size,
        optimized_size=result.optimized_size,
    )

    media_type = "application/json" if kind == "lottie" else "image/svg+xml"
    out_name = _output_filename(file.filename or "file", kind)
    token = downloads.put(result.data, out_name, media_type)

    return JSONResponse(
        {
            "kind": kind,
            "original_filename": PurePosixPath(file.filename or "file").name,
            "output_filename": out_name,
            "original_size": result.original_size,
            "optimized_size": result.optimized_size,
            "saved_bytes": result.saved_bytes,
            "reduction_percent": result.reduction_percent,
            "download_token": token,
        }
    )


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


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
