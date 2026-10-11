"""The c0d1e2f3a4b5 migration materialises exactly what data-lake's event models declare."""

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

IPO_MIGRATION = "migrations.versions.b9c0d1e2f3a4_ipo_events"
MIGRATION = "migrations.versions.c0d1e2f3a4b5_lake_event_and_calendar_data"

NEW_TABLES = (StockSplit, CorporateEvent, MacroObservation, EconomicEvent, SecFact)
#: Every table the migration creates or alters; each must match its model afterwards.
TOUCHED = (*NEW_TABLES, EarningsEvent, IpoEvent)

#: What ``earnings_events`` looked like before this revision (c2d3e4f5a6b7).
EARNINGS_BEFORE = {"id", "instrument_id", "report_ts", "source"}

#: Columns of a model that a later revision adds, so this one rightly lacks them.
#: tests/test_ipo_events_migration.py checks ``ipo_events`` matches the model at head.
ADDED_LATER = {"ipo_events": {"instrument_id", "resolution", "resolved_at"}}  # e2f3a4b5c6d7


def _run(module, step, monkeypatch, connection):
    migration = import_module(module)
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
    getattr(migration, step)()


def _previous_schema(connection, monkeypatch):
    """The tables this revision builds on, as the earlier revisions left them."""
    metadata = sa.MetaData()
    sa.Table("instruments", metadata, sa.Column("id", sa.Integer, primary_key=True))
    sa.Table(
        "earnings_events",
        metadata,
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("instrument_id", sa.Integer, sa.ForeignKey("instruments.id"), nullable=False),
        sa.Column("report_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.UniqueConstraint("instrument_id", "report_ts", "source"),
    )
    metadata.create_all(connection)
    _run(IPO_MIGRATION, "upgrade", monkeypatch, connection)


def _schema(connection, table):
    inspector = sa.inspect(connection)
    # id is the primary key; SQLite reports a PK column nullable, which says nothing here.
    columns = {c["name"]: c["nullable"] for c in inspector.get_columns(table) if c["name"] != "id"}
    indexes = {i["name"] for i in inspector.get_indexes(table)}
    uniques = {tuple(u["column_names"]) for u in inspector.get_unique_constraints(table)}
    return columns, indexes, uniques


def _declared(model):
    table = model.__table__
    later = ADDED_LATER.get(table.name, set())
    columns = {c.name: c.nullable for c in table.columns if c.name not in later | {"id"}}
    indexes = {i.name for i in table.indexes}
    uniques = {
        tuple(c.name for c in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    return columns, indexes, uniques


@pytest.mark.parametrize("model", TOUCHED, ids=lambda m: m.__tablename__)
def test_upgrade_matches_the_model(monkeypatch, model):
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _previous_schema(connection, monkeypatch)
        _run(MIGRATION, "upgrade", monkeypatch, connection)
        actual = _schema(connection, model.__tablename__)

    assert actual == _declared(model)


def test_upgrade_keeps_existing_earnings_rows(monkeypatch):
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _previous_schema(connection, monkeypatch)
        connection.execute(sa.text("INSERT INTO instruments (id) VALUES (1)"))
        connection.execute(
            sa.text(
                "INSERT INTO earnings_events (id, instrument_id, report_ts, source) "
                "VALUES (1, 1, '2026-01-01 00:00:00', 'yahoo')"
            )
        )
        _run(MIGRATION, "upgrade", monkeypatch, connection)
        row = connection.execute(
            sa.text("SELECT source, eps_actual, first_seen_at FROM earnings_events")
        ).one()

    assert tuple(row) == ("yahoo", None, None)


def test_downgrade_restores_the_previous_schema(monkeypatch):
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _previous_schema(connection, monkeypatch)
        _run(MIGRATION, "upgrade", monkeypatch, connection)
        _run(MIGRATION, "downgrade", monkeypatch, connection)
        inspector = sa.inspect(connection)
        tables = set(inspector.get_table_names())
        earnings = {c["name"] for c in inspector.get_columns("earnings_events")}
        earnings_indexes = {i["name"] for i in inspector.get_indexes("earnings_events")}
        ipo = {c["name"] for c in inspector.get_columns("ipo_events")}

    assert tables.isdisjoint(m.__tablename__ for m in NEW_TABLES)
    assert earnings == EARNINGS_BEFORE
    assert "ix_earnings_events_report_ts" not in earnings_indexes
    assert "is_spac" not in ipo
