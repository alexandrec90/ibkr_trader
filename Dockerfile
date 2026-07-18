# Build with uv: copy the resolver's static binary from the official image.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Byte-compile on install and copy (not link) into the image's own venv.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# Layer-friendly install: resolve deps from the lockfile first (no project code yet), so
# dependency layers stay cached across source-only changes.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

COPY alembic.ini ./
COPY migrations ./migrations

# Non-root user
RUN useradd --create-home appuser
USER appuser

CMD ["ibkr-trader", "serve"]
