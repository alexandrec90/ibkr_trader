from importlib import import_module

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from ibkr_trader.db.models import IpoEvent

MIGRATION = "migrations.versions.b9c0d1e2f3a4_ipo_events"
LAKE_EVENTS = "migrations.versions.c0d1e2f3a4b5_lake_event_and_calendar_data"
PRICING = "migrations.versions.d1e2f3a4b5c6_listing_dates_and_ipo_pricing"

#: Added by the later PRICING revision, so absent from the tables before it.
PRICING_COLUMNS = {"instrument_id", "resolution", "resolved_at"}

#: Columns of the model that a later revision adds, so this one rightly lacks them.
#: The PRICING tests below check the table matches the model at head.
ADDED_LATER = {"is_spac"} | PRICING_COLUMNS  # c0d1e2f3a4b5, d1e2f3a4b5c6


def _run(step, monkeypatch, connection, module=MIGRATION):
    migration = import_module(module)
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
    getattr(migration, step)()


def _before_pricing(connection, monkeypatch) -> None:
    """Every table PRICING alters, as the revisions before it left them.

    ``instruments`` and ``earnings_events`` are stubs of what the earlier revisions built;
    ``ipo_events`` is the real chain, this file's revision then LAKE_EVENTS (``is_spac``).
    """
    metadata = sa.MetaData()
    sa.Table(
        "instruments",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("symbol", sa.String(32)),
    )
    sa.Table(
        "earnings_events",
        metadata,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("instrument_id", sa.Integer(), sa.ForeignKey("instruments.id")),
        sa.Column("report_ts", sa.DateTime(timezone=True)),
        sa.Column("source", sa.String(32)),
    )
    metadata.create_all(connection)
    _run("upgrade", monkeypatch, connection)
    _run("upgrade", monkeypatch, connection, LAKE_EVENTS)


def _nullable_columns(connection, table: str) -> dict[str, bool]:
    # id is the primary key; SQLite reports a PK column nullable, which says nothing here.
    return {
        c["name"]: c["nullable"]
        for c in sa.inspect(connection).get_columns(table)
        if c["name"] != "id"
    }


def test_upgrade_creates_the_table_the_model_declares(monkeypatch):
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _run("upgrade", monkeypatch, connection)
        inspector = sa.inspect(connection)
        columns = _nullable_columns(connection, "ipo_events")
        indexes = {i["name"] for i in inspector.get_indexes("ipo_events")}
        uniques = [u["column_names"] for u in inspector.get_unique_constraints("ipo_events")]

    model = IpoEvent.__table__
    expected = {c.name: c.nullable for c in model.columns if c.name not in ADDED_LATER | {"id"}}
    assert columns == expected
    assert indexes == {i.name for i in model.indexes}
    assert uniques == [["source", "external_id"]]


def test_pricing_revision_completes_the_table_the_model_declares(monkeypatch):
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _before_pricing(connection, monkeypatch)
        _run("upgrade", monkeypatch, connection, PRICING)
        columns = _nullable_columns(connection, "ipo_events")
        foreign_keys = sa.inspect(connection).get_foreign_keys("ipo_events")
        instruments = _nullable_columns(connection, "instruments")

    model = IpoEvent.__table__
    assert columns == {c.name: c.nullable for c in model.columns if c.name != "id"}
    assert [(fk["constrained_columns"], fk["referred_table"]) for fk in foreign_keys] == [
        (["instrument_id"], "instruments")
    ]
    assert instruments["listed_on"] is True  # nullable: unknown is the honest default


def test_pricing_downgrade_restores_the_previous_tables(monkeypatch):
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _before_pricing(connection, monkeypatch)
        before = _nullable_columns(connection, "ipo_events")
        _run("upgrade", monkeypatch, connection, PRICING)
        _run("downgrade", monkeypatch, connection, PRICING)
        assert _nullable_columns(connection, "ipo_events") == before
        assert "listed_on" not in _nullable_columns(connection, "instruments")
        assert sa.inspect(connection).get_foreign_keys("ipo_events") == []


def test_downgrade_drops_it(monkeypatch):
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _run("upgrade", monkeypatch, connection)
        _run("downgrade", monkeypatch, connection)
        assert "ipo_events" not in sa.inspect(connection).get_table_names()
