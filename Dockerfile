# syntax=docker/dockerfile:1.7
# Server image — FastAPI API + RQ worker + Scheduler
# Build target: production (multi-worker, --proxy-headers)
# Override command in docker-compose.yml untuk run sebagai worker/scheduler/api.

ARG PYTHON_VERSION=3.13

FROM python:${PYTHON_VERSION}-slim AS base

# Environment
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONHASHSEED=random

# System dependencies (curl untuk healthcheck, ca-certificates untuk TLS ke DB)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps terpisah dari source code (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY app/ ./app/

# Non-root user untuk security (no-new-privileges, dll)
RUN groupadd --system --gid 1001 appuser \
    && useradd --system --uid 1001 --gid appuser --shell /bin/bash --create-home appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

# Healthcheck (dipakai oleh docker-compose untuk api service)
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# Default command (override di compose per service)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
