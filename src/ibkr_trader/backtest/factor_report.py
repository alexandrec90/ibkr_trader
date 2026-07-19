"""Alphalens diagnostics over persisted walk-forward OOS predictions.

This module is research-only. The factor is the score emitted for each fold's held-out rows;
the price input is native-currency daily close data. The production backtest remains the only
place that applies CAD conversion, dividends, costs, eligibility, and portfolio constraints.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ibkr_trader.backtest.engine import _pick_source
from ibkr_trader.backtest.oos import OOS_MODEL_VERSION
from ibkr_trader.db.models import BacktestRun, Instrument, Prediction, PriceBar

DEFAULT_PERIODS = (21, 63, 126, 252)
DEFAULT_QUANTILES = 5
DEFAULT_MAX_LOSS = 0.35
OOS_STRATEGIES = ("ml_lt_oos", "ml_lt_ridge_oos")


def _require_alphalens():
    try:
        from alphalens import performance, utils
    except ImportError as exc:  # pragma: no cover - exercised in a core-only environment
        raise RuntimeError(
            "factor reporting needs the [research] extra; run `uv sync --extra research`"
        ) from exc
    return performance, utils


@dataclass(frozen=True)
class FactorInputs:
    """Database inputs in the two shapes consumed by Alphalens."""

    run_id: int
    strategy: str
    factor: pd.Series
    prices: pd.DataFrame


def _as_utc(value: datetime, *, sqlite: bool) -> datetime:
    if value.tzinfo is None:
        if not sqlite:
            raise ValueError("database returned a timezone-naive research timestamp")
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def resolve_oos_run(session: Session, run_id: int | None = None) -> BacktestRun:
    """Resolve one persisted OOS model run, defaulting to the newest with predictions."""
    if run_id is None:
        run = session.scalar(
            select(BacktestRun)
            .join(Prediction, Prediction.backtest_run_id == BacktestRun.id)
            .where(
                BacktestRun.strategy.in_(OOS_STRATEGIES),
                Prediction.model_version == OOS_MODEL_VERSION,
            )
            .order_by(BacktestRun.created_at.desc(), BacktestRun.id.desc())
            .limit(1)
        )
    else:
        run = session.get(BacktestRun, run_id)
    if run is None:
        target = "latest persisted OOS run" if run_id is None else f"backtest run {run_id}"
        raise ValueError(f"{target} was not found")
    params = run.params or {}
    if run.strategy not in OOS_STRATEGIES or params.get("model_version") != OOS_MODEL_VERSION:
        raise ValueError(
            f"backtest run {run.id} is not an OOS walk-forward model run "
            f"({run.strategy}, model_version={params.get('model_version')!r})"
        )
    return run


def load_oos_factor(session: Session, run: BacktestRun) -> pd.Series:
    """Load only the held-out predictions linked to ``run`` as date×asset scores."""
    rows = session.execute(
        select(Prediction.ts, Instrument.symbol, Prediction.score)
        .join(Instrument, Instrument.id == Prediction.instrument_id)
        .where(
            Prediction.backtest_run_id == run.id,
            Prediction.model_name == run.strategy,
            Prediction.model_version == OOS_MODEL_VERSION,
        )
        .order_by(Prediction.ts, Instrument.symbol)
    ).all()
    if not rows:
        raise ValueError(f"backtest run {run.id} has no persisted OOS predictions")
    sqlite = session.get_bind().dialect.name == "sqlite"
    frame = pd.DataFrame(
        {
            "date": [pd.Timestamp(_as_utc(row.ts, sqlite=sqlite)).normalize() for row in rows],
            "asset": [row.symbol for row in rows],
            "factor": [float(row.score) for row in rows],
        }
    )
    if frame[["date", "asset"]].duplicated().any():
        raise ValueError(f"backtest run {run.id} has duplicate date/asset predictions")
    factor = frame.set_index(["date", "asset"])["factor"].sort_index()
    factor.index = factor.index.set_names(["date", "asset"])
    factor.name = "factor"
    return factor


def load_native_close_prices(
    session: Session,
    factor: pd.Series,
    *,
    periods: tuple[int, ...] = DEFAULT_PERIODS,
) -> pd.DataFrame:
    """Load one daily adjusted/TRADES source per factor asset as a wide close frame."""
    if not periods or min(periods) < 1:
        raise ValueError("periods must contain positive trading-day horizons")
    dates = pd.DatetimeIndex(factor.index.get_level_values("date"))
    assets = list(dict.fromkeys(factor.index.get_level_values("asset")))
    start = dates.min().to_pydatetime()
    # Two calendar days per requested trading day comfortably spans weekends/holidays.
    end = (dates.max() + timedelta(days=max(periods) * 2)).to_pydatetime()
    instruments = {
        instrument.symbol: instrument
        for instrument in session.scalars(select(Instrument).where(Instrument.symbol.in_(assets)))
    }
    missing = sorted(set(assets) - set(instruments))
    if missing:
        raise ValueError(f"factor assets have no instruments: {', '.join(missing)}")

    sqlite = session.get_bind().dialect.name == "sqlite"
    close_frames: list[pd.DataFrame] = []
    for asset in assets:
        instrument = instruments[asset]
        rows: list[PriceBar] = []
        for what_to_show in ("ADJUSTED_LAST", "TRADES"):
            source = _pick_source(session, instrument.id, "1 day", what_to_show, start, end)
            if source is None:
                continue
            rows = list(
                session.scalars(
                    select(PriceBar)
                    .where(
                        PriceBar.instrument_id == instrument.id,
                        PriceBar.bar_size == "1 day",
                        PriceBar.what_to_show == what_to_show,
                        PriceBar.source == source,
                        PriceBar.ts >= start,
                        PriceBar.ts <= end,
                    )
                    .order_by(PriceBar.ts)
                )
            )
            if rows:
                break
        if not rows:
            continue
        close_frames.append(
            pd.DataFrame(
                {
                    "date": [
                        pd.Timestamp(_as_utc(row.ts, sqlite=sqlite)).normalize() for row in rows
                    ],
                    "asset": asset,
                    "close": [float(row.close) for row in rows],
                }
            )
        )
    if not close_frames:
        raise ValueError("no daily prices found for the OOS factor assets")
    long_prices = pd.concat(close_frames, ignore_index=True)
    prices = long_prices.pivot(index="date", columns="asset", values="close").sort_index()
    prices.index = pd.DatetimeIndex(prices.index, name="date")
    prices.columns.name = "asset"
    return prices


def build_factor_inputs(
    session: Session,
    run_id: int | None = None,
    *,
    periods: tuple[int, ...] = DEFAULT_PERIODS,
) -> FactorInputs:
    """Build the auditable OOS factor Series and native-close price matrix from Postgres."""
    run = resolve_oos_run(session, run_id)
    factor = load_oos_factor(session, run)
    prices = load_native_close_prices(session, factor, periods=periods)
    return FactorInputs(run_id=run.id, strategy=run.strategy, factor=factor, prices=prices)


def clean_factor_and_forward_returns(
    inputs: FactorInputs,
    *,
    periods: tuple[int, ...] = DEFAULT_PERIODS,
    quantiles: int = DEFAULT_QUANTILES,
    max_loss: float = DEFAULT_MAX_LOSS,
) -> pd.DataFrame:
    """Align OOS scores and forward native-close returns with explicit loss controls."""
    if quantiles < 2:
        raise ValueError("quantiles must be >= 2")
    _, utils = _require_alphalens()
    # alphalens-reloaded records the inferred trading frequency on the factor date *level*.
    # Pandas 3 validates that assignment, so a naturally sparse monthly level rejects a
    # business-day frequency. Preserve the same monthly rows/codes while giving the MultiIndex
    # an unused, complete trading-calendar level; row count and values remain unchanged.
    factor_dates = pd.DatetimeIndex(inputs.factor.index.get_level_values("date").unique())
    calendar = utils.infer_trading_calendar(factor_dates, inputs.prices.index)
    full_dates = pd.date_range(inputs.prices.index.min(), inputs.prices.index.max(), freq=calendar)
    factor_assets = pd.Index(inputs.factor.index.levels[1])
    date_codes = full_dates.get_indexer(inputs.factor.index.get_level_values("date"))
    if (date_codes < 0).any():
        raise ValueError("factor dates must be present in the native-close price calendar")
    regularized_factor = pd.Series(
        inputs.factor.to_numpy(),
        index=pd.MultiIndex(
            levels=[full_dates, factor_assets],
            codes=[date_codes, inputs.factor.index.codes[1]],
            names=["date", "asset"],
            verify_integrity=True,
        ),
        name="factor",
    )
    return utils.get_clean_factor_and_forward_returns(
        regularized_factor,
        inputs.prices,
        periods=periods,
        quantiles=quantiles,
        max_loss=max_loss,
        filter_zscore=None,  # filtering on future-return magnitude would introduce look-ahead
    )


def _monthly_turnover(
    factor_data: pd.DataFrame, *, quantiles: int, month_gaps: tuple[int, ...]
) -> pd.DataFrame:
    performance, _ = _require_alphalens()
    original_dates = pd.DatetimeIndex(
        factor_data.index.get_level_values("date").unique()
    ).sort_values()
    regular_dates = pd.date_range("2000-01-31", periods=len(original_dates), freq="ME", tz="UTC")
    date_codes_by_value = {date: code for code, date in enumerate(original_dates)}
    date_codes = [date_codes_by_value[date] for date in factor_data.index.get_level_values("date")]
    assets = pd.Index(factor_data.index.get_level_values("asset").unique())
    asset_codes = assets.get_indexer(factor_data.index.get_level_values("asset"))
    remapped = pd.Series(
        factor_data["factor_quantile"].to_numpy(),
        index=pd.MultiIndex(
            levels=[regular_dates, assets],
            codes=[date_codes, asset_codes],
            names=["date", "asset"],
            verify_integrity=True,
        ),
        name="factor_quantile",
    )
    rows: dict[str, dict[str, float]] = {}
    for gap in month_gaps:
        values = {
            f"Q{quantile}": float(
                performance.quantile_turnover(remapped, quantile, period=gap).mean()
            )
            for quantile in range(1, quantiles + 1)
        }
        finite_values = [value for value in values.values() if np.isfinite(value)]
        values["mean"] = float(np.mean(finite_values)) if finite_values else float("nan")
        rows[f"{gap}M"] = values
    return pd.DataFrame.from_dict(rows, orient="index").rename_axis("period")


def summarize_factor_data(
    factor_data: pd.DataFrame,
    *,
    input_rows: int,
    quantiles: int = DEFAULT_QUANTILES,
) -> dict[str, Any]:
    """Return compact IC-decay, quantile-return, and turnover tables."""
    performance, utils = _require_alphalens()
    forward_columns = list(utils.get_forward_returns_columns(factor_data.columns))
    ic_by_date = performance.factor_information_coefficient(factor_data)
    mean_ic = pd.DataFrame(
        {
            "mean_ic": ic_by_date.mean(),
            "std_ic": ic_by_date.std(ddof=0),
            "n_dates": ic_by_date.count().astype(int),
        }
    )
    mean_ic.index.name = "period"
    first_ic = float(mean_ic.iloc[0]["mean_ic"])
    mean_ic["relative_to_shortest"] = (
        mean_ic["mean_ic"] / abs(first_ic) if first_ic != 0 else np.nan
    )

    quantile_returns, _ = performance.mean_return_by_quantile(
        factor_data, by_date=False, demeaned=False
    )
    quantile_returns = quantile_returns.loc[:, forward_columns]
    quantile_returns.index = [f"Q{int(value)}" for value in quantile_returns.index]
    quantile_returns.index.name = "quantile"
    diagnostic_rows = {}
    quantile_axis = pd.Series(range(1, quantiles + 1), dtype=float)
    for period in forward_columns:
        values = quantile_returns[period].reset_index(drop=True)
        monotonicity = float(quantile_axis.corr(values, method="spearman"))
        spread = float(values.iloc[-1] - values.iloc[0])
        diagnostic_rows[period] = {
            "q5_minus_q1": spread,
            "positive_spread": bool(spread > 0),
            "monotonic_spearman": monotonicity,
            "monotonic_ish": bool(monotonicity >= 0.7),
        }
    quantile_diagnostics = pd.DataFrame.from_dict(diagnostic_rows, orient="index")
    quantile_diagnostics.index.name = "period"

    turnover = _monthly_turnover(factor_data, quantiles=quantiles, month_gaps=(1, 3, 6, 12))
    diagnostics = {
        "input_rows": int(input_rows),
        "clean_rows": int(len(factor_data)),
        "dropped_rows": int(input_rows - len(factor_data)),
        "loss_fraction": float(1.0 - len(factor_data) / input_rows),
    }
    return {
        "mean_ic": mean_ic,
        "quantile_returns": quantile_returns,
        "quantile_diagnostics": quantile_diagnostics,
        "turnover": turnover,
        "diagnostics": diagnostics,
    }


def write_factor_report(report: dict[str, Any], output_dir: Path) -> list[Path]:
    """Write an HTML table report and compact PNG tear sheet under a local reports path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    metadata = report["metadata"]
    header = (
        f"<h1>OOS factor report: {metadata['strategy']} (run {metadata['run_id']})</h1>"
        "<p>Research diagnostic only. Forward returns use native-currency close-to-close "
        "prices; the production backtest separately handles CAD conversion, total-return "
        "rigor, costs, eligibility, and portfolio constraints.</p>"
    )
    sections = [header]
    for name in ("mean_ic", "quantile_returns", "quantile_diagnostics", "turnover"):
        sections.append(f"<h2>{name.replace('_', ' ').title()}</h2>")
        sections.append(report[name].to_html(float_format=lambda value: f"{value:.4f}"))
    html_path = output_dir / f"factor-report-run-{metadata['run_id']}.html"
    html_path.write_text(
        "<!doctype html><meta charset='utf-8'>" + "".join(sections), encoding="utf-8"
    )

    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(1, 3, figsize=(15, 4.5))
    report["mean_ic"]["mean_ic"].plot.bar(ax=axes[0], title="Mean rank IC")
    axes[0].axhline(0, color="black", linewidth=0.8)
    report["quantile_returns"].T.plot(ax=axes[1], title="Mean quantile returns")
    report["turnover"]["mean"].plot.bar(ax=axes[2], title="Mean quantile turnover")
    figure.suptitle(f"{metadata['strategy']} · OOS run {metadata['run_id']}")
    figure.tight_layout()
    png_path = output_dir / f"factor-report-run-{metadata['run_id']}.png"
    figure.savefig(png_path, dpi=150)
    plt.close(figure)
    return [html_path, png_path]


def generate_factor_report(
    session: Session,
    run_id: int | None = None,
    *,
    periods: tuple[int, ...] = DEFAULT_PERIODS,
    quantiles: int = DEFAULT_QUANTILES,
    max_loss: float = DEFAULT_MAX_LOSS,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Build, analyze, and optionally save one persisted OOS factor report."""
    inputs = build_factor_inputs(session, run_id, periods=periods)
    factor_data = clean_factor_and_forward_returns(
        inputs, periods=periods, quantiles=quantiles, max_loss=max_loss
    )
    report = summarize_factor_data(factor_data, input_rows=len(inputs.factor), quantiles=quantiles)
    report["metadata"] = {
        "run_id": inputs.run_id,
        "strategy": inputs.strategy,
        "periods": list(periods),
        "quantiles": quantiles,
        "return_basis": "native-currency close-to-close",
    }
    report["artifacts"] = write_factor_report(report, output_dir) if output_dir else []
    return report
