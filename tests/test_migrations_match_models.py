"""The migration chain builds exactly the schema the models declare -- ``alembic check`` here.

The models are not all ours: ``data_lake.db.models`` is re-exported through
``ibkr_trader.db.models``, and a data-lake commit changes them without a line changing here.
data-lake 730f759 added ``ipo_events.is_spac`` (and five tables), and for eleven hours no
revision here matched it, until ``c0d1e2f3a4b5``. Any database at the older head fails the
``ipo_calendar``, ``ipo_filings`` and ``ipo_alerts`` jobs on ``column ipo_events.is_spac does
not exist`` (production did, because nothing applied the revision). The per-revision migration
tests could not have caught the gap: each one checks only the tables its author knew the
revision touched. This one
runs every revision from base to head and compares the result with the whole
``Base.metadata``, through the same ``include_object`` filter ``migrations/env.py`` uses, so a
model change with no revision to match fails here -- against whichever data-lake ref the gate
pins, which is why a pin bump that brings new models must bring their migration with it.

The suite runs on in-memory SQLite (.claude/rules/testing.md), and two things the chain does
on Postgres need translating for it, both scoped to the test by ``monkeypatch``:

- JSONB columns compile as SQLite's JSON;
- ``a7b8c9d0e1f2`` adds a foreign key and a unique constraint by ALTER, which SQLite has
  not got, so the constraint is added through a batch rebuild of the table instead.

Integer width is not compared: data-lake's ``SqliteFriendlyBigInt`` is BIGINT on Postgres and
INTEGER on SQLite by design, so on SQLite every BIGINT id reads as a type change. Postgres-only
objects (the TimescaleDB hypertable's own ``price_bars_ts_idx``) are absent on SQLite and so
are not compared either; ``alembic check`` against a migrated Postgres covers those.
"""

from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.ddl.sqlite import SQLiteImpl
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

from ibkr_trader.db.adoption import include_object
from ibkr_trader.db.models import Base

ROOT = Path(__file__).resolve().parents[1]

#: The last revision before data-lake 730f759's models: production's schema when the
#: ``is_spac`` jobs failed.
BEFORE_EVENT_DATA = "b9c0d1e2f3a4"


def _script() -> ScriptDirectory:
    return ScriptDirectory(str(ROOT / "migrations"))


def _add_constraint_by_rebuild(impl: SQLiteImpl, constraint: Any) -> None:
    """SQLite's stand-in for ``ALTER TABLE ... ADD CONSTRAINT``: rebuild the table with it."""
    operations = Operations(MigrationContext.configure(impl.connection))
    with operations.batch_alter_table(constraint.table.name, recreate="always") as batch:
        if isinstance(constraint, sa.ForeignKeyConstraint):
            referent = {e.target_fullname.split(".")[0] for e in constraint.elements}
            batch.create_foreign_key(
                constraint.name,
                referent.pop(),
                [e.parent.name for e in constraint.elements],
                [e.target_fullname.split(".")[1] for e in constraint.elements],
            )
        elif isinstance(constraint, sa.UniqueConstraint):
            batch.create_unique_constraint(constraint.name, [c.name for c in constraint.columns])
        else:
            raise NotImplementedError(f"no SQLite stand-in for {constraint!r}")


@pytest.fixture
def sqlite_runs_the_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        SQLiteTypeCompiler, "visit_JSONB", SQLiteTypeCompiler.visit_JSON, raising=False
    )
    monkeypatch.setattr(SQLiteImpl, "add_constraint", _add_constraint_by_rebuild)


def _upgrade(connection: sa.Connection, destination: str) -> None:
    """Run every revision from base to ``destination``, oldest first."""
    revisions = list(_script().walk_revisions("base", destination))
    with Operations.context(MigrationContext.configure(connection)):
        for revision in reversed(revisions):
            revision.module.upgrade()


def _same_unless_width(_context, _inspected, _metadata, inspected_type, metadata_type):
    """Ignore integer width (see the module docstring); defer every other type to Alembic."""
    if isinstance(inspected_type, sa.Integer) and isinstance(metadata_type, sa.Integer):
        return False
    return None


def _describe(entry: Any) -> str:
    if isinstance(entry, list):
        return "; ".join(_describe(e) for e in entry)
    kind, *rest = entry
    if kind.startswith("modify_"):
        _schema, table, column = rest[:3]
        return f"{kind} {table}.{column}"
    if kind in ("add_column", "remove_column"):
        _schema, table, column = rest
        return f"{kind} {table}.{column.name}"
    obj = rest[0]
    table = getattr(getattr(obj, "table", None), "name", None)
    return f"{kind} {f'{table}.' if table else ''}{obj.name}"


def _drift(destination: str) -> list[str]:
    """What autogenerate would still propose after migrating an empty database."""
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _upgrade(connection, destination)
        context = MigrationContext.configure(
            connection,
            opts={"include_object": include_object, "compare_type": _same_unless_width},
        )
        return [_describe(entry) for entry in compare_metadata(context, Base.metadata)]


def test_migrations_build_the_schema_the_models_declare(sqlite_runs_the_chain):
    drift = _drift("heads")

    assert drift == [], (
        "the models and the migrations disagree; write a revision "
        "(`uv run alembic revision --autogenerate`) for:\n  " + "\n  ".join(drift)
    )


def test_the_check_sees_the_is_spac_drift_production_hit(sqlite_runs_the_chain):
    """Not vacuous: stopped short of the event-data revision, it names the missing column."""
    drift = _drift(BEFORE_EVENT_DATA)

    assert "add_column ipo_events.is_spac" in drift
    assert "add_table stock_splits" in drift
