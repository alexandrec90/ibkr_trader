# Build with uv: copy the resolver's static binary from the official image.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Byte-compile on install and copy (not link) into the image's own venv.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# Build context is the PARENT directory (see docker-compose.yml): `data-lake` is a sibling
# checkout and an editable path dependency, so it has to be inside the context to be copied.
# Its source is copied before `uv sync` because an editable install resolves the path at
# install time and imports it from that location at runtime.
COPY data-lake /data-lake

# Layer-friendly install: resolve deps from the lockfile first (no project code yet), so
# dependency layers stay cached across source-only changes.
COPY ibkr_trader/pyproject.toml ibkr_trader/uv.lock ibkr_trader/README.md ./
COPY ibkr_trader/src ./src
RUN uv sync --frozen --no-dev

COPY ibkr_trader/alembic.ini ./
COPY ibkr_trader/migrations ./migrations

# Non-root user
RUN useradd --create-home appuser
USER appuser

CMD ["ibkr-trader", "serve"]
