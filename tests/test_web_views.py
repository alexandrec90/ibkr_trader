"""The UI's page data (web/views.py): hermetic, in-memory SQLite, no [ui] extra needed."""

import json
import math
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ibkr_trader.db.models import (
    BacktestRun,
    Base,
    Execution,
    Instrument,
    Order,
    OrderSide,
    PriceBar,
    StrategySnapshot,
)
from ibkr_trader.web import views

CURVE = [["2010-01-04", 200000.0], ["2010-01-05", 220000.0], ["2010-01-06", 198000.0]]
BENCH = [["2010-01-04", 100000.0], ["2010-01-05", 100500.0], ["2010-01-06", 100200.0]]


@pytest.fixture
def session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as value:
        yield value


def _midnight(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=UTC)


def _run(session: Session, *, curve=None, sharpe=1.0) -> BacktestRun:
    metrics = {"sharpe": sharpe, "cagr": 0.1}
    if curve is not None:
        metrics |= {"equity_curve": curve, "benchmark_equity_curve": BENCH}
    run = BacktestRun(
        strategy="momentum_lt",
        params={"account": "tfsa", "benchmark": "XEQT"},
        start=datetime(2010, 1, 4, tzinfo=UTC),
        end=datetime(2010, 1, 6, tzinfo=UTC),
        metrics=metrics,
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    session.add(run)
    session.flush()
    return run


def _instrument(session: Session, symbol: str) -> Instrument:
    row = Instrument(
        symbol=symbol,
        exchange="SMART",
        currency="CAD",
        sec_type="STK",
        name=symbol,
        asset_class="ETF",
        leveraged=False,
    )
    session.add(row)
    session.flush()
    return row


def _snapshot(session: Session, strategy: str, day: date, weights: dict[int, float]) -> None:
    session.add(
        StrategySnapshot(
            strategy=strategy,
            model_version="v1",
            feature_set_version="1",
            ts=_midnight(day),
            weights={str(k): v for k, v in weights.items()},
            params={"bar_asof": day.isoformat()},
            created_at=datetime.now(tz=UTC),
        )
    )
    session.flush()


def _bar(session: Session, instrument: Instrument, day: date, close: float) -> None:
    session.add(
        PriceBar(
            instrument_id=instrument.id,
            ts=_midnight(day),
            bar_size="1 day",
            source="yahoo",
            what_to_show="ADJUSTED_LAST",
            open=close,
            high=close,
            low=close,
            close=close,
            volume=1_000_000,
        )
    )


# --- backtests ------------------------------------------------------------------------


def test_runs_are_empty_on_an_empty_database(session):
    assert views.backtest_runs(session) == []


def test_runs_are_json_safe_with_nan_metrics(session):
    _run(session, sharpe=math.nan)

    (row,) = views.backtest_runs(session)

    assert row["sharpe"] is None
    assert row["created_at"].startswith("2026-07-01")
    json.dumps(row, allow_nan=False)  # what the browser receives must parse as JSON


def test_curves_are_rebased_to_100_with_drawdown_and_their_benchmark(session):
    run = _run(session, curve=CURVE)

    curves = {c["kind"]: c for c in views.backtest_curves(session, [run.id])}

    strategy = curves["strategy"]["points"]
    assert [p[1] for p in strategy] == [100.0, 110.0, 99.0]  # growth of 100, not CAD
    assert strategy[-1][2] == pytest.approx(99 / 110 - 1)  # drawdown from the 110 peak
    assert strategy[0][0] == "2010-01-04"
    assert curves["benchmark"]["label"] == f"XEQT #{run.id}"
    assert curves["strategy"]["run_id"] == run.id


def test_identical_benchmarks_are_listed_once(session):
    first, second = _run(session, curve=CURVE), _run(session, curve=CURVE)

    curves = views.backtest_curves(session, [first.id, second.id])

    assert [c["kind"] for c in curves].count("benchmark") == 1
    assert {c["run_id"] for c in curves if c["kind"] == "strategy"} == {first.id, second.id}


def test_long_curves_are_sampled_on_one_shared_grid_keeping_the_last_date(session):
    start = date(2010, 1, 4)
    long = [[(start + timedelta(days=i)).isoformat(), 100.0 + i] for i in range(10)]
    short = long[4:]  # a later window: its dates are a subset of the first run's
    a, b = _run(session, curve=long), _run(session, curve=short)

    curves = views.backtest_curves(session, [a.id, b.id], max_points=4)

    dates = {c["run_id"]: [p[0] for p in c["points"]] for c in curves if c["kind"] == "strategy"}
    assert dates[a.id] == [long[i][0] for i in (0, 3, 6, 9)]  # stride 3, last kept
    assert set(dates[b.id]) <= set(dates[a.id])  # same x values, so the rows line up
    assert dates[b.id][-1] == long[-1][0]


def test_runs_without_a_stored_curve_contribute_nothing(session):
    run = _run(session)
    assert views.backtest_curves(session, [run.id]) == []


def test_more_runs_than_palette_slots_are_refused(session):
    with pytest.raises(ValueError, match="at most 8"):
        views.backtest_curves(session, list(range(9)))


# --- trained model --------------------------------------------------------------------


def test_no_trained_model_is_none_not_an_error(tmp_path):
    assert views.trained_model(tmp_path) is None


def test_trained_model_reports_folds_and_overall_ic(tmp_path):
    model_dir = tmp_path / "ml_lt"
    (model_dir / "v2").mkdir(parents=True)
    (model_dir / "latest").write_text("v2")
    metadata = {
        "model": "ml_lt",
        "version": "v2",
        "label": {"spec": "12m excess"},
        "universe": {"n_symbols": 400, "survivorship": "point-in-time"},
        "train_window": {"n_rows": 1000, "n_dates": 120, "dataset_dates": ["2010", "2026"]},
        "validation": {
            "scheme": "expanding",
            "purge_months": 12,
            "folds": [
                {
                    "fold_id": 0,
                    "train": ["2010", "2015"],
                    "test": ["2016", "2017"],
                    "ic": {"ridge": {"mean": 0.04, "std": 0.1, "n_dates": 12}, "lgbm": None},
                }
            ],
            "overall_ic": {"ridge": {"mean": 0.05, "std": 0.09, "n_dates": 60}},
        },
    }
    (model_dir / "v2" / "metadata.json").write_text(json.dumps(metadata))

    model = views.trained_model(tmp_path)

    assert model["version"] == "v2"
    assert model["dataset"]["survivorship"] == "point-in-time"
    assert model["folds"][0]["ic"] == {"ridge": 0.04, "lgbm": None}
    assert model["overall_ic"]["ridge"] == {"mean": 0.05, "std": 0.09}


# --- forward shadow -------------------------------------------------------------------


def test_shadow_books_track_turnover_and_name_the_latest_holdings(session):
    a, b = _instrument(session, "AAA"), _instrument(session, "BBB")
    _snapshot(session, "core", date(2026, 8, 1), {a.id: 1.0})
    _snapshot(session, "core", date(2026, 9, 1), {a.id: 0.25, b.id: 0.75})

    (book,) = views.shadow_books(session)

    assert [h["turnover"] for h in book["history"]] == [None, 0.75]
    assert book["latest"]["asof"] == "2026-09-01"
    assert book["latest"]["holdings"] == [
        {"symbol": "BBB", "weight": 0.75},
        {"symbol": "AAA", "weight": 0.25},
    ]


def test_snapshot_strategies_are_distinct_and_sorted(session):
    assert views.snapshot_strategies(session) == []
    for day, strategy in enumerate(("momentum", "core", "momentum"), start=1):
        _snapshot(session, strategy, date(2026, 9, day), {})
    assert views.snapshot_strategies(session) == ["core", "momentum"]


def test_a_snapshot_name_no_longer_in_the_database_is_still_shown(session):
    _snapshot(session, "core", date(2026, 9, 1), {999: 1.0})
    (book,) = views.shadow_books(session)
    assert book["latest"]["holdings"] == [{"symbol": "#999", "weight": 1.0}]


def test_shadow_performance_scores_mature_snapshots_against_xeqt(session):
    asset, xeqt = _instrument(session, "ASSET"), _instrument(session, "XEQT")
    day = date(2026, 1, 10)
    for instrument, closes in ((asset, [100.0, 110.0]), (xeqt, [100.0, 100.0])):
        _bar(session, instrument, day + timedelta(days=1), closes[0])
        _bar(session, instrument, date(2026, 2, 10), closes[1])
    _snapshot(session, "core", day, {asset.id: 1.0})

    result = views.shadow_performance(session, asof=date(2026, 2, 10), horizons=(1, 3))

    assert result["error"] is None
    one, three = result["horizons"]
    assert one["months"] == 1
    assert one["outcomes"][0]["excess"] == pytest.approx(0.10)
    assert one["outcomes"][0]["horizon_end"] == "2026-02-10"
    assert three["outcomes"] == []  # not mature for three months yet


def test_shadow_performance_reports_a_missing_benchmark_instead_of_raising(session):
    asset = _instrument(session, "ASSET")
    _snapshot(session, "core", date(2026, 1, 10), {asset.id: 1.0})

    result = views.shadow_performance(session, asof=date(2026, 6, 1))

    assert "XEQT daily bars are required" in result["error"]


# --- paper orders ---------------------------------------------------------------------


def test_no_paper_orders_yet(session):
    assert views.paper_orders(session) == {"orders": []}


def test_paper_orders_carry_their_fill_totals(session):
    xic = _instrument(session, "XIC")
    order = Order(
        id=1,
        environment="paper",
        instrument_id=xic.id,
        side=OrderSide.BUY,
        quantity=10,
        order_type="LMT",
        limit_price=40.0,
        status="Filled",
        placed_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    session.add(order)
    session.flush()
    for n, commission in enumerate((1.0, 0.5)):
        session.add(
            Execution(
                id=n + 1,
                order_id=order.id,
                ibkr_exec_id=f"e{n}",
                ts=datetime(2026, 10, 1, tzinfo=UTC),
                quantity=5,
                price=40.0,
                commission=commission,
            )
        )
    session.flush()

    (row,) = views.paper_orders(session)["orders"]

    assert row["symbol"] == "XIC"
    assert row["side"] == "BUY"
    assert row["fills"] == 2
    assert row["commission"] == pytest.approx(1.5)
    assert row["placed_at"].startswith("2026-10-01")


# --- files ----------------------------------------------------------------------------


def test_latest_plan_is_none_before_any_plan(tmp_path):
    assert views.latest_plan(tmp_path / "questrade-plan.json") is None


def test_latest_plan_round_trips_and_refuses_garbage(tmp_path):
    path = tmp_path / "questrade-plan.json"
    path.write_text(json.dumps({"strategy": "core"}))
    assert views.latest_plan(path) == {"strategy": "core"}

    path.write_text("{not json")
    with pytest.raises(ValueError, match="not a readable plan"):
        views.latest_plan(path)


def test_report_files_lists_nested_html_only(tmp_path):
    assert views.report_files(tmp_path / "missing") == []
    (tmp_path / "factor").mkdir()
    (tmp_path / "factor" / "tear.html").write_text("x")
    (tmp_path / "lab-report.html").write_text("x")
    (tmp_path / "questrade-plan.csv").write_text("x")

    paths = {f["path"] for f in views.report_files(tmp_path)}

    assert paths == {"factor/tear.html", "lab-report.html"}
