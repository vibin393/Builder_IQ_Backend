# Backend: FastAPI + asyncpg, served by uvicorn.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps occasionally needed by asyncpg/build wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for layer caching. Pillow is declared in pyproject
# (used by the avatar service) but was missing from the lock file, so install it
# explicitly alongside the lock.
COPY requirements.lock.txt ./
RUN pip install -r requirements.lock.txt && pip install "Pillow>=11.0"

# App code.
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY pyproject.toml ./

# Avatar uploads live here; present so static mount does not fail.
RUN mkdir -p uploads

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=10 \
    CMD curl -fsS http://localhost:8000/docs > /dev/null || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
