"""Simulator guardrails on synthetic panels: no look-ahead, trade budget, FX, withholding."""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ibkr_trader.accounts import AccountType, get_profile
from ibkr_trader.backtest.costs import RegisteredAccountCostModel
from ibkr_trader.backtest.engine import (
    BacktestEngine,
    RegisteredStrategyConfig,
    Series,
    _load_series,
    simulate,
)
from ibkr_trader.db.models import BacktestRun, Base, Instrument, PriceBar
from ibkr_trader.signals.eligibility import Candidate, EligibilityLimits
from ibkr_trader.signals.portfolio import Allocator, Weights

# permissive screen so tiny synthetic panels pass eligibility
OPEN_LIMITS = EligibilityLimits(
    min_price=0.0, min_avg_dollar_volume=0.0, min_history_days=1, exclude_leveraged=True
)
# cheap cost model so arithmetic in assertions stays legible (all costs off, incl. FX)
CHEAP = RegisteredAccountCostModel(
    commission_per_share=0.0,
    min_commission=0.0,
    slippage_bps=0.0,
    spread_bps=0.0,
    churn_penalty_bps=0.0,
    fx_conversion_bps=0.0,
)
# only the FX conversion spread is on, to isolate currency-crossing cost
FX_ONLY = RegisteredAccountCostModel(
    commission_per_share=0.0,
    min_commission=0.0,
    slippage_bps=0.0,
    spread_bps=0.0,
    churn_penalty_bps=0.0,
    fx_conversion_bps=40.0,
)


class FixedAllocator(Allocator):
    """Targets fixed weights for whatever it's told, ignoring features — isolates engine
    mechanics from allocator logic in tests."""

    name = "fixed_test"
    version = "1"

    def __init__(self, weights: dict[int, float]):
        self._weights = weights

    def allocate(self, candidates: Sequence[Candidate], features: dict) -> Weights:
        eligible = {c.instrument_id for c in candidates}
        return {iid: w for iid, w in self._weights.items() if iid in eligible}


def _calendar(n: int, start: date = date(2020, 1, 6)) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def _series(iid, symbol, currency, calendar, closes, opens=None, volume=1_000_000.0):
    opens = opens if opens is not None else closes
    return Series(
        instrument_id=iid,
        symbol=symbol,
        currency=currency,
        exchange="TSX" if currency == "CAD" else "SMART",
        dates=list(calendar),
        opens=list(opens),
        closes=list(closes),
        volumes=[volume] * len(calendar),
    )


def _config(account=AccountType.RRSP, budget=100):
    return RegisteredStrategyConfig(
        account=account,
        start_capital=100_000.0,
        annual_trade_budget=budget,
        rebalance_band=0.05,
        eligibility=OPEN_LIMITS,
    )


def test_no_lookahead_first_bar_is_all_cash_and_fill_is_next_open():
    cal = _calendar(6)
    universe = {1: _series(1, "AAA", "CAD", cal, closes=[100.0] * 6)}
    result = simulate(
        universe,
        cal,
        FixedAllocator({1: 1.0}),
        cost_model=CHEAP,
        profile=get_profile(AccountType.RRSP),
        config=_config(),
    )
    equity = [v for _, v in result.equity_curve]
    # decision made at close(day0) must NOT be reflected on day0 — still 100% cash
    assert abs(equity[0] - 100_000.0) < 1e-6
    # the buy fills at day1's open; from then on we're invested (flat price → value ~ unchanged)
    assert result.trades == 1
    assert abs(equity[-1] - 100_000.0) < 1e-6  # flat price, zero cost


def test_trade_budget_is_a_hard_cap():
    cal = _calendar(10)
    universe = {
        1: _series(1, "AAA", "CAD", cal, closes=[100.0] * 10),
        2: _series(2, "BBB", "CAD", cal, closes=[100.0] * 10),
    }
    targets = {1: 0.5, 2: 0.5}
    capped = simulate(
        universe,
        cal,
        FixedAllocator(targets),
        cost_model=CHEAP,
        profile=get_profile(AccountType.RRSP),
        config=_config(budget=1),
    )
    assert capped.trades == 1  # wanted two buys, budget allowed one

    uncapped = simulate(
        universe,
        cal,
        FixedAllocator(targets),
        cost_model=CHEAP,
        profile=get_profile(AccountType.RRSP),
        config=_config(budget=100),
    )
    assert uncapped.trades == 2


def test_us_holding_carries_fx_return_in_cad():
    cal = _calendar(8)
    # flat USD price, USD/CAD rising ~10% → CAD equity should rise ~10%
    universe = {1: _series(1, "SPY", "USD", cal, closes=[100.0] * 8)}
    fx_rates = [1.30 + 0.13 * i / 7 for i in range(8)]  # 1.30 → 1.43
    fx = _series(99, "USDCAD", "CAD", cal, closes=fx_rates)
    result = simulate(
        universe,
        cal,
        FixedAllocator({1: 1.0}),
        fx=fx,
        cost_model=CHEAP,
        profile=get_profile(AccountType.RRSP),
        config=_config(),
    )
    assert result.metrics["total_return"] > 0.05  # FX gain flows through to CAD
    assert result.tax_cad == 0.0  # RRSP: US dividends treaty-exempt


