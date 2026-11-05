# Backend: FastAPI + asyncpg, served by uvicorn.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Runtime system deps (curl is used by the healthcheck below).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for layer caching.
# --only-binary=:all: installs pre-built wheels only, so no package setup
# scripts are executed during the install. Every version is pinned (the lock
# file plus the explicit, exact Pillow pin) for a reproducible dependency tree.
# Pillow is used by the avatar service but is not in the lock file, so it is
# pinned explicitly here.
COPY requirements.lock.txt ./
RUN pip install --only-binary=:all: -r requirements.lock.txt \
    && pip install --only-binary=:all: "Pillow==11.0.0"

# App code.
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

# Create and run as an unprivileged user instead of root. The uploads directory
# (avatar uploads / static mount) must be writable by that user.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/uploads \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=10 \
    CMD curl -fsS http://localhost:8000/docs > /dev/null || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
