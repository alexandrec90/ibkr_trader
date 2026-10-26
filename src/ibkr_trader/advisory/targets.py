"""A strategy's target book, read from its latest forward snapshot. DB only, no network.

The snapshot is what ``snapshot run`` (the monthly task) recorded before its returns were
known, so the recommender trades the same book the forward shadow is scoring rather
than re-deciding it on the day.
"""

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise

from sqlalchemy import select
from sqlalchemy.orm import Session

from ibkr_trader.backtest.engine import load_instrument_series
from ibkr_trader.db.models import Instrument, StrategySnapshot
from ibkr_trader.signals.portfolio import BROAD_ETF_SYMBOLS

VOL_WINDOW = 60
#: Below this many daily returns a volatility estimate is noise; the planner's default
#: rung spacing is used instead.
MIN_VOL_RETURNS = 20
STALE_SNAPSHOT_DAYS = 45


@dataclass(frozen=True)
class TargetWeight:
    symbol: str
    currency: str
    weight: float
    broad_etf: bool
    daily_vol: float | None


@dataclass(frozen=True)
class StrategyTargets:
    strategy: str
    asof: date
    weights: list[TargetWeight]
    #: Snapshot instrument ids that no longer resolve to an instrument row.
    missing: tuple[str, ...] = ()


def daily_volatility(closes: list[float], window: int = VOL_WINDOW) -> float | None:
    """Sample standard deviation of the last ``window`` daily log returns."""
    recent = closes[-(window + 1) :]
    returns = [math.log(b / a) for a, b in pairwise(recent) if a > 0 and b > 0]
    if len(returns) < MIN_VOL_RETURNS:
        return None
    mean = sum(returns) / len(returns)
    return math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))


def questrade_candidates(symbol: str, currency: str) -> list[str]:
    """Questrade spellings to try for a stored instrument, most likely first.

    Questrade's documented symbology suffixes TSX listings with ``.TO`` (``TD.TO``) and
    leaves US listings bare (``AAPL``). Yahoo-ingested share classes use a dash
    (``BRK-B``); the dotted form is tried too, and the caller keeps only a candidate whose
    currency matches, so a wrong guess is a skipped name, never a wrong security.
    """
    dotted = symbol.upper().replace("-", ".")
    if currency.upper() == "CAD":
        return [f"{dotted}.TO"]
    return list(dict.fromkeys([symbol.upper(), dotted]))


def latest_targets(
    session: Session, strategy: str, *, today: date | None = None
) -> StrategyTargets:
    """The newest snapshot of ``strategy``, with each name's recent daily volatility."""
    row = session.scalar(
        select(StrategySnapshot)
        .where(StrategySnapshot.strategy == strategy)
        .order_by(StrategySnapshot.ts.desc())
        .limit(1)
    )
    if row is None:
        raise ValueError(
            f"no forward snapshot for {strategy!r}; run 'Snapshot: Run Monthly' "
            "(ibkr-trader snapshot run) first"
        )
    today = today or datetime.now(tz=UTC).date()
    start = datetime.combine(today - timedelta(days=VOL_WINDOW * 2 + 30), time.min, tzinfo=UTC)
    end = datetime.combine(today, time.max, tzinfo=UTC)
    weights: list[TargetWeight] = []
    missing: list[str] = []
    for instrument_id, weight in row.weights.items():
        instrument = session.get(Instrument, int(instrument_id))
        if instrument is None:
            missing.append(str(instrument_id))
            continue
        if float(weight) <= 0:
            continue
        series = load_instrument_series(session, instrument, start, end, validate=False)
        weights.append(
            TargetWeight(
                symbol=instrument.symbol,
                currency=instrument.currency,
                weight=float(weight),
                broad_etf=instrument.symbol.upper() in BROAD_ETF_SYMBOLS
                or instrument.asset_class == "ETF",
                daily_vol=daily_volatility(series.closes) if series else None,
            )
        )
    weights.sort(key=lambda w: (-w.weight, w.symbol))
    return StrategyTargets(strategy, row.ts.date(), weights, tuple(missing))
