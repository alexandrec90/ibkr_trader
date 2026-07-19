"""Tests for the dashboard data layer (dashboard/data.py) — hermetic, in-memory SQLite.

The Streamlit page itself (dashboard/app.py) is a thin rendering shell over these helpers and
needs the [dashboard] extra; see test_cli.py for the `ibkr-trader dashboard` launcher tests.
"""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from ibkr_trader.dashboard import data as dash
from ibkr_trader.db.models import BacktestRun, Base


def _session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _add_run(
    session,
    *,
    strategy="momentum_lt",
    account="tfsa",
    sharpe=1.0,
    curve=None,
    bench_curve=None,
    created=datetime(2026, 7, 1, tzinfo=UTC),
):
    metrics = {"sharpe": sharpe, "total_return": 0.5, "cagr": 0.1}
    if curve is not None:
        metrics["equity_curve"] = curve
    if bench_curve is not None:
        metrics["benchmark_equity_curve"] = bench_curve
    run = BacktestRun(
        strategy=strategy,
        params={
            "account": account,
            "benchmark": "XEQT",
            "model_version": "v3",
            "universe": {"n_symbols": 24, "sha256_16": "abcd", "survivorship": "curated-current"},
        },
        start=datetime(2010, 1, 4, tzinfo=UTC),
        end=datetime(2026, 7, 1, tzinfo=UTC),
        metrics=metrics,
        created_at=created,
    )
    session.add(run)
    session.commit()
    return run


CURVE = [["2010-01-04", 100000.0], ["2010-01-05", 101000.0], ["2010-01-06", 99000.0]]
BENCH = [["2010-01-04", 100000.0], ["2010-01-05", 100500.0], ["2010-01-06", 100200.0]]


def test_runs_frame_flattens_params_metrics_and_flags_curves():
    session = _session()
    with_curve = _add_run(session, curve=CURVE, bench_curve=BENCH)
    legacy = _add_run(session, strategy="ml_lt_ridge", created=datetime(2026, 7, 2, tzinfo=UTC))

    frame = dash.runs_frame(session)

    assert list(frame["run_id"]) == [legacy.id, with_curve.id]  # newest first
    assert set(dash.SUMMARY_METRICS).issubset(frame.columns)
    by_id = frame.set_index("run_id")
    assert by_id.loc[with_curve.id, "has_curve"]
    assert not by_id.loc[legacy.id, "has_curve"]
    assert by_id.loc[with_curve.id, "account"] == "tfsa"
    assert by_id.loc[with_curve.id, "universe_n"] == 24
    assert by_id.loc[with_curve.id, "window"] == "2010-01-04 → 2026-07-01"


def test_curve_frame_returns_long_format_with_benchmark_kind():
    session = _session()
    run = _add_run(session, curve=CURVE, bench_curve=BENCH)
    legacy = _add_run(session)  # no curve → contributes nothing

    curves = dash.curve_frame(session, [run.id, legacy.id])

    assert set(curves["kind"]) == {"strategy", "benchmark"}
    strat = curves[curves["kind"] == "strategy"]
    assert len(strat) == 3
    assert strat["date"].tolist() == [date(2010, 1, 4), date(2010, 1, 5), date(2010, 1, 6)]
    assert strat["equity_cad"].tolist() == [100000.0, 101000.0, 99000.0]
    bench = curves[curves["kind"] == "benchmark"]
    assert bench["series"].iloc[0] == f"XEQT #{run.id}"
    assert (curves["run_id"] == run.id).all()


def test_curve_frame_empty_selection_gives_typed_empty_frame():
    session = _session()
    curves = dash.curve_frame(session, [999])
    assert curves.empty
    assert list(curves.columns) == ["date", "equity_cad", "series", "kind", "run_id"]


def test_rebase_sets_each_series_first_point_to_base():
    session = _session()
    run = _add_run(session, curve=CURVE, bench_curve=BENCH)
    curves = dash.curve_frame(session, [run.id])

    rebased = dash.rebase(curves)

    for _, block in rebased.groupby("series"):
        assert block.sort_values("date")["equity_cad"].iloc[0] == pytest.approx(100.0)
    strat = rebased[rebased["kind"] == "strategy"].sort_values("date")
    assert strat["equity_cad"].iloc[1] == pytest.approx(101.0)  # +1% preserved


def test_drawdown_is_zero_at_peaks_and_negative_after():
    session = _session()
    run = _add_run(session, curve=CURVE, bench_curve=BENCH)
    curves = dash.curve_frame(session, [run.id])

    dd = dash.drawdown(curves[curves["kind"] == "strategy"]).sort_values("date")

    assert dd["drawdown"].tolist() == pytest.approx([0.0, 0.0, 99000.0 / 101000.0 - 1.0])


def test_read_universe_file(tmp_path):
    good = tmp_path / "u.txt"
    good.write_text("xiu\n\n spy \n", encoding="utf-8")
    assert dash.read_universe_file(str(good)) == ["XIU", "SPY"]

    empty = tmp_path / "empty.txt"
    empty.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty universe"):
        dash.read_universe_file(str(empty))
    with pytest.raises(ValueError, match="cannot read"):
        dash.read_universe_file(str(tmp_path / "missing.txt"))


def test_run_backtest_builds_cli_equivalent_config(monkeypatch, tmp_path):
    from ibkr_trader.backtest import engine as engine_mod

    universe_file = tmp_path / "u.txt"
    universe_file.write_text("XIU\nSPY\n", encoding="utf-8")
    seen = {}

    def fake_run(self, strategy, universe, start, end, *, config, persist=True):
        seen.update(
            strategy=strategy,
            universe=universe,
            start=start,
            end=end,
            account=config.account.value,
            eval_start=config.eval_start,
            floor=config.eligibility.min_history_days,
            persist=persist,
        )
        return "result-sentinel"

    monkeypatch.setattr(engine_mod.BacktestEngine, "run", fake_run)

    result = dash.run_backtest(
        "momentum_lt",
        str(universe_file),
        "rrsp",
        date(2008, 6, 2),
        date(2026, 7, 1),
        eval_start=date(2010, 1, 4),
        min_history_days=63,
    )

    assert result == "result-sentinel"
    assert seen["strategy"] == "momentum_lt"
    assert seen["universe"] == ["XIU", "SPY"]
    assert seen["start"] == datetime(2008, 6, 2, tzinfo=UTC)
    assert seen["end"] == datetime(2026, 7, 1, tzinfo=UTC)
    assert seen["account"] == "rrsp"
    assert seen["eval_start"] == date(2010, 1, 4)
    assert seen["floor"] == 63
    assert seen["persist"] is True  # dashboard runs always persist (that's their point)


def test_run_backtest_rejects_unknown_strategy_and_bad_eval_start(tmp_path):
    universe_file = tmp_path / "u.txt"
    universe_file.write_text("XIU\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unknown allocator"):
        dash.run_backtest("nope", str(universe_file), "tfsa", date(2010, 1, 1), date(2020, 1, 1))

    with pytest.raises(ValueError, match="warm-up"):
        dash.run_backtest(
            "momentum_lt",
            str(universe_file),
            "tfsa",
            date(2010, 1, 1),
            date(2020, 1, 1),
            eval_start=date(2010, 1, 1),
        )
