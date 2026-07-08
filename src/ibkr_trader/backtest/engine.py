"""Backtest engine — the long-term registered-account portfolio simulator.

Answers the one question the owner cares about: *if I had followed this model in this account,
how much money would I have made, after every real-world cost?*

Design goals:
- Reads bars ONLY from Postgres (no network) → reproducible runs.
- Realistic conditions are first-class: decide at close(t), fill at **open(t+1)** (no
  look-ahead); costs + churn penalty + US-dividend withholding on every relevant flow; a hard
  per-account annual trade budget; everything valued in **CAD** (US holdings carry FX risk).
- Long-only, capped weights (registered accounts can't short or lever).
- Results persist to ``backtest_runs`` (params + metrics JSON) so ``backtest.compare`` ranks
  models fairly — the account type, budget and cost model are pinned into ``params``.

Guardrails against classic backtest lies:
- Signals/eligibility computed at t use only bars ≤ t; fills happen the next session.
- Use ADJUSTED_LAST bars for returns (dividends reinvested; withholding modelled as a drag).
- Universe selection must not condition on the future (survivorship bias — IBKR has no
  delisted-ticker history).

The pure ``simulate()`` core runs on in-memory ``Series`` panels so the guardrails are unit
tested without a database; ``BacktestEngine.run()`` is the thin DB-loading wrapper around it.
"""

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ibkr_trader.accounts import AccountTaxProfile, AccountType, get_profile, security_domicile
from ibkr_trader.backtest import metrics as metrics_mod
from ibkr_trader.backtest.costs import RegisteredAccountCostModel
from ibkr_trader.db.models import BacktestRun, Instrument, PriceBar
from ibkr_trader.signals.eligibility import Candidate, EligibilityLimits, screen
from ibkr_trader.signals.features import (
    FEATURE_SET_VERSION,
    CorporateData,
    FeatureInputs,
    build_features_asof,
    load_corporate_inputs,
)
from ibkr_trader.signals.portfolio import Allocator, Weights, get_allocator


@dataclass
class CostModel:
    """Legacy single-name cost model, kept for the score-signal path. The portfolio simulator
    uses ibkr_trader.backtest.costs.RegisteredAccountCostModel."""

    commission_per_share: float = 0.005
    min_commission: float = 1.0
    slippage_bps: float = 5.0

    def cost(self, quantity: float, price: float) -> float:
        commission = max(self.min_commission, abs(quantity) * self.commission_per_share)
        slippage = abs(quantity) * price * self.slippage_bps / 10_000
        return commission + slippage


@dataclass
class Series:
    """One instrument's daily bars, sorted by date — the panel the simulator reasons over."""

    instrument_id: int
    symbol: str
    currency: str
    exchange: str
    dates: list[date]
    opens: list[float]
    closes: list[float]
    volumes: list[float]
    asset_class: str | None = None
    leveraged: bool = False

    def idx_asof(self, day: date) -> int:
        """Index of the latest bar on or before ``day``; -1 if none (not yet listed)."""
        return bisect_right(self.dates, day) - 1

    def open_on(self, day: date) -> float | None:
        """Open price on exactly ``day`` (None if the market wasn't open for it that day)."""
        i = bisect_right(self.dates, day) - 1
        return self.opens[i] if i >= 0 and self.dates[i] == day else None


@dataclass
class RegisteredStrategyConfig:
    """One backtest's parameters — persisted into ``backtest_runs.params`` for fair ranking."""

    account: AccountType = AccountType.TFSA
    start_capital: float = 100_000.0
    annual_trade_budget: int = 100  # per account, buys + sells (hard cap; cost fn does the work)
    rebalance_band: float = 0.05  # only trade a name whose weight drifts past this
    rebalance_months: int = 1  # consider rebalancing every N months
    # feature lookbacks (momentum, volatility, …) are pinned by the versioned feature set —
    # see signals.features.FEATURE_SET_VERSION — not per-run parameters.
    liquidity_lookback: int = 63
    benchmark_symbol: str = "XEQT"
    eligibility: EligibilityLimits = field(default_factory=EligibilityLimits)


@dataclass
class BacktestResult:
    strategy: str
    params: dict
    start: datetime
    end: datetime
    equity_curve: list = field(default_factory=list)  # (date, equity_cad) pairs
    metrics: dict = field(default_factory=dict)
    trades: int = 0
    costs_cad: float = 0.0
    tax_cad: float = 0.0
    fx_cost_cad: float = 0.0


