"""Connector *ambient dependencies* are injected, not reached for — config and DB access both.

``Connector.settings`` replaced the per-module ``get_settings()`` calls and
``Connector.session()`` replaced the per-module ``get_session()`` calls, so the connector tree
carries no import-time dependency on ``ibkr_trader.config`` or ``ibkr_trader.db.session`` — both
properties resolve lazily and an explicitly injected object always wins. That is what lets the
``data-lake`` package take ``ingestion/`` in Phase 2 (docs/plans/active/data-lake.md) with a
foreign consumer supplying its own config and its own session factory over its own engine.
"""

import ast
import inspect
import pathlib
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from ibkr_trader.ingestion.base import Connector, resolve_session_factory

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "ibkr_trader"

#: Module-level helpers in the connector tree that read the database directly. Each must accept
#: an injected factory too, or the extracted package would still reach for this repo's engine.
DB_TOUCHING_HELPERS = [
    ("ibkr_trader.ingestion.market.alpha_vantage", "fetch_universe"),
    ("ibkr_trader.ingestion.news.finnhub_backfill", "run_backfill"),
    ("ibkr_trader.ingestion.news.newsapi", "fresh_tagged_symbols"),
]


class DummyConnector(Connector):
    name = "dummy"

    def fetch(self, **kwargs) -> int:
        return 0


def _fake_factory(marker="session"):
    """A minimal stand-in for ``get_session``: a zero-arg callable yielding a context manager."""

    @contextmanager
    def factory():
        yield marker

    return factory


def test_injected_settings_are_used_verbatim():
    injected = SimpleNamespace(newsapi_key="injected")
    assert DummyConnector(injected).settings is injected


def test_falls_back_to_process_settings_when_none_injected(monkeypatch):
    sentinel = SimpleNamespace(newsapi_key="from-process")
    monkeypatch.setattr("ibkr_trader.config.get_settings", lambda: sentinel)
    assert DummyConnector().settings is sentinel


def test_fallback_is_lazy_and_resolved_once(monkeypatch):
    """Constructing must not touch config; the first access resolves and caches it."""
    calls = []

    def fake_get_settings():
        calls.append(1)
        return SimpleNamespace(newsapi_key="x")

    monkeypatch.setattr("ibkr_trader.config.get_settings", fake_get_settings)
    connector = DummyConnector()
    assert calls == []  # construction alone resolves nothing

    first = connector.settings
    assert connector.settings is first
    assert len(calls) == 1


def test_injected_settings_never_call_the_process_getter(monkeypatch):
    def explode():  # pragma: no cover - must never run
        raise AssertionError("injected settings must not fall back to get_settings()")

    monkeypatch.setattr("ibkr_trader.config.get_settings", explode)
    assert DummyConnector(SimpleNamespace(k=1)).settings.k == 1


def test_injected_session_factory_is_used_verbatim():
    factory = _fake_factory()
    assert DummyConnector(session_factory=factory).session_factory is factory


def test_session_opens_the_injected_factory():
    connector = DummyConnector(session_factory=_fake_factory("injected-session"))
    with connector.session() as session:
        assert session == "injected-session"


def test_session_factory_falls_back_to_this_repos_get_session(monkeypatch):
    sentinel = _fake_factory()
    monkeypatch.setattr("ibkr_trader.db.session.get_session", sentinel)
    assert DummyConnector().session_factory is sentinel


def test_session_factory_fallback_is_lazy_and_resolved_once(monkeypatch):
    """Constructing resolves nothing; the first access resolves and then caches."""
    connector = DummyConnector()

    first = _fake_factory("first")
    monkeypatch.setattr("ibkr_trader.db.session.get_session", first)
    # patched *after* construction and still picked up => construction resolved nothing
    assert connector.session_factory is first

    monkeypatch.setattr("ibkr_trader.db.session.get_session", _fake_factory("second"))
    assert connector.session_factory is first  # cached, not re-resolved per access


def test_injected_session_factory_never_touches_the_repo_engine(monkeypatch):
    def explode():  # pragma: no cover - must never run
        raise AssertionError("an injected session factory must not fall back to get_session()")

    monkeypatch.setattr("ibkr_trader.db.session.get_session", explode)
    factory = _fake_factory()
    with DummyConnector(session_factory=factory).session() as session:
        assert session == "session"


def test_resolve_session_factory_prefers_the_argument(monkeypatch):
    factory = _fake_factory()
    monkeypatch.setattr("ibkr_trader.db.session.get_session", _fake_factory("process-wide"))
    assert resolve_session_factory(factory) is factory


def test_resolve_session_factory_falls_back_to_the_process_wide_one(monkeypatch):
    sentinel = _fake_factory("process-wide")
    monkeypatch.setattr("ibkr_trader.db.session.get_session", sentinel)
    assert resolve_session_factory(None) is sentinel


@pytest.mark.parametrize("module_name,func_name", DB_TOUCHING_HELPERS)
def test_db_touching_helpers_accept_an_injected_session_factory(module_name, func_name):
    """Batch entrypoints are injectable too — not just the connector classes."""
    import importlib

    func = getattr(importlib.import_module(module_name), func_name)
    parameter = inspect.signature(func).parameters.get("session_factory")
    assert parameter is not None, f"{module_name}.{func_name} cannot be given a session factory"
    assert parameter.default is None, "the injected factory must be optional, defaulting to None"


@pytest.mark.parametrize(
    "module_path",
    sorted(
        p.relative_to(SRC).as_posix()
        for p in (SRC / "ingestion").rglob("*.py")
        if p.name != "base.py"
    ),
)
def test_no_connector_module_imports_config_at_module_scope(module_path):
    """Only base.py may reference config, and only behind a lazy/TYPE_CHECKING import."""
    tree = ast.parse((SRC / module_path).read_text(encoding="utf-8"))
    offenders = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "ibkr_trader.config"
    ]
    assert offenders == [], f"{module_path} still imports ibkr_trader.config directly"


@pytest.mark.parametrize(
    "module_path",
    sorted(
        p.relative_to(SRC).as_posix()
        for p in (SRC / "ingestion").rglob("*.py")
        if p.name != "base.py"
    ),
)
def test_no_connector_module_reaches_for_the_session_factory(module_path):
    """Only base.py may name ``db.session`` — everything else takes what it is given.

    A module that imports ``get_session`` (at any scope) owns this repo's engine and would keep
    owning it inside the shared package, which is exactly the coupling Phase 2 has to remove.
    """
    tree = ast.parse((SRC / module_path).read_text(encoding="utf-8"))
    offenders = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "ibkr_trader.db.session"
    }
    assert offenders == set(), f"{module_path} still imports ibkr_trader.db.session"


@pytest.mark.parametrize("dependency", ["ibkr_trader.config", "ibkr_trader.db.session"])
def test_base_module_has_no_import_time_repo_dependency(dependency):
    """``ingestion.base`` may import these only inside a function or TYPE_CHECKING block."""
    tree = ast.parse((SRC / "ingestion" / "base.py").read_text(encoding="utf-8"))
    for node in tree.body:  # module-level statements only
        assert not (isinstance(node, ast.ImportFrom) and node.module == dependency), (
            f"ingestion.base imports {dependency} at module scope — breaks Phase 2 extraction"
        )
