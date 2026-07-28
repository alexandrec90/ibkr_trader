"""Connector configuration is injected, not reached for.

``Connector.settings`` replaced the per-module ``get_settings()`` calls so the connector tree
carries no import-time dependency on ``ibkr_trader.config`` — the property resolves lazily and
an explicit settings object always wins. That is what lets the ``data-lake`` package take
``ingestion/`` in Phase 2 (docs/plans/active/data-lake.md) with a foreign consumer supplying
its own config.
"""

import ast
import pathlib
from types import SimpleNamespace

import pytest

from ibkr_trader.ingestion.base import Connector

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "ibkr_trader"


class DummyConnector(Connector):
    name = "dummy"

    def fetch(self, **kwargs) -> int:
        return 0


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


def test_base_module_has_no_import_time_config_dependency():
    """``ingestion.base`` must import config only inside a function or TYPE_CHECKING block."""
    tree = ast.parse((SRC / "ingestion" / "base.py").read_text(encoding="utf-8"))
    for node in tree.body:  # module-level statements only
        assert not (isinstance(node, ast.ImportFrom) and node.module == "ibkr_trader.config"), (
            "ingestion.base imports config at module scope — breaks Phase 2 extraction"
        )
