from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from ibkr_trader.db.models import (
    CorporateEvent,
    EarningsEvent,
    EconomicEvent,
    IpoEvent,
    MacroObservation,
    SecFact,
    StockSplit,
)

MIGRATION = "migrations.versions.c0d1e2f3a4b5_market_events"
IPO_MIGRATION = "migrations.versions.b9c0d1e2f3a4_ipo_events"

NEW_TABLES = (CorporateEvent, EconomicEvent, MacroObservation, SecFact, StockSplit)


def _run(module: str, step: str, monkeypatch, connection) -> None:
    migration = import_module(module)
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
    getattr(migration, step)()


def _previous_schema(monkeypatch, connection) -> None:
    """The tables this revision alters or references, as the revisions before it left them."""
    connection.execute(sa.text("CREATE TABLE instruments (id INTEGER PRIMARY KEY)"))
    connection.execute(
        sa.text(
            "CREATE TABLE earnings_events (id INTEGER PRIMARY KEY, instrument_id INTEGER NOT NULL"
            " REFERENCES instruments (id), report_ts DATETIME NOT NULL, source VARCHAR(32) NOT"
            " NULL, UNIQUE (instrument_id, report_ts, source))"
        )
    )
    _run(IPO_MIGRATION, "upgrade", monkeypatch, connection)


def _columns(connection, table: str) -> dict[str, bool]:
    return {
        c["name"]: c["nullable"]
        for c in sa.inspect(connection).get_columns(table)
        if c["name"] != "id"
    }


def _model_columns(model) -> dict[str, bool]:
    # id is the primary key; SQLite reports a PK column nullable, which says nothing here.
    return {c.name: c.nullable for c in model.__table__.columns if c.name != "id"}


@pytest.fixture
def upgraded(monkeypatch):
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _previous_schema(monkeypatch, connection)
        _run(MIGRATION, "upgrade", monkeypatch, connection)
        yield connection


@pytest.mark.parametrize("model", NEW_TABLES, ids=lambda m: m.__tablename__)
def test_upgrade_creates_each_table_the_model_declares(upgraded, model):
    table = model.__tablename__
    inspector = sa.inspect(upgraded)
    assert _columns(upgraded, table) == _model_columns(model)
    assert {i["name"] for i in inspector.get_indexes(table)} == {
        i.name for i in model.__table__.indexes
    }
    declared = sorted(
        sorted(c.columns.keys())
        for c in model.__table__.constraints
        if isinstance(c, sa.UniqueConstraint)
    )
    assert sorted(sorted(u["column_names"]) for u in inspector.get_unique_constraints(table)) == (
        declared
    )


@pytest.mark.parametrize("model", (EarningsEvent, IpoEvent), ids=lambda m: m.__tablename__)
def test_upgrade_brings_altered_tables_to_the_model(upgraded, model):
    assert _columns(upgraded, model.__tablename__) == _model_columns(model)
    indexes = {i["name"] for i in sa.inspect(upgraded).get_indexes(model.__tablename__)}
    assert indexes == {i.name for i in model.__table__.indexes}


def test_downgrade_restores_the_previous_schema(monkeypatch):
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _previous_schema(monkeypatch, connection)
        before = {t: _columns(connection, t) for t in ("earnings_events", "ipo_events")}
        _run(MIGRATION, "upgrade", monkeypatch, connection)
        _run(MIGRATION, "downgrade", monkeypatch, connection)
        tables = set(sa.inspect(connection).get_table_names())
        assert not tables & {m.__tablename__ for m in NEW_TABLES}
        assert {t: _columns(connection, t) for t in before} == before
