"""Tests for the static HTML report generator (dashboard/report.py).

Hermetic: in-memory SQLite + synthetic runs, no server, no browser. Needs the [report] extra
(plotly), so the whole module importorskips it — CI installs `--extra report`.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

pytest.importorskip("plotly")

from ibkr_trader.dashboard import data as dash  # noqa: E402
from ibkr_trader.dashboard import report as rpt  # noqa: E402
from ibkr_trader.db.models import BacktestRun, Base  # noqa: E402

CURVE = [["2010-01-04", 100000.0], ["2010-01-05", 101000.0], ["2010-01-06", 99000.0]]
BENCH = [["2010-01-04", 100000.0], ["2010-01-05", 100500.0], ["2010-01-06", 100200.0]]
STAMP = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


def _session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _add_run(session, *, strategy="momentum_lt", sharpe=1.0, curve=None, bench_curve=None):
    metrics = {"sharpe": sharpe, "total_return": 0.5, "cagr": 0.1}
    if curve is not None:
        metrics["equity_curve"] = curve
    if bench_curve is not None:
        metrics["benchmark_equity_curve"] = bench_curve
    run = BacktestRun(
        strategy=strategy,
        params={"account": "tfsa", "benchmark": "XEQT", "model_version": "v3"},
        start=datetime(2010, 1, 4, tzinfo=UTC),
        end=datetime(2026, 7, 1, tzinfo=UTC),
        metrics=metrics,
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    session.add(run)
    session.commit()
    return run


def test_empty_db_renders_no_runs_page():
    html = rpt.build_report_from_db(_session(), generated_at=STAMP)
    assert "Backtest results" in html
    assert "No persisted backtest runs yet" in html
    assert "0 runs" in html
    assert "Plotly" not in html  # no chart library pulled in when there is nothing to plot


def test_runs_with_curves_embed_plotly_and_both_charts():
    session = _session()
    _add_run(session, strategy="momentum_lt", curve=CURVE, bench_curve=BENCH)

    html = rpt.build_report_from_db(session, generated_at=STAMP)

    assert "1 run" in html  # singular
    assert "momentum_lt" in html  # leaderboard row
    assert "Equity curves" in html and "Drawdown" in html
    assert "Plotly.newPlot" in html  # plotly.js inlined → self-contained, offline-capable


def test_single_series_defaults_to_raw_cad_multi_series_rebases():
    single = _session()
    _add_run(single, curve=CURVE)  # one strategy, no benchmark → 1 series
    html_single = rpt.build_report_from_db(single, generated_at=STAMP)
    assert "portfolio value (CAD)" in html_single
    assert "growth of 100" not in html_single

    multi = _session()
    _add_run(multi, curve=CURVE, bench_curve=BENCH)  # strategy + benchmark → 2 series
    html_multi = rpt.build_report_from_db(multi, generated_at=STAMP)
    assert "growth of 100" in html_multi  # rebase kicked in by default


def test_rebase_flag_overrides_the_default():
    session = _session()
    _add_run(session, curve=CURVE, bench_curve=BENCH)  # would rebase by default
    html = rpt.build_report_from_db(session, rebased=False, generated_at=STAMP)
    assert "portfolio value (CAD)" in html
    assert "growth of 100" not in html


def test_runs_without_curves_show_leaderboard_but_note_instead_of_charts():
    session = _session()
    _add_run(session, strategy="legacy_no_curve")  # no equity_curve stored

    html = rpt.build_report_from_db(session, generated_at=STAMP)

    assert "legacy_no_curve" in html  # still on the leaderboard
    assert "None of the runs stored an equity curve" in html
    assert "Plotly" not in html


def test_leaderboard_html_formats_numbers_and_escapes():
    session = _session()
    _add_run(session, sharpe=1.234, curve=CURVE)
    runs = dash.runs_frame(session)

    table = rpt.leaderboard_html(runs)

    assert "1.23" in table  # sharpe formatted to 2 decimals
    assert "0.50" in table  # total_return
    assert "<table" in table and "leaderboard" in table  # styled via the .leaderboard class


def test_equity_chart_styles_strategies_and_benchmarks_distinctly():
    session = _session()
    run = _add_run(session, curve=CURVE, bench_curve=BENCH)
    curves = dash.curve_frame(session, [run.id])

    fig = rpt.equity_chart(curves, rebased=False)

    assert len(fig.data) == 2  # one strategy trace + one benchmark trace
    strategy_trace, benchmark_trace = fig.data
    assert strategy_trace.line.color == rpt.SERIES_COLORS[0]
    assert benchmark_trace.line.color == rpt.BENCHMARK_COLOR
    assert benchmark_trace.line.dash == "dash"


def test_drawdown_chart_hides_legend_for_a_single_series():
    session = _session()
    run = _add_run(session, curve=CURVE, bench_curve=BENCH)
    curves = dash.curve_frame(session, [run.id])

    fig = rpt.drawdown_chart(curves)  # benchmarks excluded → one strategy series

    assert len(fig.data) == 1
    assert fig.layout.showlegend is False
    assert fig.data[0].line.color == rpt.DRAWDOWN_COLOR