def _fx_to_cad(currency: str, fx: Series | None, day: date) -> float:
    """CAD per unit of the instrument's currency on ``day`` (forward-filled). CAD → 1.0.

    ``fx`` is a USD→CAD series (close = CAD per 1 USD). Missing FX → 1.0 with the run treating
    prices as already-CAD (fine for CAD-only universes / tests)."""
    if currency.upper() == "CAD":
        return 1.0
    if fx is None:
        return 1.0
    i = fx.idx_asof(day)
    return fx.closes[i] if i >= 0 else 1.0


def _build_candidates(
    universe: dict[int, Series],
    fx: Series | None,
    day: date,
    config: RegisteredStrategyConfig,
) -> list[Candidate]:
    """Eligible-input candidates as-of ``day`` — dollar-volume normalized to CAD; all as-of."""
    candidates: list[Candidate] = []
    for series in universe.values():
        i = series.idx_asof(day)
        if i < 0:
            continue
        fx_rate = _fx_to_cad(series.currency, fx, day)
        lo = max(0, i - config.liquidity_lookback + 1)
        dollar_vol = [series.closes[j] * series.volumes[j] * fx_rate for j in range(lo, i + 1)]
        candidates.append(
            Candidate(
                instrument_id=series.instrument_id,
                symbol=series.symbol,
                currency=series.currency,
                exchange=series.exchange,
                last_price=series.closes[i],
                avg_dollar_volume=float(np.mean(dollar_vol)) if dollar_vol else 0.0,
                history_days=i + 1,
                asset_class=series.asset_class,
                leveraged=series.leveraged,
            )
        )
    return candidates


def _features_asof(
    universe: dict[int, Series],
    eligible_ids: set[int],
    day: date,
    *,
    benchmark: Series | None = None,
    corporate: dict[int, CorporateData] | None = None,
) -> dict[int, dict[str, float]]:
    """Feature set v1 (signals.features) as-of ``day`` — bars/corporate data ≤ day only.

    Missing ML-01 corporate data degrades to price-only features, never an error.
    """
    features: dict[int, dict[str, float]] = {}
    for iid in eligible_ids:
        series = universe[iid]
        corp = (corporate or {}).get(iid) or CorporateData()
        inputs = FeatureInputs(
            dates=series.dates,
            closes=series.closes,
            volumes=series.volumes,
            dividends=corp.dividends,
            share_counts=corp.share_counts,
            sector=corp.sector,
            benchmark_dates=benchmark.dates if benchmark is not None else None,
            benchmark_closes=benchmark.closes if benchmark is not None else None,
        )
        features[iid] = build_features_asof(inputs, day)
    return features


def _portfolio_value(
    cash: float,
    positions: dict[int, float],
    universe: dict[int, Series],
    fx: Series | None,
    day: date,
    use_open: bool = False,
) -> float:
    value = cash
    for iid, shares in positions.items():
        if shares == 0:
            continue
        series = universe[iid]
        i = series.idx_asof(day)
        if i < 0:
            continue
        price = series.opens[i] if use_open else series.closes[i]
        value += shares * price * _fx_to_cad(series.currency, fx, day)
    return value


