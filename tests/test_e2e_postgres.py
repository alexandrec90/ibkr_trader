"""The schema and the UI against the real TimescaleDB container: the tier `scripts/e2e.py` runs.

Everything else under `tests/` runs on in-memory SQLite (`.claude/rules/testing.md`), so
nothing there can say whether the migration chain applies on the Postgres this project
deploys. `test_migrations_match_models.py` names what it cannot compare -- BIGINT widths,
JSONB, the TimescaleDB hypertable `f7b8c9d0e1f2` builds -- and leaves them to "``alembic
check`` against a migrated Postgres", which until this file nothing ran. Production found
the last such gap (`ipo_events.is_spac`) by failing three jobs.

**Nothing here touches the dataset.** Each run creates its own database on the `db`
container's server, migrates it from empty, and drops it afterwards; the `ibkr_trader`
database is only the address the server is reached through. So a migration merged today
and not yet applied to the dev database is no red here -- `scripts/db-upgrade.py` is what
carries it there.

The connection string resolves as `scripts/db-upgrade.py` resolves it (the environment,
then this checkout's `.env`, then the 5433 default), so this tier and the fix pass's
migrator cannot disagree on which server they mean. A server that is not up is a failure
naming what to start, not a skip: under the scheduler a skip is red anyway, and by hand a
skip would read as green.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

from ibkr_trader.db.adoption import include_object
from ibkr_trader.db.models import Base

pytestmark = pytest.mark.local

ROOT = Path(__file__).resolve().parents[1]
SCRATCH_PREFIX = "ibkr_trader_e2e_"
# Every read the UI's pages make. `POST /api/plan` is left out: it writes a plan file.
UI_READS = (
    "/api/runs",
    "/api/curves?ids=",
    "/api/model",
    "/api/shadow",
    "/api/paper",
    "/api/plan",
    "/api/reports",
)


def _load_db_upgrade() -> ModuleType:
    path = ROOT / "scripts" / "db-upgrade.py"
    name = "_test_e2e_db_upgrade"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


db_upgrade = _load_db_upgrade()


def _server_url() -> tuple[URL, str]:
    """`(the dev database's URL, where it came from)`, failing when nothing answers there."""
    raw, where = db_upgrade.database_source()
    if not db_upgrade.reachable(raw):
        host, port = db_upgrade.address(raw)
        pytest.fail(
            f"nothing accepts connections on {host}:{port} (DATABASE_URL from {where}); "
            "start the database with `docker compose up -d db`"
        )
    return make_url(raw), where


@pytest.fixture(scope="module")
def scratch_url() -> Iterator[URL]:
    """A database of its own on the dev server, empty, dropped when the module is done."""
    server, where = _server_url()
    name = f"{SCRATCH_PREFIX}{uuid4().hex[:12]}"
    admin = sa.create_engine(server, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    except sa.exc.OperationalError as exc:
        admin.dispose()
        pytest.fail(f"the server at DATABASE_URL from {where} refused CREATE DATABASE: {exc}")
    try:
        yield server.set(database=name)
    finally:
        with admin.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="module")
def migrated(scratch_url: URL) -> Iterator[sa.Engine]:
    """The scratch database at head, migrated by the CLI the fix pass and `serve` use."""
    url = scratch_url.render_as_string(hide_password=False)
    output, code = db_upgrade.alembic(url, "upgrade", "heads")
    assert code == 0, "alembic upgrade heads failed:\n" + "\n".join(output)
    engine = sa.create_engine(scratch_url)
    try:
        yield engine
    finally:
        engine.dispose()


def _describe(entry: Any) -> str:
    """One autogenerate diff as `kind table.object`, the way `alembic check` reports it."""
    if isinstance(entry, list):
        return "; ".join(_describe(e) for e in entry)
    kind, *rest = entry
    if kind.startswith("modify_"):
        return f"{kind} {rest[1]}.{rest[2]}"
    if kind in ("add_column", "remove_column"):
        return f"{kind} {rest[1]}.{rest[2].name}"
    obj = rest[0]
    table = getattr(getattr(obj, "table", None), "name", None)
    return f"{kind} {f'{table}.' if table else ''}{obj.name}"


def test_the_chain_migrates_an_empty_postgres_to_the_schema_the_models_declare(migrated):
    """`alembic check` on Postgres: every revision applies, and nothing is left to propose."""
    heads = set(ScriptDirectory(str(ROOT / "migrations")).get_heads())
    with migrated.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"include_object": include_object, "compare_type": True}
        )
        assert set(context.get_current_heads()) == heads
        drift = [_describe(entry) for entry in compare_metadata(context, Base.metadata)]
    assert drift == []


def test_price_bars_is_a_hypertable_on_the_timescaledb_image(migrated):
    """`f7b8c9d0e1f2` converts only where the extension is available, which CI never is:
    the `db` image ships it, so a silent no-op here would leave production uncompressed."""
    with migrated.connect() as connection:
        found = connection.execute(
            sa.text(
                "SELECT hypertable_name FROM timescaledb_information.hypertables "
                "WHERE hypertable_name = 'price_bars'"
            )
        ).scalars()
        assert list(found) == ["price_bars"]


def test_every_ui_read_answers_on_a_freshly_migrated_postgres(migrated, tmp_path):
    """The pages' queries on the dialect they run on; SQLite accepts SQL Postgres refuses.

    The `ui` extra is imported here, not at the top: `run-tests.py` and a bare `uv run
    pytest` sync without it, and a module-level import would break their collection of
    the whole suite. `scripts/e2e.py` installs it, so here a missing one fails by name.
    """
    from fastapi.testclient import TestClient

    from ibkr_trader.web.app import AppConfig, create_app

    @contextmanager
    def open_session() -> Iterator[Session]:
        with Session(migrated, expire_on_commit=False) as session:
            yield session

    app = create_app(
        AppConfig(
            session_factory=open_session,
            models_dir=tmp_path / "models",
            reports_dir=tmp_path / "reports",
            static_dir=None,
            today=lambda: date(2026, 11, 12),
            allowed_hosts=("testserver",),
        )
    )
    client = TestClient(app)
    answers = {path: client.get(path) for path in UI_READS}
    failed = {path: (r.status_code, r.text[:200]) for path, r in answers.items() if r.is_error}
    assert failed == {}
