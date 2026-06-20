# TinyAnim — production image.
# Switches Render from the native Python runtime to Docker so we can install
# Ghostscript (system package) for PDF compression. Pillow / HEIF / AVIF come
# from pip wheels and need no system libs.
FROM python:3.12-slim

# Ghostscript for PDF optimization. --no-install-recommends keeps the image lean.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ghostscript \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render injects $PORT at runtime; default to 8000 locally.
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
