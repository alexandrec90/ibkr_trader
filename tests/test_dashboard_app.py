"""Tests for the Streamlit page (dashboard/app.py) via streamlit's headless AppTest.

Needs the [dashboard] extra (importorskip'd) — CI installs it. The page runs against a
monkeypatched in-memory SQLite session; no Postgres, no browser.
"""

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

pytest.importorskip("streamlit")
pytest.importorskip("plotly")

from pathlib import Path  # noqa: E402

from streamlit.testing.v1 import AppTest  # noqa: E402

import ibkr_trader.dashboard  # noqa: E402
import ibkr_trader.db.session as db_session  # noqa: E402
from ibkr_trader.dashboard import data as data_mod  # noqa: E402
from ibkr_trader.db.models import BacktestRun, Base  # noqa: E402

APP_PATH = str(Path(ibkr_trader.dashboard.__file__).parent / "app.py")

CURVE = [["2010-01-04", 100000.0], ["2010-01-05", 101000.0], ["2010-01-06", 99000.0]]
BENCH = [["2010-01-04", 100000.0], ["2010-01-05", 100500.0], ["2010-01-06", 100200.0]]


def _session_scope():
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


def _add_run(scope, *, strategy="momentum_lt", curve=CURVE, bench_curve=BENCH):
    with scope() as session:
        metrics = {"sharpe": 1.2, "total_return": 0.5, "cagr": 0.1}
        if curve is not None:
            metrics["equity_curve"] = curve
            metrics["benchmark_equity_curve"] = bench_curve
        session.add(
            BacktestRun(
                strategy=strategy,
                params={"account": "tfsa", "benchmark": "XEQT", "model_version": "v3"},
                start=datetime(2010, 1, 4, tzinfo=UTC),
                end=datetime(2026, 7, 1, tzinfo=UTC),
                metrics=metrics,
                created_at=datetime(2026, 7, 1, tzinfo=UTC),
            )
        )


@pytest.fixture(autouse=True)
def clear_streamlit_caches():
    """@st.cache_data storage is process-global and keyed on function source, so a cached
    (e.g. empty) runs frame from one AppTest would leak into the next test's page run."""
    import streamlit as st

    st.cache_data.clear()
    yield
    st.cache_data.clear()


def _app(monkeypatch, scope) -> AppTest:
    # app.py re-executes `from ibkr_trader.db.session import get_session` on every AppTest
    # run, so patching the *source* module redirects the page to the SQLite fixture. Each
    # AppTest gets a fresh runtime, so the @st.cache_data caches are test-isolated.
    monkeypatch.setattr(db_session, "get_session", scope)
    return AppTest.from_file(APP_PATH)


def test_empty_db_shows_hint_and_run_launcher(monkeypatch):
    at = _app(monkeypatch, _session_scope()).run(timeout=30)

    assert not at.exception
    assert any("No persisted backtest runs" in info.value for info in at.info)
    assert len(at.selectbox) >= 2  # launcher form still offered (strategy + account)


def test_leaderboard_charts_and_filters_render(monkeypatch):
    scope = _session_scope()
    _add_run(scope, strategy="momentum_lt")
    _add_run(scope, strategy="ml_lt_ridge")
    _add_run(scope, strategy="legacy_no_curve", curve=None)

    at = _app(monkeypatch, scope).run(timeout=30)

    assert not at.exception
    assert at.title[0].value == "Backtest results"
    # leaderboard holds all three runs; curve picker defaults to the plottable ones
    leaderboard = at.dataframe[0].value
    assert len(leaderboard) == 3
    strategies_filter = at.sidebar.multiselect[0]
    assert set(strategies_filter.value) == {"momentum_lt", "ml_lt_ridge", "legacy_no_curve"}
    # at.multiselect mixes the sidebar filters in — find the curve picker by label
    picker = next(m for m in at.multiselect if m.label == "runs to plot")
    assert len(picker.value) == 2  # only runs with stored curves are plottable
    # rebase toggle defaulted on (2 curves picked) and both charts rendered exception-free
    assert at.toggle[0].value is True


def test_strategy_filter_narrows_leaderboard(monkeypatch):
    scope = _session_scope()
    _add_run(scope, strategy="momentum_lt")
    _add_run(scope, strategy="ml_lt_ridge")

    at = _app(monkeypatch, scope).run(timeout=30)
    at.sidebar.multiselect[0].set_value(["ml_lt_ridge"]).run(timeout=30)

    assert not at.exception
    leaderboard = at.dataframe[0].value
    assert list(leaderboard["strategy"]) == ["ml_lt_ridge"]


def test_run_launcher_reports_success_and_error(monkeypatch):
    scope = _session_scope()
    _add_run(scope)

    class FakeResult:
        run_id = 77
        metrics = {"total_return": 0.42, "excess_return": 0.1}

    calls = {}

    def fake_run_backtest(strategy, universe_file, account, start, end, **kwargs):
        calls["strategy"] = strategy
        calls["universe_file"] = universe_file
        return FakeResult()

    monkeypatch.setattr(data_mod, "run_backtest", fake_run_backtest)
    at = _app(monkeypatch, scope).run(timeout=30)
    at.button[0].set_value(True).run(timeout=30)

    assert not at.exception
    assert any("run #77" in s.value for s in at.success)
    assert calls["universe_file"] == "tickers.txt"

    def broken_run_backtest(*args, **kwargs):
        raise ValueError("no daily bars found")

    monkeypatch.setattr(data_mod, "run_backtest", broken_run_backtest)
    at = _app(monkeypatch, scope).run(timeout=30)
    at.button[0].set_value(True).run(timeout=30)

    assert not at.exception
    assert any("no daily bars found" in e.value for e in at.error)
