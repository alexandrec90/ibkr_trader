"""Feature engineering: turn raw rows (bars, news, social, trends) into model inputs.

Everything reads from Postgres and writes back to Postgres (sentiment columns,
predictions, feature snapshots) — no network calls in this package, so feature builds
are reproducible.

The heart is a **pure core + thin DB wrapper**: ``build_features_asof`` computes feature
set v2 from in-memory inputs (no session, no network) so training (ML-03) and the backtest
engine share the exact same feature code and can never disagree; ``build_daily_features``
loads those inputs from Postgres and persists snapshots to the ``features`` table, keyed by
``FEATURE_SET_VERSION`` so a saved model knows what it was trained on.
"""

import math
import re
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ibkr_trader.db.models import Dividend, Feature, Instrument, ShareCount

#: Version stamp for the feature *definitions* below. Bump whenever a feature's semantics
#: change (lookbacks, formulas, additions) — persisted snapshots and trained models carry it,
#: so mixed-version rows never silently feed one model.
FEATURE_SET_VERSION = "2"

TRADING_DAYS_PER_YEAR = 252

#: Trailing total-return lookbacks in trading days (a month ≈ 21 trading days). ``return_12m``
#: must keep the exact semantics of the pre-ML-02 engine (guards the momentum_lt baseline).
_RETURN_LOOKBACKS: dict[str, int] = {
    "return_1m": 21,
    "return_3m": 63,
    "return_6m": 126,
    "return_12m": 252,
}
_ONE_MONTH = 21
#: Minimum observations for the risk-window features — same floor the engine used for
#: ``volatility``, kept identical for parity.
_MIN_RISK_OBS = 20
_ONE_YEAR = timedelta(days=365)


@dataclass(frozen=True)
class CorporateData:
    """One instrument's ML-01 corporate rows, shaped for ``FeatureInputs``.

    ``None`` means "not ingested" → the corresponding features are simply absent; an empty
    tuple means "ingested, and there are none" (e.g. a stock that pays no dividends →
    ``dividend_yield_ttm`` is a genuine 0.0).
    """

    dividends: tuple[tuple[date, float], ...] | None = None  # (ex_date, amount), sorted
    share_counts: tuple[tuple[date, float], ...] | None = None  # (date, shares), sorted
    sector: str | None = None


@dataclass(frozen=True)
class FeatureInputs:
    """In-memory inputs for one instrument's feature build.

    ``dates`` must be sorted ascending and align 1:1 with ``closes``/``volumes``. Inputs may
    extend beyond the as-of day — ``build_features_asof`` only ever reads entries ≤ day.
    """

    dates: Sequence[date]
    closes: Sequence[float]
    volumes: Sequence[float] | None = None
    dividends: Sequence[tuple[date, float]] | None = None
    share_counts: Sequence[tuple[date, float]] | None = None
    sector: str | None = None
    benchmark_dates: Sequence[date] | None = None
    benchmark_closes: Sequence[float] | None = None


def _trailing_return(closes: Sequence[float], i: int, lookback: int) -> float | None:
    back = i - lookback
    if back < 0 or closes[back] <= 0:
        return None
    return closes[i] / closes[back] - 1.0


def _add_price_features(inputs: FeatureInputs, i: int, feats: dict[str, float]) -> None:
    closes = inputs.closes
    for name, lookback in _RETURN_LOOKBACKS.items():
        ret = _trailing_return(closes, i, lookback)
        if ret is not None:
            feats[name] = ret

    # 12-month return skipping the most recent month — momentum without short-term reversal.
    back12 = i - _RETURN_LOOKBACKS["return_12m"]
    if back12 >= 0 and closes[back12] > 0:
        feats["momentum_12_1"] = closes[i - _ONE_MONTH] / closes[back12] - 1.0

    annualize = math.sqrt(TRADING_DAYS_PER_YEAR)
    window = np.asarray(closes[max(0, i - TRADING_DAYS_PER_YEAR + 1) : i + 1], dtype=float)
    if len(window) >= _MIN_RISK_OBS:
        rets = np.diff(window) / window[:-1]
        feats["volatility"] = float(rets.std() * annualize)
        downside = np.minimum(rets, 0.0)
        feats["downside_deviation_252d"] = float(np.sqrt(np.mean(np.square(downside))) * annualize)
        peaks = np.maximum.accumulate(window)
        feats["max_drawdown_252d"] = float(((window - peaks) / peaks).min())

    window_60 = np.asarray(closes[max(0, i - 59) : i + 1], dtype=float)
    if len(window_60) >= _MIN_RISK_OBS:
        rets_60 = np.diff(window_60) / window_60[:-1]
        feats["volatility_60d"] = float(rets_60.std() * annualize)

    if i + 1 >= TRADING_DAYS_PER_YEAR:
        high = max(closes[i - TRADING_DAYS_PER_YEAR + 1 : i + 1])
        if high > 0:
            feats["pct_off_52w_high"] = closes[i] / high - 1.0

    if inputs.volumes is not None:
        vol_window = np.asarray(inputs.volumes[max(0, i - 59) : i + 1], dtype=float)
        if len(vol_window) >= _MIN_RISK_OBS:
            std = vol_window.std()
            zscore = (vol_window[-1] - vol_window.mean()) / std if std > 0 else 0.0
            feats["volume_zscore_60d"] = float(zscore)


