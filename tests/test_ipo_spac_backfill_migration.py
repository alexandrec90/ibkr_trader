"""d1e2f3a4b5c6 labels the EDGAR IPO rows that predate ``is_spac`` from the SIC in ``raw``."""

import json
from importlib import import_module
from types import SimpleNamespace

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

IPO_MIGRATION = "migrations.versions.b9c0d1e2f3a4_ipo_events"
LAKE_MIGRATION = "migrations.versions.c0d1e2f3a4b5_lake_event_and_calendar_data"
MIGRATION = "migrations.versions.d1e2f3a4b5c6_ipo_events_spac_backfill"


def _run(module, step, monkeypatch, connection):
    migration = import_module(module)
    monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
    getattr(migration, step)()


class _RecordingOp:
    """An ``op`` whose bind reports ``dialect`` and which records what it would execute."""

    def __init__(self, dialect: str) -> None:
        self.executed: list[str] = []
        self._bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect))

    def get_bind(self):
        return self._bind

    def execute(self, statement: str) -> None:
        self.executed.append(statement)


def _ipo_table(monkeypatch, connection) -> None:
    connection.execute(sa.text("CREATE TABLE instruments (id INTEGER PRIMARY KEY)"))
    connection.execute(
        sa.text(
            "CREATE TABLE earnings_events (id INTEGER PRIMARY KEY, instrument_id INTEGER NOT NULL,"
            " report_ts DATETIME NOT NULL, source VARCHAR(32) NOT NULL)"
        )
    )
    _run(IPO_MIGRATION, "upgrade", monkeypatch, connection)
    _run(LAKE_MIGRATION, "upgrade", monkeypatch, connection)


def _insert(connection, external_id: str, source: str, raw: dict | None, is_spac=None) -> None:
    # id is BIGINT, which SQLite does not alias to its rowid, so it is never filled in.
    next_id = connection.execute(sa.text("SELECT COUNT(*) + 1 FROM ipo_events")).scalar_one()
    connection.execute(
        sa.text(
            "INSERT INTO ipo_events (id, source, external_id, stage, company_name, raw,"
            " first_seen_at, fetched_at, is_spac) VALUES (:id, :source, :external_id, 'filed',"
            " 'X', :raw, '2026-01-02 00:00:00', '2026-01-02 00:00:00', :is_spac)"
        ),
        {
            "id": next_id,
            "source": source,
            "external_id": external_id,
            "raw": None if raw is None else json.dumps(raw),
            "is_spac": is_spac,
        },
    )


def test_upgrade_backfills_on_postgres_only(monkeypatch):
    migration = import_module(MIGRATION)
    for dialect, expected in (("postgresql", [migration.BACKFILL]), ("sqlite", [])):
        op = _RecordingOp(dialect)
        monkeypatch.setattr(migration, "op", op)
        migration.upgrade()
        assert op.executed == expected


def test_backfill_labels_by_sic_and_leaves_unknowns_and_set_rows_alone(monkeypatch):
    """The statement itself, run on SQLite, whose ``->>`` reads a JSON key as Postgres does."""
    migration = import_module(MIGRATION)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        _ipo_table(monkeypatch, connection)
        _insert(connection, "spac", "sec_edgar", {"sic": "6770"})
        _insert(connection, "operating", "sec_edgar", {"sic": "7372"})
        _insert(connection, "no-sic", "sec_edgar", {"sic": None})
        _insert(connection, "no-raw", "sec_edgar", None)
        _insert(connection, "other-source", "finnhub", {"sic": "6770"})
        _insert(connection, "already-set", "sec_edgar", {"sic": "6770"}, is_spac=False)
        connection.execute(sa.text(migration.BACKFILL))
        rows = dict(
            connection.execute(sa.text("SELECT external_id, is_spac FROM ipo_events")).all()
        )

    assert rows == {
        "spac": 1,
        "operating": 0,
        "no-sic": None,
        "no-raw": None,
        "other-source": None,
        "already-set": 0,
    }


def test_downgrade_is_a_no_op(monkeypatch):
    migration = import_module(MIGRATION)
    op = _RecordingOp("postgresql")
    monkeypatch.setattr(migration, "op", op)
    migration.downgrade()
    assert op.executed == []