def _execute(
    targets: Weights,
    positions: dict[int, float],
    cash: float,
    universe: dict[int, Series],
    fx: Series | None,
    day: date,
    cost_model: RegisteredAccountCostModel,
    config: RegisteredStrategyConfig,
    budget_left: int,
) -> tuple[float, int, float, float]:
    """Trade toward ``targets`` at ``day``'s open. Returns (cash, trades_done, cost_cad, fx_cad).

    Respects the rebalance band (skip small drifts) and the per-account annual trade budget
    (sells first to raise cash, then buys; when the budget is spent, remaining names are held).
    The account is funded in CAD, so the net value of trades in a non-CAD currency crosses the
    CAD↔USD boundary and pays the FX conversion spread (netted — a US→US reshuffle is free).
    """
    value = _portfolio_value(cash, positions, universe, fx, day, use_open=True)
    if value <= 0:
        return cash, 0, 0.0, 0.0

    # price/weight snapshot at the open, only for names tradable today
    priced: dict[int, float] = {}
    for iid in set(positions) | set(targets):
        open_native = universe[iid].open_on(day) if iid in universe else None
        if open_native is not None and open_native > 0:
            priced[iid] = open_native * _fx_to_cad(universe[iid].currency, fx, day)

    deltas: list[tuple[int, float]] = []  # (iid, delta_shares) needing a trade
    for iid, price_cad in priced.items():
        current_cad = positions.get(iid, 0.0) * price_cad
        current_w = current_cad / value
        target_w = targets.get(iid, 0.0)
        if abs(target_w - current_w) <= config.rebalance_band:
            continue
        delta_shares = (target_w * value - current_cad) / price_cad
        if delta_shares != 0:
            deltas.append((iid, delta_shares))

    # sells first (raise cash), then buys; each trade spends one unit of the annual budget
    deltas.sort(key=lambda kv: kv[1])
    trades_done = 0
    total_cost = 0.0
    net_fx_crossing = 0.0  # signed CAD value of non-CAD trades (buys +, sells -)
    for iid, delta_shares in deltas:
        if budget_left - trades_done <= 0:
            break
        price_cad = priced[iid]
        cost = cost_model.trade_cost(delta_shares, price_cad)
        cash -= delta_shares * price_cad + cost
        positions[iid] = positions.get(iid, 0.0) + delta_shares
        if abs(positions[iid]) < 1e-9:
            positions.pop(iid, None)
        trades_done += 1
        total_cost += cost
        if universe[iid].currency.upper() != "CAD":
            net_fx_crossing += delta_shares * price_cad

    fx_cost = cost_model.fx_conversion_cost(net_fx_crossing)
    cash -= fx_cost
    return cash, trades_done, total_cost, fx_cost


def simulate(
    universe: dict[int, Series],
    calendar: list[date],
    allocator: Allocator,
    *,
    fx: Series | None = None,
    cost_model: RegisteredAccountCostModel | None = None,
    profile: AccountTaxProfile | None = None,
    config: RegisteredStrategyConfig | None = None,
    strategy_name: str | None = None,
    corporate: dict[int, CorporateData] | None = None,
) -> BacktestResult:
    """Walk ``calendar`` day by day, rebalancing toward ``allocator``'s target weights.

    Pure over in-memory panels — no DB, no network. This is where the no-look-ahead, budget,
    FX and withholding rules live and are tested. ``corporate`` carries optional ML-01 data
    (dividends, share counts, sector) per instrument for the feature build; absent → the
    allocator sees price-only features.
    """
    config = config or RegisteredStrategyConfig()
    cost_model = cost_model or RegisteredAccountCostModel()
    profile = profile or get_profile(config.account)
    # benchmark series for benchmark-relative features, when it's part of the universe
    benchmark_series = next(
        (s for s in universe.values() if s.symbol.upper() == config.benchmark_symbol.upper()),
        None,
    )

    cash = config.start_capital
    positions: dict[int, float] = {}
    pending: Weights | None = None
    equity_curve: list[tuple[date, float]] = []
    total_trades = 0
    total_cost = 0.0
    total_tax = 0.0
    total_fx = 0.0
    trades_by_year: dict[int, int] = {}
    last_rebalance_month = -1

    for day in calendar:
        # 1. execute yesterday's decision at today's open (no look-ahead)
        if pending is not None:
            year = day.year
            spent = trades_by_year.get(year, 0)
            budget_left = max(0, config.annual_trade_budget - spent)
            cash, done, cost, fx_cost = _execute(
                pending, positions, cash, universe, fx, day, cost_model, config, budget_left
            )
            trades_by_year[year] = spent + done
            total_trades += done
            total_cost += cost
            total_fx += fx_cost
            pending = None

        # 2. dividend withholding drag on US-domiciled holdings, charged daily
        for iid, shares in positions.items():
            series = universe[iid]
            i = series.idx_asof(day)
            if i < 0 or shares == 0:
                continue
            drag = cost_model.dividend_tax_drag_daily(
                security_domicile(series.currency), profile, metrics_mod.TRADING_DAYS
            )
            if drag:
                holding_cad = shares * series.closes[i] * _fx_to_cad(series.currency, fx, day)
                tax = holding_cad * drag
                cash -= tax
                total_tax += tax

        # 3. mark to market at today's close
        equity_curve.append((day, _portfolio_value(cash, positions, universe, fx, day)))

        # 4. decide targets as-of today's close, for execution tomorrow
        month_index = day.year * 12 + day.month
        due = last_rebalance_month < 0 or (month_index - last_rebalance_month) >= (
            config.rebalance_months
        )
        if due:
            candidates = _build_candidates(universe, fx, day, config)
            result = screen(candidates, config.eligibility)
            features = _features_asof(
                universe, result.eligible_ids, day, benchmark=benchmark_series, corporate=corporate
            )
            pending = allocator.allocate(result.eligible, features)
            last_rebalance_month = month_index

    equity = np.asarray([value for _, value in equity_curve], dtype=float)
    summary = metrics_mod.summarize(equity) if len(equity) >= 2 else {}
    years = max(len(equity) / metrics_mod.TRADING_DAYS, 1e-9)
    summary.update(
        {
            "start_value_cad": float(equity[0]) if len(equity) else config.start_capital,
            "end_value_cad": float(equity[-1]) if len(equity) else config.start_capital,
            "n_trades": total_trades,
            "trades_per_year": total_trades / years,
            "costs_cad": total_cost,
            "tax_cad": total_tax,
            "fx_cost_cad": total_fx,
        }
    )
    start_dt = datetime.combine(calendar[0], datetime.min.time(), tzinfo=UTC)
    end_dt = datetime.combine(calendar[-1], datetime.min.time(), tzinfo=UTC)
    return BacktestResult(
        strategy=strategy_name or allocator.name,
        params={
            "account": config.account.value,
            "model_version": allocator.version,
            "feature_set_version": FEATURE_SET_VERSION,
            "horizon": "long_term",
            "annual_trade_budget": config.annual_trade_budget,
            "rebalance_band": config.rebalance_band,
            "rebalance_months": config.rebalance_months,
            "start_capital": config.start_capital,
            "benchmark": config.benchmark_symbol,
        },
        start=start_dt,
        end=end_dt,
        equity_curve=equity_curve,
        metrics=summary,
        trades=total_trades,
        costs_cad=total_cost,
        tax_cad=total_tax,
        fx_cost_cad=total_fx,
    )