def _add_benchmark_relative(inputs: FeatureInputs, day: date, feats: dict[str, float]) -> None:
    if not inputs.benchmark_dates or not inputs.benchmark_closes:
        return
    j = bisect_right(inputs.benchmark_dates, day) - 1
    if j < 0:
        return
    for suffix in ("3m", "12m"):
        own = feats.get(f"return_{suffix}")
        bench = _trailing_return(inputs.benchmark_closes, j, _RETURN_LOOKBACKS[f"return_{suffix}"])
        if own is not None and bench is not None:
            feats[f"excess_return_{suffix}"] = own - bench


def _add_corporate_features(
    inputs: FeatureInputs, day: date, close: float, feats: dict[str, float]
) -> None:
    if inputs.dividends is not None and close > 0:
        paid = [(ex_date, amount) for ex_date, amount in inputs.dividends if ex_date <= day]
        ttm = sum(amount for ex_date, amount in paid if ex_date > day - _ONE_YEAR)
        feats["dividend_yield_ttm"] = ttm / close
        # growth needs an honest 3-years-ago TTM: only when dividend history reaches back
        # far enough, annualized as a 3-year CAGR of the trailing-12-month payout.
        earliest = min((ex_date for ex_date, _ in paid), default=None)
        if earliest is not None and earliest <= day - 3 * _ONE_YEAR:
            prior = sum(
                amount
                for ex_date, amount in paid
                if day - 4 * _ONE_YEAR < ex_date <= day - 3 * _ONE_YEAR
            )
            if prior > 0:
                feats["dividend_growth_3y"] = (ttm / prior) ** (1 / 3) - 1.0

    if inputs.share_counts is not None and close > 0:
        latest = max(((d, shares) for d, shares in inputs.share_counts if d <= day), default=None)
        if latest is not None and latest[1] > 0:
            feats["log_market_cap"] = math.log(close * latest[1])


def build_features_asof(inputs: FeatureInputs, day: date) -> dict[str, float]:
    """Feature set v2 as-of ``day`` — the numeric dict allocators and models consume.

    No-look-ahead contract: **only data dated ≤ day influences the output**; appending later
    bars/dividends/share counts to ``inputs`` must not change the result at ``day``. Features
    with insufficient history are simply absent (never NaN). ``return_12m`` and ``volatility``
    keep the exact semantics of the pre-ML-02 engine so existing baselines are unchanged.
    Non-finite values are dropped (degenerate inputs like zero closes), keeping payloads
    JSON-safe. The categorical ``sector`` is deliberately not here — see
    ``build_feature_payload``.
    """
    i = bisect_right(inputs.dates, day) - 1
    if i < 0:
        return {}
    # Count only bars observable as of the decision. This is deliberately the same quantity
    # used by Candidate.history_days, exposed to the model so young-listing missingness has an
    # explicit regime signal. Every pre-v2 feature below retains its v1 semantics.
    feats: dict[str, float] = {"history_days": float(i + 1)}
    _add_price_features(inputs, i, feats)
    _add_benchmark_relative(inputs, day, feats)
    _add_corporate_features(inputs, day, inputs.closes[i], feats)
    return {name: value for name, value in feats.items() if math.isfinite(value)}


def build_feature_payload(inputs: FeatureInputs, day: date) -> dict[str, float | str]:
    """The persisted ``features.payload``: numeric features plus the categorical ``sector``.

    Allocators use the numeric dict from ``build_features_asof``; training encodes ``sector``
    categorically.
    """
    payload: dict[str, float | str] = dict(build_features_asof(inputs, day))
    if inputs.sector:
        payload["sector"] = inputs.sector
    return payload


def load_corporate_inputs(session: Session, instrument_id: int) -> CorporateData:
    """One instrument's ML-01 rows from Postgres, shaped for ``FeatureInputs``.

    No rows at all → ``None`` fields, so feature builds degrade to price-only (data not yet
    ingested) instead of erroring. Multi-source duplicates collapse by date so amounts never
    double-count.
    """
    div_rows = session.execute(
        select(Dividend.ex_date, Dividend.amount)
        .where(Dividend.instrument_id == instrument_id)
        .order_by(Dividend.ex_date, Dividend.source)
    ).all()
    dividends = {ex_date: amount for ex_date, amount in div_rows}
    share_rows = session.execute(
        select(ShareCount.date, ShareCount.shares)
        .where(ShareCount.instrument_id == instrument_id)
        .order_by(ShareCount.date, ShareCount.source)
    ).all()
    share_counts = {day: shares for day, shares in share_rows}
    instrument = session.get(Instrument, instrument_id)
    return CorporateData(
        dividends=tuple(sorted(dividends.items())) if div_rows else None,
        share_counts=tuple(sorted(share_counts.items())) if share_rows else None,
        sector=instrument.sector if instrument is not None else None,
    )


