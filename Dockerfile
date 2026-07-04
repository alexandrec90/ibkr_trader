FROM python:3.12-slim

WORKDIR /app

# Layer-friendly install: metadata + source first, then the rest.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY alembic.ini ./
COPY migrations ./migrations

# Non-root user
RUN useradd --create-home appuser
USER appuser

CMD ["ibkr-trader", "serve"]