class BacktestEngine:
    def __init__(self, cost_model: RegisteredAccountCostModel | None = None):
        self.cost_model = cost_model or RegisteredAccountCostModel()

    def run(
        self,
        strategy: str,
        symbols: list[str],
        start: datetime,
        end: datetime,
        *,
        config: RegisteredStrategyConfig | None = None,
        session: Session | None = None,
        persist: bool = True,
    ) -> BacktestResult:
        """Load daily bars for ``symbols`` from the DB and simulate ``strategy`` over them.

        ``strategy`` resolves to a registered ``Allocator``. Runs the strategy and a
        buy-and-hold benchmark, stamps the comparison into metrics, and persists a
        ``backtest_runs`` row unless ``persist=False``.
        """
        config = config or RegisteredStrategyConfig()
        allocator = get_allocator(strategy)

        if session is not None:
            return self._run_with_session(
                session, allocator, strategy, symbols, start, end, config, persist
            )
        from ibkr_trader.db.session import get_session

        with get_session() as owned:
            return self._run_with_session(
                owned, allocator, strategy, symbols, start, end, config, persist
            )

    def _run_with_session(
        self,
        session: Session,
        allocator: Allocator,
        strategy: str,
        symbols: list[str],
        start: datetime,
        end: datetime,
        config: RegisteredStrategyConfig,
        persist: bool,
    ) -> BacktestResult:
        universe = _load_universe(session, symbols, start, end)
        fx = _load_series(session, "USDCAD", start, end)
        if not universe:
            raise ValueError(f"no daily bars found for {symbols!r} in the requested window")

        calendar = sorted({day for series in universe.values() for day in series.dates})
        profile = get_profile(config.account)
        # ML-01 corporate data (dividends, share counts, sector) per instrument, when ingested;
        # instruments without it simply get price-only features.
        corporate = {iid: load_corporate_inputs(session, iid) for iid in universe}

        result = simulate(
            universe,
            calendar,
            allocator,
            fx=fx,
            cost_model=self.cost_model,
            profile=profile,
            config=config,
            strategy_name=strategy,
            corporate=corporate,
        )

        # buy-and-hold benchmark through the same engine (never rebalances after the first buy)
        benchmark = config.benchmark_symbol.upper()
        bench_universe = {
            iid: s for iid, s in universe.items() if s.symbol.upper() == benchmark
        } or universe
        bench = simulate(
            bench_universe,
            calendar,
            get_allocator("buy_and_hold"),
            fx=fx,
            cost_model=self.cost_model,
            profile=profile,
            config=config,
            strategy_name="buy_and_hold",
            corporate=corporate,
        )
        result.metrics["benchmark_end_value_cad"] = bench.metrics.get("end_value_cad")
        result.metrics["benchmark_total_return"] = bench.metrics.get("total_return")
        if "total_return" in result.metrics and "total_return" in bench.metrics:
            result.metrics["excess_return"] = (
                result.metrics["total_return"] - bench.metrics["total_return"]
            )

        if persist:
            session.add(
                BacktestRun(
                    strategy=result.strategy,
                    params=result.params,
                    start=result.start,
                    end=result.end,
                    metrics=result.metrics,
                    created_at=datetime.now(tz=UTC),
                )
            )
        return result


