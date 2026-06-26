# syntax=docker/dockerfile:1
#
# StockLens container — one image that serves both delivery surfaces:
#   * the FastAPI JSON API   (default CMD: uvicorn on :8000)
#   * the Streamlit dashboard (override CMD; see docker-compose.yml, :8501)
#
# The deterministic synthetic artifacts are baked at build time (fixed RNG seed), so the
# container serves real numbers immediately with no runtime data step.

# ── Build stage: install deps + bake the artifacts ────────────────────────────
FROM python:3.12-slim-bookworm AS builder

# uv (pinned) provides fast, locked installs from uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-system \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Cache the dependency layer from the manifests before copying the source.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --extra api --extra viz

# Install the project, then bake + validate the synthetic dataset.
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra api --extra viz
RUN uv run python cli.py all && uv run python cli.py validate

# ── Runtime stage: slim image with just the venv, source, and baked artifacts ──
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN useradd --create-home --uid 1000 app
COPY --from=builder --chown=app:app /app /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').getcode()==200 else 1)"

# Default surface: the JSON API. The dashboard reuses this image with a CMD override.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
