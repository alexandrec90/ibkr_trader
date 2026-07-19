"""Guard tests for the TimescaleDB price_bars migration.

These deliberately never require TimescaleDB (or even Postgres): the whole point of the
migration's guard is that it is a clean no-op anywhere the extension is absent, so CI and
SQLite test runs stay green. The live hypertable/compression conversion is verified manually
against the Timescale dev DB (recorded in docs/plans/tools-06-timescaledb.md), not here.
"""

import types
from importlib import import_module

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION = "migrations.versions.f7b8c9d0e1f2_timescaledb_price_bars_hypertable"


def _make_price_bars(engine: sa.Engine) -> None:
    sa.Table(
        "price_bars",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ts", sa.DateTime),
        sa.Column("close", sa.Float),
    ).create(engine)


def test_upgrade_and_downgrade_noop_on_sqlite():
    """On SQLite the migration must do nothing and never raise (hypertables are Postgres-only)."""
    engine = sa.create_engine("sqlite://")
    _make_price_bars(engine)

    migration = import_module(MIGRATION)
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO price_bars (id, ts, close) VALUES (1, '2026-01-01', 10.0)")
        )
        migration.op = Operations(MigrationContext.configure(connection))
        # Both directions are no-ops; neither may error.
        migration.upgrade()
        migration.downgrade()

        # The ordinary table is untouched: row still present, still queryable.
        rows = connection.execute(sa.text("SELECT id, close FROM price_bars")).all()
        assert rows == [(1, 10.0)]


def _patch_bind(migration, dialect_name: str, scalar_value):
    """Point the migration's ``op`` at a fake bind reporting the given dialect / extension row."""

    class _Result:
        def scalar(self):
            return scalar_value

    class _Bind:
        dialect = types.SimpleNamespace(name=dialect_name)

        def execute(self, *_args, **_kwargs):
            return _Result()

    class _Op:
        _bind = _Bind()

        def get_bind(self):
            return self._bind

        def execute(self, *_args, **_kwargs):  # pragma: no cover - guard tripwire
            raise AssertionError("DDL executed while TimescaleDB was unavailable")

    migration.op = _Op()


def test_timescale_unavailable_when_not_postgres():
    migration = import_module(MIGRATION)
    _patch_bind(migration, "sqlite", None)
    assert migration._timescale_available() is False


def test_timescale_unavailable_on_plain_postgres():
    """Postgres without the extension row -> guard False -> upgrade() stays a no-op."""
    migration = import_module(MIGRATION)
    _patch_bind(migration, "postgresql", None)
    assert migration._timescale_available() is False
    # _Op.execute would raise if the guard let DDL through.
    migration.upgrade()


def test_timescale_available_on_timescale_postgres():
    migration = import_module(MIGRATION)
    _patch_bind(migration, "postgresql", 1)
    assert migration._timescale_available() is True