def _upsert_feature(
    session: Session, instrument_id: int, day: date, payload: dict[str, float | str]
) -> None:
    ts = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    existing = session.scalar(
        select(Feature).where(
            Feature.instrument_id == instrument_id,
            Feature.ts == ts,
            Feature.feature_set_version == FEATURE_SET_VERSION,
        )
    )
    if existing:
        existing.payload = payload
    else:
        session.add(
            Feature(
                instrument_id=instrument_id,
                ts=ts,
                feature_set_version=FEATURE_SET_VERSION,
                payload=payload,
            )
        )


#: Calendar-day margin loaded before the earliest requested date so 252-trading-day lookbacks
#: (~365 calendar days) have full history; generous to absorb holidays and listing gaps.
_HISTORY_BUFFER_DAYS = 550


def build_daily_features(
    session: Session,
    instrument_ids: Sequence[int],
    dates: Sequence[date],
    *,
    benchmark_symbol: str = "XEQT",
) -> int:
    """Compute feature set v2 for each (instrument, date) and upsert ``features`` snapshots.

    Thin DB wrapper around the pure core: loads bars (via the engine's one-source-per-
    instrument loader), dividends, share counts and sector from Postgres — never the network —
    and persists ``build_feature_payload`` keyed on (instrument_id, ts, FEATURE_SET_VERSION),
    so re-runs are idempotent. Days before an instrument's first bar produce no row. Returns
    the number of rows upserted.
    """
    # Runtime import: engine imports this module's core at module load; the wrapper borrowing
    # its Series loader the other way must stay function-level to avoid the cycle.
    from ibkr_trader.backtest.engine import _load_series

    if not instrument_ids or not dates:
        return 0
    days = sorted(set(dates))
    start = datetime.combine(days[0], datetime.min.time(), tzinfo=UTC) - timedelta(
        days=_HISTORY_BUFFER_DAYS
    )
    end = datetime.combine(days[-1], datetime.min.time(), tzinfo=UTC) + timedelta(days=1)
    benchmark = _load_series(session, benchmark_symbol, start, end)

    count = 0
    for instrument_id in instrument_ids:
        instrument = session.get(Instrument, instrument_id)
        if instrument is None:
            continue
        series = _load_series(session, instrument.symbol, start, end)
        if series is None:
            continue
        corporate = load_corporate_inputs(session, instrument_id)
        inputs = FeatureInputs(
            dates=series.dates,
            closes=series.closes,
            volumes=series.volumes,
            dividends=corporate.dividends,
            share_counts=corporate.share_counts,
            sector=corporate.sector,
            benchmark_dates=benchmark.dates if benchmark is not None else None,
            benchmark_closes=benchmark.closes if benchmark is not None else None,
        )
        for day in days:
            payload = build_feature_payload(inputs, day)
            if not payload:
                continue
            _upsert_feature(session, instrument_id, day, payload)
            count += 1
    return count


# Naive first pass: $TSLA / uppercase-word matching against the instruments table.
# Known weakness: words like "CEO", "YOLO", "DD" false-positive — filter against a
# stoplist + the instruments table, and revisit with a proper NER pass later.
CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5})\b")


def extract_tickers(text: str, known_symbols: set[str]) -> list[str]:
    """Extract candidate tickers from free text, keeping only known instruments."""
    if not text:
        return []
    candidates = {match.upper() for match in CASHTAG_RE.findall(text)}
    candidates |= {word for word in re.findall(r"\b[A-Z]{2,5}\b", text)}
    return sorted(candidates & known_symbols)


#: Lazy VADER singleton — the lexicon load happens once, on the first scoring call, so
#: importing this module stays cheap for code paths that never score text.
_vader_analyzer: Any = None


def score_sentiment(text: str) -> float:
    """Sentiment in [-1, 1] via VADER's compound score; blank/empty text scores 0.0.

    VADER is a general-purpose lexicon model — good enough as v1 for headline-grade text
    (persisted into news_articles.sentiment / social_posts.sentiment by signals.sentiment);
    revisit a finance-tuned model if the event study shows promise.
    """
    global _vader_analyzer
    if not text or not text.strip():
        return 0.0
    if _vader_analyzer is None:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

        _vader_analyzer = SentimentIntensityAnalyzer()
    return float(_vader_analyzer.polarity_scores(text)["compound"])
