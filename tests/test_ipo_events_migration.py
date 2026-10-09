from importlib import import_module

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from ibkr_trader.db.models import IpoEvent

MIGRATION = "migrations.versions.b9c0d1e2f3a4_ipo_events"


def _run(step, monkeypatch, connection):
    migration = import_module(MIGRATION)
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
    getattr(migration, step)()


def test_upgrade_creates_the_table_the_model_declares(monkeypatch):
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _run("upgrade", monkeypatch, connection)
        inspector = sa.inspect(connection)
        columns = {c["name"]: c["nullable"] for c in inspector.get_columns("ipo_events")}
        indexes = {i["name"] for i in inspector.get_indexes("ipo_events")}
        uniques = [u["column_names"] for u in inspector.get_unique_constraints("ipo_events")]

    model = IpoEvent.__table__
    # id is the primary key; SQLite reports a PK column nullable, which says nothing here.
    expected = {c.name: c.nullable for c in model.columns if c.name != "id"}
    assert {k: v for k, v in columns.items() if k != "id"} == expected
    assert set(columns) == {c.name for c in model.columns}
    assert indexes == {i.name for i in model.indexes}
    assert uniques == [["source", "external_id"]]


def test_downgrade_drops_it(monkeypatch):
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _run("upgrade", monkeypatch, connection)
        _run("downgrade", monkeypatch, connection)
        assert "ipo_events" not in sa.inspect(connection).get_table_names()
