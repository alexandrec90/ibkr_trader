"""Database access is injected into the connector tree, not reached for.

``Connector.session()`` / :func:`resolve_session_factory` replaced the per-module
``get_session()`` calls, so ``ingestion/`` owns no engine and carries no import-time
dependency on ``ibkr_trader.db.session``. That was the last hard coupling blocking Phase 2 of
docs/plans/active/data-lake.md — a foreign consumer supplies its own session factory (and its
own engine) when the package moves.

Companion to ``tests/test_connector_settings.py``, which pins the same contract for config.
"""

import ast
import pathlib
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ibkr_trader.db.models import Base, PriceBar
from ibkr_trader.ingestion.base import Connector, resolve_session_factory
from ibkr_trader.ingestion.market import alpha_vantage as av
from ibkr_trader.ingestion.market import yahoo_fx
from ibkr_trader.ingestion.news import newsapi

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "ibkr_trader"


def _sqlite_session_factory():
    """A factory over throwaway in-memory SQLite — what a foreign consumer would hand in."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def scope():
        session = factory()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    return scope


class DummyConnector(Connector):
    name = "dummy"

    def fetch(self, **kwargs) -> int:
        return 0


def _exploding_factory():  # pragma: no cover - must never run
    raise AssertionError("the injected session factory must be used")


# --- injection -----------------------------------------------------------------------


def test_injected_factory_is_used_verbatim():
    factory = _sqlite_session_factory()
    assert DummyConnector(session_factory=factory).session_factory is factory


def test_session_opens_a_transaction_from_the_injected_factory():
    factory = _sqlite_session_factory()
    connector = DummyConnector(session_factory=factory)

    with connector.session() as session:
        session.add(
            PriceBar(
                instrument_id=1,
                ts=datetime(2026, 1, 2, tzinfo=UTC),
                bar_size="1 day",
                source="dummy",
                what_to_show="TRADES",
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
            )
        )

    with factory() as session:  # committed on clean exit, visible to the next session
        assert session.scalar(select(PriceBar)) is not None


def test_settings_and_session_factory_are_independent():
    settings = SimpleNamespace(newsapi_key="k")
    factory = _sqlite_session_factory()
    connector = DummyConnector(settings, session_factory=factory)
    assert connector.settings is settings
    assert connector.session_factory is factory


def test_injected_factory_never_touches_the_default(monkeypatch):
    monkeypatch.setattr("ibkr_trader.db.session.get_session", _exploding_factory)
    connector = DummyConnector(session_factory=_sqlite_session_factory())
    with connector.session():
        pass


# --- fallback ------------------------------------------------------------------------


def test_fallback_is_lazy_and_resolved_once(monkeypatch):
    """Constructing must not resolve the default factory; first use does, and caches it."""
    import ibkr_trader.db.session as session_mod

    calls: list[int] = []
    sentinel = _sqlite_session_factory()

    def counting_factory():
        calls.append(1)
        return sentinel()

    monkeypatch.setattr(session_mod, "get_session", counting_factory)

    connector = DummyConnector()
    assert calls == []  # construction alone resolves nothing

    first = connector.session_factory
    assert connector.session_factory is first
    assert calls == []  # resolving the factory does not open a session either

    with connector.session():
        pass
    assert calls == [1]


def test_resolve_session_factory_returns_the_process_default():
    from ibkr_trader.db.session import get_session

    assert resolve_session_factory(None) is get_session
    assert resolve_session_factory(get_session) is get_session


def test_resolve_session_factory_prefers_the_injected_one():
    factory = _sqlite_session_factory()
    assert resolve_session_factory(factory) is factory


# --- module-level helpers take the same argument ---------------------------------------


def test_module_level_helpers_accept_a_session_factory():
    """The helpers that own their own session must be injectable too, not just connectors."""
    factory = _sqlite_session_factory()

    assert yahoo_fx._next_missing_date("USDCAD", factory) is None
    assert newsapi.fresh_tagged_symbols(datetime.now(UTC), session_factory=factory) == set()
    assert av.fetch_universe([], sleep=lambda _s: None, session_factory=factory) == 0


def test_fetch_universe_hands_its_factory_to_the_connector_it_builds(monkeypatch):
    """A helper that constructs a connector must pass the factory down, or the connector
    would silently fall back to the process engine."""
    factory = _sqlite_session_factory()
    seen: list[object] = []

    def fake_fetch(self, symbol="", **kwargs):
        seen.append(self.session_factory)
        return 1

    monkeypatch.setattr(av.AlphaVantageConnector, "fetch", fake_fetch)
    monkeypatch.setattr("ibkr_trader.db.session.get_session", _exploding_factory)

    assert av.fetch_universe(["NVDA"], sleep=lambda _s: None, session_factory=factory) == 1
    assert seen == [factory]


def test_run_backfill_hands_its_factory_to_the_connector_it_builds(monkeypatch):
    from ibkr_trader.ingestion.news import finnhub_backfill
    from ibkr_trader.ingestion.news.finnhub_news import FinnhubNewsConnector

    factory = _sqlite_session_factory()
    seen: list[object] = []

    def fake_fetch(self, symbol="", date_from="", date_to="", **kwargs):
        seen.append(self.session_factory)
        return 0

    monkeypatch.setattr(FinnhubNewsConnector, "fetch", fake_fetch)
    monkeypatch.setattr("ibkr_trader.db.session.get_session", _exploding_factory)

    finnhub_backfill.run_backfill(
        ["AAPL"], backfill_days=30, sleep=lambda _s: None, session_factory=factory
    )
    assert seen and set(seen) == {factory}


# --- the extraction guardrail ----------------------------------------------------------


@pytest.mark.parametrize(
    "module_path",
    sorted(
        p.relative_to(SRC).as_posix()
        for p in (SRC / "ingestion").rglob("*.py")
        if p.name != "base.py"
    ),
)
def test_no_connector_module_imports_db_session(module_path):
    """Only base.py may reference ``db.session``, and only behind a lazy import."""
    tree = ast.parse((SRC / module_path).read_text(encoding="utf-8"))
    offenders = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "ibkr_trader.db.session"
    ]
    assert offenders == [], f"{module_path} still imports ibkr_trader.db.session directly"


def test_base_module_has_no_import_time_session_dependency():
    """``ingestion.base`` must import db.session only inside a function."""
    tree = ast.parse((SRC / "ingestion" / "base.py").read_text(encoding="utf-8"))
    for node in tree.body:  # module-level statements only
        assert not (isinstance(node, ast.ImportFrom) and node.module == "ibkr_trader.db.session"), (
            "ingestion.base imports db.session at module scope — breaks Phase 2 extraction"
        )


def test_importing_the_connector_tree_loads_neither_engine_nor_config():
    """The whole point, checked end-to-end: importing every connector must not drag in
    ``db.session`` (which owns the engine) or ``config`` — not even transitively through
    ``db.models``'s package ``__init__``. Runs in a subprocess: this test session has both
    modules loaded already."""
    program = (
        "import importlib, pkgutil, sys\n"
        "import ibkr_trader.ingestion as pkg\n"
        "for mod in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + '.'):\n"
        "    importlib.import_module(mod.name)\n"
        "leaked = [m for m in ('ibkr_trader.db.session', 'ibkr_trader.config')"
        " if m in sys.modules]\n"
        "print(','.join(leaked))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "", f"connector tree leaked: {result.stdout.strip()}"


def test_db_package_still_re_exports_lazily():
    """The laziness that keeps the engine out of the import graph must not break the
    documented ``from ibkr_trader.db import Base, get_session`` surface."""
    import ibkr_trader.db as db
    from ibkr_trader.db.base import Base as RealBase
    from ibkr_trader.db.session import get_engine as real_get_engine
    from ibkr_trader.db.session import get_session as real_get_session

    assert db.Base is RealBase
    assert db.get_session is real_get_session
    assert db.get_engine is real_get_engine
    with pytest.raises(AttributeError):
        getattr(db, "not_a_real_attribute")  # noqa: B009 - the point is the lazy __getattr__