def test_us_dividend_withholding_only_bites_taxable_registered_accounts():
    cal = _calendar(40)
    universe = {1: _series(1, "SPY", "USD", cal, closes=[100.0] * 40)}
    fx = _series(99, "USDCAD", "CAD", cal, closes=[1.30] * 40)

    tfsa = simulate(
        universe,
        cal,
        FixedAllocator({1: 1.0}),
        fx=fx,
        cost_model=CHEAP,
        profile=get_profile(AccountType.TFSA),
        config=_config(account=AccountType.TFSA),
    )
    rrsp = simulate(
        universe,
        cal,
        FixedAllocator({1: 1.0}),
        fx=fx,
        cost_model=CHEAP,
        profile=get_profile(AccountType.RRSP),
        config=_config(account=AccountType.RRSP),
    )
    assert tfsa.tax_cad > 0.0  # non-recoverable US withholding
    assert rrsp.tax_cad == 0.0  # treaty-exempt
    assert tfsa.metrics["end_value_cad"] < rrsp.metrics["end_value_cad"]


def test_canadian_holding_is_never_withheld():
    cal = _calendar(40)
    universe = {1: _series(1, "XIU", "CAD", cal, closes=[30.0] * 40)}
    result = simulate(
        universe,
        cal,
        FixedAllocator({1: 1.0}),
        cost_model=CHEAP,
        profile=get_profile(AccountType.TFSA),
        config=_config(account=AccountType.TFSA),
    )
    assert result.tax_cad == 0.0


def test_fx_conversion_charged_when_shifting_into_usd():
    cal = _calendar(6)  # single month → one rebalance, one purchase
    universe = {1: _series(1, "SPY", "USD", cal, closes=[100.0] * 6)}
    fx = _series(99, "USDCAD", "CAD", cal, closes=[1.30] * 6)
    result = simulate(
        universe,
        cal,
        FixedAllocator({1: 1.0}),
        fx=fx,
        cost_model=FX_ONLY,
        profile=get_profile(AccountType.RRSP),
        config=_config(),
    )
    # 100k CAD fully converted to USD → 40bps of 100k
    assert abs(result.fx_cost_cad - 100_000.0 * 0.0040) < 1.0
    assert result.fx_cost_cad > 0


def test_fx_conversion_only_on_the_currency_crossing_leg():
    cal = _calendar(6)
    universe = {
        1: _series(1, "XIU", "CAD", cal, closes=[100.0] * 6),  # CAD leg: no conversion
        2: _series(2, "SPY", "USD", cal, closes=[100.0] * 6),  # USD leg: converts
    }
    fx = _series(99, "USDCAD", "CAD", cal, closes=[1.30] * 6)
    result = simulate(
        universe,
        cal,
        FixedAllocator({1: 0.5, 2: 0.5}),
        fx=fx,
        cost_model=FX_ONLY,
        profile=get_profile(AccountType.RRSP),
        config=_config(),
    )
    # only the 50% USD leg crosses currency → 40bps of 50k, not 100k
    assert abs(result.fx_cost_cad - 50_000.0 * 0.0040) < 1.0


def test_cad_only_run_never_pays_fx_conversion():
    cal = _calendar(6)
    universe = {1: _series(1, "XIU", "CAD", cal, closes=[100.0] * 6)}
    result = simulate(
        universe,
        cal,
        FixedAllocator({1: 1.0}),
        cost_model=FX_ONLY,
        profile=get_profile(AccountType.TFSA),
        config=_config(account=AccountType.TFSA),
    )
    assert result.fx_cost_cad == 0.0


def test_eval_start_default_is_none_and_leaves_the_run_unchanged():
    assert RegisteredStrategyConfig().eval_start is None
    cal = _calendar(12)
    universe = {1: _series(1, "AAA", "CAD", cal, closes=[100.0 + i for i in range(12)])}
    result = simulate(
        universe,
        cal,
        FixedAllocator({1: 1.0}),
        cost_model=CHEAP,
        profile=get_profile(AccountType.RRSP),
        config=_config(),
    )
    assert "eval_start" not in result.params  # unset ⇒ params byte-identical to before
    assert [day for day, _ in result.equity_curve] == cal  # every bar has an equity point


