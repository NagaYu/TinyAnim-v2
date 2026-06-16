# TinyAnim ✨

**Lossless Lottie & SVG compression — make your animations weightless.**

TinyAnim shrinks Lottie (JSON) and SVG files by up to **80%** while preserving
**100% of the visual quality**. It does this by discarding only what the human
eye and the renderer never use:

- excess floating-point precision on coordinates, bezier tangents and time
  values,
- authoring metadata (After Effects layer names, editor namespaces, comments,
  unreferenced `id`s, `xml:space`, `data-*` attributes, …),
- redundant whitespace and verbose JSON formatting.

A solid, Apple/Vercel-grade dark UI lets you drag & drop files and instantly see
the before/after size, reduction percentage and a glowing download button.

---

## Features

- **Lottie Optimizer Core** — recursive float rounding, metadata (`nm`/`mn`/`meta`)
  stripping, and compact JSON re-serialization.
- **SVG Optimizer Core** — editor-namespace & comment removal, unreferenced-`id`
  pruning, path-data (`d`) number rounding + whitespace minification.
- **FastAPI backend** with a lightweight **SQLite** database tracking lifetime
  savings (total KB reduced, files optimized, average reduction).
- **Production-grade safety**: strict extension allow-list, content sniffing,
  streamed upload reading with a hard size ceiling (no memory blow-ups), and an
  in-memory TTL download store — uploads are **never written to disk**.
- **Zero heavy dependencies** in the optimizer core (pure Python), so it is
  trivially testable and embeddable.

---

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --reload
```

Then open <http://127.0.0.1:8000>.

---

## Project layout

```
app/
  main.py          FastAPI app, routes, upload security, download store
  optimizer.py     Lottie & SVG compression engines (pure Python)
  database.py      SQLite engine / session management
  models.py        ORM models + persistence helpers
  templates/
    index.html     Drag & drop dark-mode UI
requirements.txt
README.md
```

---

## API

| Method | Path                  | Description                                  |
| ------ | --------------------- | -------------------------------------------- |
| `GET`  | `/`                   | Landing page (UI + lifetime stats)           |
| `POST` | `/api/optimize`       | Optimize one uploaded file → JSON + token    |
| `GET`  | `/api/download/{tok}` | Download a previously optimized payload      |
| `GET`  | `/api/stats`          | Lifetime aggregate statistics (JSON)         |
| `GET`  | `/healthz`            | Liveness probe                               |

### Example

```bash
curl -F "file=@animation.json" http://127.0.0.1:8000/api/optimize
```

```json
{
  "kind": "lottie",
  "output_filename": "animation.min.json",
  "original_size": 84210,
  "optimized_size": 19877,
  "saved_bytes": 64333,
  "reduction_percent": 76.4,
  "download_token": "…"
}
```

---

## Configuration (env vars)

| Variable                    | Default          | Purpose                          |
| --------------------------- | ---------------- | -------------------------------- |
| `TINYANIM_MAX_UPLOAD_BYTES` | `15728640` (15M) | Hard per-file upload ceiling     |
| `TINYANIM_DATABASE_URL`     | `sqlite:///…`    | SQLAlchemy database URL          |

---

## How the compression stays lossless

Lottie players and SVG renderers rasterize at device pixel resolution. A
coordinate like `123.456789123` and `123.46` produce **identical pixels** on any
real display, so the extra digits are pure payload. Likewise, `nm` layer names,
editor namespaces and comments are ignored at render time. TinyAnim removes only
this redundant data — it never alters geometry, colors, timing curves or
structure.

---

## License

MIT.
