"""OOS factor-report provenance, frame-shape, and synthetic-signal sanity checks."""

from datetime import datetime

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

pytest.importorskip("alphalens")

from ibkr_trader.backtest.factor_report import (  # noqa: E402
    FactorInputs,
    build_factor_inputs,
    clean_factor_and_forward_returns,
    resolve_oos_run,
    summarize_factor_data,
    write_factor_report,
)
from ibkr_trader.backtest.oos import OOS_MODEL_VERSION  # noqa: E402
from ibkr_trader.db.models import (  # noqa: E402
    BacktestRun,
    Base,
    Instrument,
    Prediction,
    PriceBar,
)


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _run(session: Session, strategy: str, version: str, day: datetime) -> BacktestRun:
    run = BacktestRun(
        strategy=strategy,
        params={"model_version": version},
        start=day,
        end=day,
        metrics={},
        created_at=day,
    )
    session.add(run)
    session.flush()
    return run


def test_database_builders_select_only_linked_oos_predictions_and_preserve_utc():
    session = _session()
    dates = pd.bdate_range("2024-01-02", periods=12, tz="UTC")
    factor_dates = dates[[2, 5]]
    oos_run = _run(session, "ml_lt_ridge_oos", OOS_MODEL_VERSION, dates[0].to_pydatetime())
    in_sample = _run(session, "ml_lt_ridge", "artifact-v1", dates[0].to_pydatetime())

    for asset_number in range(1, 6):
        symbol = f"S{asset_number}"
        instrument = Instrument(symbol=symbol, exchange="TSX", currency="CAD")
        session.add(instrument)
        session.flush()
        for offset, day in enumerate(dates):
            close = 100.0 + asset_number * offset
            session.add(
                PriceBar(
                    instrument_id=instrument.id,
                    ts=day.to_pydatetime(),
                    bar_size="1 day",
                    source="yahoo",
                    what_to_show="ADJUSTED_LAST",
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=1_000_000.0,
                )
            )
        for factor_day in factor_dates:
            common = {
                "instrument_id": instrument.id,
                "ts": factor_day.to_pydatetime(),
                "horizon": "12m",
                "score": asset_number / 5,
                "features": {},
                "created_at": dates[-1].to_pydatetime(),
            }
            session.add(
                Prediction(
                    model_name=oos_run.strategy,
                    model_version=OOS_MODEL_VERSION,
                    backtest_run_id=oos_run.id,
                    **common,
                )
            )
            session.add(
                Prediction(
                    model_name=in_sample.strategy,
                    model_version="artifact-v1",
                    backtest_run_id=in_sample.id,
                    **common,
                )
            )
    session.commit()

    inputs = build_factor_inputs(session, oos_run.id, periods=(1, 2))

    assert inputs.run_id == oos_run.id
    assert inputs.strategy == "ml_lt_ridge_oos"
    assert inputs.factor.shape == (10,)
    assert inputs.factor.index.names == ["date", "asset"]
    assert str(inputs.factor.index.get_level_values("date").tz) == "UTC"
    assert inputs.prices.shape == (7, 5)
    assert inputs.prices.index.name == "date"
    assert str(inputs.prices.index.tz) == "UTC"
    assert inputs.prices.columns.name == "asset"
    assert resolve_oos_run(session).id == oos_run.id
    with pytest.raises(ValueError, match="not an OOS"):
        resolve_oos_run(session, in_sample.id)


def _synthetic_inputs(*, shuffled: bool) -> FactorInputs:
    rng = np.random.default_rng(20260718)
    assets = [f"S{number:02d}" for number in range(20)]
    price_dates = pd.bdate_range("2018-01-02", periods=1100, tz="UTC")
    daily_returns = np.linspace(0.00005, 0.001, len(assets))
    prices = pd.DataFrame(
        {
            asset: 100.0 * np.exp(daily_returns[index] * np.arange(len(price_dates)))
            for index, asset in enumerate(assets)
        },
        index=price_dates,
    )
    prices.index.name = "date"
    prices.columns.name = "asset"
    factor_dates = price_dates[20 : 20 + 36 * 21 : 21]
    tuples = []
    values = []
    base_scores = np.arange(len(assets), dtype=float)
    for day in factor_dates:
        scores = rng.permutation(base_scores) if shuffled else base_scores
        for asset, score in zip(assets, scores, strict=True):
            tuples.append((day, asset))
            values.append(score)
    factor = pd.Series(
        values,
        index=pd.MultiIndex.from_tuples(tuples, names=["date", "asset"]),
        name="factor",
    )
    return FactorInputs(run_id=1, strategy="ml_lt_ridge_oos", factor=factor, prices=prices)


def _summary(inputs: FactorInputs):
    clean = clean_factor_and_forward_returns(inputs, max_loss=0.35)
    return summarize_factor_data(clean, input_rows=len(inputs.factor))


def test_perfect_foresight_factor_has_strong_ic_and_monotonic_quantiles():
    report = _summary(_synthetic_inputs(shuffled=False))

    assert (report["mean_ic"]["mean_ic"] > 0.99).all()
    assert (report["quantile_diagnostics"]["q5_minus_q1"] > 0).all()
    assert report["quantile_diagnostics"]["monotonic_ish"].all()
    assert report["turnover"].loc["1M", "mean"] == pytest.approx(0.0)


def test_shuffled_factor_has_near_zero_mean_ic():
    report = _summary(_synthetic_inputs(shuffled=True))

    assert report["mean_ic"]["mean_ic"].abs().max() < 0.15


def test_optional_html_and_png_artifacts_are_written(tmp_path):
    report = {
        "metadata": {"run_id": 7, "strategy": "ml_lt_ridge_oos"},
        "mean_ic": pd.DataFrame({"mean_ic": [0.05]}, index=["21D"]),
        "quantile_returns": pd.DataFrame(
            {"21D": [-0.01, 0.0, 0.01, 0.02, 0.03]},
            index=["Q1", "Q2", "Q3", "Q4", "Q5"],
        ),
        "quantile_diagnostics": pd.DataFrame(
            {"q5_minus_q1": [0.04], "monotonic_ish": [True]}, index=["21D"]
        ),
        "turnover": pd.DataFrame({"mean": [0.2]}, index=["1M"]),
    }

    paths = write_factor_report(report, tmp_path)

    assert {path.suffix for path in paths} == {".html", ".png"}
    assert all(path.exists() and path.stat().st_size > 0 for path in paths)
    assert "native-currency close-to-close" in paths[0].read_text(encoding="utf-8")
