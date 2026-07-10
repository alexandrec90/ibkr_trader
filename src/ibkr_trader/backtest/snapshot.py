"""Forward-only strategy snapshots and realized-return reporting.

The snapshot path is deliberately DB-only. It records today's target weights before future
bars exist, then reports them only after their horizon has elapsed.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ibkr_trader.backtest.costs import RegisteredAccountCostModel
from ibkr_trader.backtest.engine import (
    RegisteredStrategyConfig,
    Series,
    _build_candidates,
    _features_asof,
    _fx_to_cad,
    _load_series,
    _load_universe,
)
from ibkr_trader.db.models import Instrument, PriceBar, StrategySnapshot
from ibkr_trader.signals.eligibility import screen
from ibkr_trader.signals.features import FEATURE_SET_VERSION, load_corporate_inputs
from ibkr_trader.signals.portfolio import available, get_allocator

MAX_BACKFILL_DAYS = 1
STALE_BAR_DAYS = 7


@dataclass(frozen=True)
class SnapshotRunResult:
    snapshots: list[StrategySnapshot]
    skipped: dict[str, str]
    latest_bar_date: date
    stale_days: int


@dataclass(frozen=True)
class SnapshotReportRow:
    strategy: str
    horizon_months: int
    mean_excess: float
    hit_rate: float
    n_snapshots: int


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def _add_months(day: date, months: int) -> date:
    month_index = day.year * 12 + day.month - 1 + months
    year, month0 = divmod(month_index, 12)
    month = month0 + 1
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    last_day = (next_month - timedelta(days=1)).day
    return date(year, month, min(day.day, last_day))


def assert_snapshot_date(asof: date, *, today: date | None = None) -> None:
    """Refuse historical snapshots; one day is allowed only for failed-job recovery."""
    today = today or datetime.now(tz=UTC).date()
    if asof > today:
        raise ValueError(f"snapshot date {asof} is in the future")
    if (today - asof).days > MAX_BACKFILL_DAYS:
        raise ValueError(
            f"refusing to backfill snapshot for {asof}; forward snapshots must be taken now "
            f"(maximum retry allowance: {MAX_BACKFILL_DAYS} day)"
        )


def _all_bar_symbols(session: Session) -> list[str]:
    return list(
        session.scalars(
            select(Instrument.symbol)
            .join(PriceBar, PriceBar.instrument_id == Instrument.id)
            .where(Instrument.sec_type != "CASH")
            .distinct()
            .order_by(Instrument.symbol)
        )
    )


def run_snapshots(
    session: Session,
    *,
    strategies: list[str] | None = None,
    asof: date | None = None,
    today: date | None = None,
    config: RegisteredStrategyConfig | None = None,
) -> SnapshotRunResult:
    """Build and upsert forward target weights using only bars available by ``asof``."""
    today = today or datetime.now(tz=UTC).date()
    asof = asof or today
    assert_snapshot_date(asof, today=today)
    latest_ts = session.scalar(
        select(func.max(PriceBar.ts)).where(
            PriceBar.bar_size == "1 day", PriceBar.ts < _midnight(asof) + timedelta(days=1)
        )
    )
    if latest_ts is None:
        raise ValueError("no daily price bars found; run ingestion before snapshotting")
    latest_day = latest_ts.date()
    stale_days = (asof - latest_day).days

    symbols = _all_bar_symbols(session)
    start = datetime(1900, 1, 1, tzinfo=UTC)
    end = _midnight(asof) + timedelta(days=1) - timedelta(microseconds=1)
    universe = _load_universe(session, symbols, start, end)
    if not universe:
        raise ValueError("no usable adjusted or trade daily bars found")
    fx = _load_series(session, "USDCAD", start, end)
    config = config or RegisteredStrategyConfig()
    candidates = _build_candidates(universe, fx, asof, config)
    screened = screen(candidates, config.eligibility)
    benchmark = next(
        (s for s in universe.values() if s.symbol.upper() == config.benchmark_symbol.upper()),
        None,
    )
    corporate = {iid: load_corporate_inputs(session, iid) for iid in universe}
    features = _features_asof(
        universe,
        screened.eligible_ids,
        asof,
        benchmark=benchmark,
        corporate=corporate,
    )

    names = strategies or available()
    saved: list[StrategySnapshot] = []
    skipped: dict[str, str] = {}
    snapshot_ts = _midnight(asof)
    for name in names:
        try:
            allocator = get_allocator(name)
            allocator.asof(asof)
            weights = allocator.allocate(screened.eligible, features)
        except (KeyError, RuntimeError, FileNotFoundError, ValueError) as exc:
            if strategies is not None:
                raise ValueError(f"cannot resolve strategy {name!r}: {exc}") from exc
            skipped[name] = str(exc)
            continue
        params = {
            "benchmark": config.benchmark_symbol,
            "bar_asof": latest_day.isoformat(),
            "eligible_names": len(screened.eligible),
            "universe_names": len(universe),
        }
        row = session.scalar(
            select(StrategySnapshot).where(
                StrategySnapshot.strategy == name, StrategySnapshot.ts == snapshot_ts
            )
        )
        if row is None:
            row = StrategySnapshot(
                strategy=name,
                model_version=allocator.version,
                feature_set_version=FEATURE_SET_VERSION,
                ts=snapshot_ts,
                weights={},
                params={},
                created_at=datetime.now(tz=UTC),
            )
            session.add(row)
        row.model_version = allocator.version
        row.feature_set_version = FEATURE_SET_VERSION
        row.weights = {str(iid): weight for iid, weight in weights.items()}
        row.params = params
        saved.append(row)
    session.flush()
    return SnapshotRunResult(saved, skipped, latest_day, stale_days)


def _return_over_horizon(series: Series, fx: Series | None, start: date, end: date) -> float | None:
    indices = [i for i, day in enumerate(series.dates) if start < day <= end]
    if not indices:
        return None
    first, last = indices[0], indices[-1]
    first_cad = series.closes[first] * _fx_to_cad(series.currency, fx, series.dates[first])
    last_cad = series.closes[last] * _fx_to_cad(series.currency, fx, series.dates[last])
    return last_cad / first_cad - 1.0 if first_cad else None


def _snapshot_return(
    session: Session,
    snapshot: StrategySnapshot,
    horizon_end: date,
    *,
    fx: Series | None,
) -> tuple[float, dict[int, tuple[Series, float]]] | None:
    holdings: dict[int, tuple[Series, float]] = {}
    total = 0.0
    for raw_iid, weight in snapshot.weights.items():
        iid = int(raw_iid)
        instrument = session.get(Instrument, iid)
        if instrument is None:
            return None
        series = _load_series(session, instrument.symbol, snapshot.ts, _midnight(horizon_end))
        if series is None:
            return None
        realized = _return_over_horizon(series, fx, snapshot.ts.date(), horizon_end)
        if realized is None:
            return None
        total += float(weight) * realized
        holdings[iid] = (series, float(weight))
    return total, holdings


def _turnover_cost_return(
    snapshot: StrategySnapshot,
    previous: StrategySnapshot | None,
    holdings: dict[int, tuple[Series, float]],
    cost_model: RegisteredAccountCostModel,
    *,
    fx: Series | None,
    notional_cad: float = 100_000.0,
) -> float:
    if previous is None:
        return 0.0
    previous_weights = {int(iid): float(w) for iid, w in previous.weights.items()}
    cost = 0.0
    for iid in set(previous_weights) | set(holdings):
        delta = holdings.get(iid, (None, 0.0))[1] - previous_weights.get(iid, 0.0)  # type: ignore[index]
        if not delta:
            continue
        series = holdings.get(iid, (None, 0.0))[0]
        if series is None or not series.closes:
            # A full exit still pays the variable component; omit only the unknown commission.
            cost += (
                abs(delta)
                * notional_cad
                * (cost_model.slippage_bps + cost_model.spread_bps + cost_model.churn_penalty_bps)
                / 10_000
            )
            continue
        price_cad = series.closes[0] * _fx_to_cad(series.currency, fx, series.dates[0])
        cost += cost_model.trade_cost(abs(delta) * notional_cad / price_cad, price_cad)
    return cost / notional_cad


def build_snapshot_report(
    session: Session,
    *,
    horizon_months: int = 1,
    asof: date | None = None,
    cost_model: RegisteredAccountCostModel | None = None,
) -> list[SnapshotReportRow]:
    """Score mature snapshots against XEQT using strictly post-snapshot daily bars."""
    if horizon_months < 1:
        raise ValueError("horizon_months must be at least 1")
    asof = asof or datetime.now(tz=UTC).date()
    cost_model = cost_model or RegisteredAccountCostModel()
    snapshots = list(session.scalars(select(StrategySnapshot).order_by(StrategySnapshot.ts)))
    if not snapshots:
        return []
    snapshots = [row for row in snapshots if _add_months(row.ts.date(), horizon_months) <= asof]
    if not snapshots:
        return []
    earliest = min(row.ts for row in snapshots)
    fx = _load_series(session, "USDCAD", earliest, _midnight(asof))
    benchmark = _load_series(session, "XEQT", earliest, _midnight(asof))
    if benchmark is None:
        raise ValueError("XEQT daily bars are required for snapshot reporting")

    excess_by_strategy: dict[str, list[float]] = {}
    previous_by_strategy: dict[str, StrategySnapshot] = {}
    for snapshot in snapshots:
        horizon_end = _add_months(snapshot.ts.date(), horizon_months)
        previous = previous_by_strategy.get(snapshot.strategy)
        previous_by_strategy[snapshot.strategy] = snapshot
        if horizon_end > asof:
            continue
        outcome = _snapshot_return(session, snapshot, horizon_end, fx=fx)
        benchmark_return = _return_over_horizon(benchmark, fx, snapshot.ts.date(), horizon_end)
        if outcome is None or benchmark_return is None:
            continue
        portfolio_return, holdings = outcome
        cost_return = _turnover_cost_return(snapshot, previous, holdings, cost_model, fx=fx)
        excess_by_strategy.setdefault(snapshot.strategy, []).append(
            portfolio_return - cost_return - benchmark_return
        )
    return [
        SnapshotReportRow(
            strategy=strategy,
            horizon_months=horizon_months,
            mean_excess=sum(values) / len(values),
            hit_rate=sum(value > 0 for value in values) / len(values),
            n_snapshots=len(values),
        )
        for strategy, values in sorted(excess_by_strategy.items())
    ]