#: When one instrument has bars from several providers in the same what_to_show bucket, the
#: loader keeps exactly one provider — otherwise every date shows up once per provider and
#: returns/volumes silently double-count. Widest coverage in the window wins; this order
#: breaks ties (unknown sources rank last, alphabetically, so the pick stays deterministic).
SOURCE_PREFERENCE = ("ibkr", "fmp", "yahoo", "alpha_vantage", "finnhub")


def _pick_source(
    session: Session,
    instrument_id: int,
    bar_size: str,
    what_to_show: str,
    start: datetime,
    end: datetime,
) -> str | None:
    """The single source to load this instrument's bars from (None if it has no bars)."""
    counts: list[tuple[str, int]] = [
        (source, n_bars)
        for source, n_bars in session.execute(
            select(PriceBar.source, func.count())
            .where(
                PriceBar.instrument_id == instrument_id,
                PriceBar.bar_size == bar_size,
                PriceBar.what_to_show == what_to_show,
                PriceBar.ts >= start,
                PriceBar.ts <= end,
            )
            .group_by(PriceBar.source)
        ).all()
    ]
    if not counts:
        return None

    def rank(item: tuple[str, int]) -> tuple[int, int, str]:
        source, n_bars = item
        preference = (
            SOURCE_PREFERENCE.index(source)
            if source in SOURCE_PREFERENCE
            else len(SOURCE_PREFERENCE)
        )
        return (-n_bars, preference, source)

    return min(counts, key=rank)[0]


def _load_series(
    session: Session,
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    bar_size: str = "1 day",
    what_to_show: str = "ADJUSTED_LAST",
) -> Series | None:
    """Load one instrument's daily bars into a Series. Falls back to TRADES if no adjusted bars
    exist for it (so a universe ingested only as TRADES still runs). Bars come from exactly one
    source per instrument (see SOURCE_PREFERENCE) so multi-provider stores don't double-count."""
    instrument = session.scalar(select(Instrument).where(Instrument.symbol == symbol.upper()))
    if instrument is None:
        return None
    for wts in (what_to_show, "TRADES"):
        source = _pick_source(session, instrument.id, bar_size, wts, start, end)
        if source is None:
            continue
        rows = list(
            session.scalars(
                select(PriceBar)
                .where(
                    PriceBar.instrument_id == instrument.id,
                    PriceBar.bar_size == bar_size,
                    PriceBar.what_to_show == wts,
                    PriceBar.source == source,
                    PriceBar.ts >= start,
                    PriceBar.ts <= end,
                )
                .order_by(PriceBar.ts)
            )
        )
        if rows:
            return Series(
                instrument_id=instrument.id,
                symbol=instrument.symbol,
                currency=instrument.currency,
                exchange=instrument.exchange,
                dates=[bar.ts.date() for bar in rows],
                opens=[bar.open for bar in rows],
                closes=[bar.close for bar in rows],
                volumes=[bar.volume or 0.0 for bar in rows],
                asset_class=getattr(instrument, "asset_class", None),
                leveraged=bool(getattr(instrument, "leveraged", False)),
            )
    return None


def _load_universe(
    session: Session, symbols: list[str], start: datetime, end: datetime
) -> dict[int, Series]:
    universe: dict[int, Series] = {}
    for symbol in symbols:
        series = _load_series(session, symbol, start, end)
        if series is not None:
            universe[series.instrument_id] = series
    return universe