def test_eval_start_holds_cash_and_starts_metrics_at_first_decision():
    cal = _calendar(40)
    closes = [100.0 + i for i in range(40)]
    universe = {1: _series(1, "AAA", "CAD", cal, closes=closes)}
    eval_start = cal[20]

    config = _config()
    config.eval_start = eval_start
    result = simulate(
        universe,
        cal,
        FixedAllocator({1: 1.0}),
        cost_model=CHEAP,
        profile=get_profile(AccountType.RRSP),
        config=config,
    )

    days = [day for day, _ in result.equity_curve]
    assert days[0] == eval_start  # no equity points (and no decisions) before eval_start
    assert result.start.date() == eval_start
    assert abs(result.equity_curve[0][1] - 100_000.0) < 1e-6  # still all cash at eval_start
    assert result.trades == 1  # the first decision fills at the next open, as always
    assert result.params["eval_start"] == eval_start.isoformat()

    # identical to simply starting the calendar at eval_start: warm-up bars only feed
    # features, they never leak into equity or decisions
    trimmed = simulate(
        universe,
        [day for day in cal if day >= eval_start],
        FixedAllocator({1: 1.0}),
        cost_model=CHEAP,
        profile=get_profile(AccountType.RRSP),
        config=_config(),
    )
    assert result.equity_curve == trimmed.equity_curve


def test_eval_start_after_last_bar_raises():
    cal = _calendar(5)
    universe = {1: _series(1, "AAA", "CAD", cal, closes=[100.0] * 5)}
    config = _config()
    config.eval_start = cal[-1] + timedelta(days=1)
    try:
        simulate(
            universe,
            cal,
            FixedAllocator({1: 1.0}),
            cost_model=CHEAP,
            profile=get_profile(AccountType.RRSP),
            config=config,
        )
        raise AssertionError("expected ValueError for eval_start past the last bar")
    except ValueError:
        pass


def _sqlite_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _seed_bars(session, instrument, closes, *, source="fmp", what_to_show="ADJUSTED_LAST"):
    for i, close in enumerate(closes):
        ts = datetime(2020, 1, 1, tzinfo=UTC) + timedelta(days=i)
        session.add(
            PriceBar(
                instrument_id=instrument.id,
                ts=ts,
                bar_size="1 day",
                source=source,
                what_to_show=what_to_show,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=2_000_000.0,
            )
        )


def _seed_instrument(session, symbol, closes, *, currency="CAD", exchange="TSX"):
    instrument = Instrument(symbol=symbol, exchange=exchange, currency=currency)
    session.add(instrument)
    session.flush()
    _seed_bars(session, instrument, closes)
    return instrument


def test_engine_run_loads_bars_benchmarks_and_persists():
    session = _sqlite_session()
    _seed_instrument(session, "AAA", [100.0 + i for i in range(40)])  # steady uptrend
    _seed_instrument(session, "XEQT", [50.0] * 40)  # flat benchmark
    session.commit()

    config = RegisteredStrategyConfig(
        account=AccountType.RRSP, eligibility=OPEN_LIMITS, benchmark_symbol="XEQT"
    )
    result = BacktestEngine(cost_model=CHEAP).run(
        "equal_weight",
        ["AAA", "XEQT"],
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2020, 3, 1, tzinfo=UTC),
        config=config,
        session=session,
        persist=True,
    )

    assert result.trades >= 1
    assert result.metrics["end_value_cad"] > 0
    # AAA rose while the flat XEQT benchmark didn't → strategy should beat buy-and-hold
    assert result.metrics["benchmark_end_value_cad"] is not None
    assert result.metrics["excess_return"] > 0
    # a backtest_runs row was persisted (only the strategy, not the benchmark)
    assert session.scalar(select(func.count()).select_from(BacktestRun)) == 1


def test_load_series_uses_one_source_and_never_double_counts():
    """Same instrument+bucket from two providers: widest coverage wins, no interleaving."""
    session = _sqlite_session()
    instrument = _seed_instrument(session, "AAA", [100.0] * 10)  # fmp, 10 days
    _seed_bars(session, instrument, [999.0] * 20, source="yahoo")  # yahoo, 20 days
    session.commit()

    series = _load_series(
        session, "AAA", datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 3, 1, tzinfo=UTC)
    )

    assert series is not None
    assert len(series.dates) == 20  # not 30: the fmp bars were not interleaved
    assert len(set(series.dates)) == len(series.dates)  # each date exactly once
    assert all(close == 999.0 for close in series.closes)  # every bar from the wider source


def test_load_series_source_tie_breaks_by_preference_order():
    session = _sqlite_session()
    instrument = _seed_instrument(session, "BBB", [100.0] * 10)  # fmp
    _seed_bars(session, instrument, [999.0] * 10, source="yahoo")  # same coverage
    session.commit()

    series = _load_series(
        session, "BBB", datetime(2020, 1, 1, tzinfo=UTC), datetime(2020, 3, 1, tzinfo=UTC)
    )

    assert series is not None
    assert len(series.dates) == 10
    assert all(close == 100.0 for close in series.closes)  # fmp outranks yahoo on a tie


def test_engine_run_raises_when_no_bars_found():
    session = _sqlite_session()
    session.commit()
    try:
        BacktestEngine().run(
            "equal_weight",
            ["NOPE"],
            datetime(2020, 1, 1, tzinfo=UTC),
            datetime(2020, 3, 1, tzinfo=UTC),
            config=RegisteredStrategyConfig(eligibility=OPEN_LIMITS),
            session=session,
        )
        raise AssertionError("expected ValueError for empty universe")
    except ValueError:
        pass
