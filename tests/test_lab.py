"""Strategy lab: windows, recency scoring, calendar returns, and a full synthetic run."""

import math
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ibkr_trader.accounts import AccountType
from ibkr_trader.backtest import lab
from ibkr_trader.backtest.engine import RegisteredStrategyConfig, Series
from ibkr_trader.db.models import Base, Instrument, PriceBar
from ibkr_trader.signals.eligibility import EligibilityLimits

END = date(2026, 10, 5)


def test_recent_windows_full_plus_trailing_with_normalized_weights():
    windows = lab.recent_windows(END, date(2010, 1, 4))
    assert [w.label for w in windows] == ["Since 2010", "Last 5y", "Last 3y", "Last 1y"]
    assert [w.eval_start for w in windows] == [
        date(2010, 1, 4),
        date(2021, 10, 5),
        date(2023, 10, 5),
        date(2025, 10, 5),
    ]
    assert [w.weight for w in windows] == pytest.approx([0.1, 0.2, 0.3, 0.4])


def test_recent_windows_drops_trailing_windows_older_than_the_floor():
    windows = lab.recent_windows(END, date(2024, 1, 1))
    assert [w.label for w in windows] == ["Since 2024", "Last 1y"]
    assert [w.weight for w in windows] == pytest.approx([0.2, 0.8])


def test_recent_windows_handles_a_leap_day_end():
    windows = lab.recent_windows(date(2028, 2, 29), date(2010, 1, 4))
    assert windows[-1].eval_start == date(2027, 2, 28)


def test_calendar_year_returns_chain_from_the_prior_close():
    curve = [
        (date(2024, 12, 30), 100.0),
        (date(2024, 12, 31), 110.0),
        (date(2025, 1, 2), 121.0),
        (date(2025, 12, 31), 132.0),
        (date(2026, 1, 2), 99.0),
    ]
    years = lab.calendar_year_returns(curve)
    assert years[2024] == pytest.approx(0.10)  # partial first year: from its own first day
    assert years[2025] == pytest.approx(132 / 110 - 1)  # from 2024's last close
    assert years[2026] == pytest.approx(99 / 132 - 1)
    assert lab.calendar_year_returns([]) == {}


def _spec(name: str) -> lab.StrategySpec:
    return lab.StrategySpec(name, name, "", lab.CouchPotatoAllocator, 12, 0.05)


def test_recency_scores_weight_recent_windows_and_skip_missing_runs():
    windows = [
        lab.Window("old", date(2010, 1, 1), 0.25),
        lab.Window("new", date(2025, 1, 1), 0.75),
    ]
    result = lab.LabResult(
        specs=[_spec(lab.REFERENCE), _spec("a"), _spec("b")], windows=windows, account="tfsa"
    )
    for strategy, window, cagr in [
        (lab.REFERENCE, "old", 0.10),
        (lab.REFERENCE, "new", 0.10),
        ("a", "old", 0.30),  # won long ago…
        ("a", "new", 0.05),  # …lags now
        ("b", "new", 0.14),  # only has the recent window
    ]:
        result.runs.append(lab.LabRun(strategy, window, "tfsa", {"cagr": cagr}, []))
    scores = lab.recency_scores(result)
    assert scores[lab.REFERENCE] == 0.0
    assert scores["a"] == pytest.approx(0.25 * 0.20 + 0.75 * -0.05)
    assert scores["b"] == pytest.approx(0.04)
    assert result.run("a", "new", "rrsp") is None


def _calendar(n: int, start: date = date(2023, 1, 2)) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def _series(iid: int, symbol: str, currency: str, cal: list[date], drift: float) -> Series:
    closes = [50.0 * math.exp(drift * i + 0.01 * math.sin(i / 3 + iid)) for i in range(len(cal))]
    return Series(
        instrument_id=iid,
        symbol=symbol,
        currency=currency,
        exchange="TSX" if currency == "CAD" else "SMART",
        dates=list(cal),
        opens=list(closes),
        closes=closes,
        volumes=[1_000_000.0] * len(cal),
    )


def _synthetic_universe() -> tuple[dict[int, Series], Series]:
    cal = _calendar(900)
    specs = [
        ("XIC", "CAD", 0.0003),
        ("SPY", "USD", 0.0004),
        ("EFA", "USD", 0.0002),
        ("EEM", "USD", 0.0001),
        *[(f"STK{i}", "CAD" if i % 2 else "USD", 0.0001 * i - 0.0002) for i in range(10)],
    ]
    universe = {i: _series(i, sym, cur, cal, drift) for i, (sym, cur, drift) in enumerate(specs, 1)}
    fx = Series(0, "USDCAD", "CAD", "IDEALPRO", list(cal), [1.35] * 900, [1.35] * 900, [0.0] * 900)
    return universe, fx


OPEN_CONFIG = RegisteredStrategyConfig(
    eligibility=EligibilityLimits(min_price=0.0, min_avg_dollar_volume=0.0, min_history_days=260)
)
OPEN = lab.LabSettings(base_config=OPEN_CONFIG)
NO_COMPARE = lab.LabSettings(compare_account=None, base_config=OPEN_CONFIG)


def test_run_lab_runs_every_spec_window_and_the_compare_account():
    universe, fx = _synthetic_universe()
    end = max(universe[1].dates)
    windows = lab.recent_windows(end, date(2024, 3, 1), weights={1: 0.7, None: 0.3})
    specs = lab.default_specs()
    result = lab.run_lab(lab.LabInputs(universe, fx), specs, windows, OPEN)

    assert len(result.runs) == len(specs) * len(windows) + len(specs)
    assert {r.account for r in result.runs} == {"tfsa", "rrsp"}
    assert result.asof == end and result.universe_n == len(universe)
    for run in result.runs:
        assert run.equity_curve, run
        window = next(w for w in windows if w.label == run.window)
        # fresh start: no equity point before the window's first decision
        assert run.equity_curve[0][0] >= window.eval_start
        assert "avg_holding_years" in run.metrics

    reference = result.holdings[lab.REFERENCE]
    assert dict(reference) == pytest.approx({"XIC": 0.25, "SPY": 0.45, "EFA": 0.25, "EEM": 0.05})
    assert all(symbol.startswith("STK") for symbol, _ in result.holdings["recent_momentum"])
    assert lab.recency_scores(result)[lab.REFERENCE] == 0.0

    # RRSP pays no US-dividend withholding; TFSA does on the US sleeves
    first = windows[0].label
    assert result.run(lab.REFERENCE, first, "rrsp").metrics["tax_cad"] == 0.0
    assert result.run(lab.REFERENCE, first).metrics["tax_cad"] > 0.0


def test_run_lab_without_compare_account_and_with_mood():
    universe, fx = _synthetic_universe()
    windows = lab.recent_windows(max(universe[1].dates), date(2025, 1, 1), weights={None: 1.0})
    specs = lab.default_specs(mood=lambda symbols, day: {})
    assert specs[-1].name == "mood_tilt"
    result = lab.run_lab(lab.LabInputs(universe, fx), specs, windows, NO_COMPARE)
    assert {r.account for r in result.runs} == {"tfsa"}
    # with no mood at all, the tilt is exactly recent_momentum
    label = windows[0].label
    assert (
        result.run("mood_tilt", label).equity_curve
        == result.run("recent_momentum", label).equity_curve
    )


def test_run_lab_rebalance_cadence_comes_from_the_spec():
    universe, fx = _synthetic_universe()
    windows = lab.recent_windows(max(universe[1].dates), date(2024, 3, 1), weights={None: 1.0})
    seen: list[date] = []

    class Spy(lab.CouchPotatoAllocator):
        def asof(self, day: date) -> None:
            seen.append(day)

    spec = replace(lab.default_specs()[0], build=Spy, rebalance_months=6)
    lab.run_lab(lab.LabInputs(universe, fx), [spec], windows, NO_COMPARE)
    simulated = seen[:-1]  # the last call is the day-one holdings snapshot
    months = {(d.year, d.month) for d in simulated}
    assert len(simulated) == len(months)
    gaps = [
        (b.year * 12 + b.month) - (a.year * 12 + a.month)
        for a, b in zip(simulated, simulated[1:], strict=False)
    ]
    assert gaps and all(gap == 6 for gap in gaps)


def test_run_lab_empty_universe_is_refused():
    with pytest.raises(ValueError, match="empty universe"):
        lab.run_lab(lab.LabInputs({}), lab.default_specs(), [])


def test_default_specs_review_no_more_than_quarterly():
    for spec in lab.default_specs(mood=lambda symbols, day: {}):
        assert spec.rebalance_months >= 3, spec.name
        assert spec.build() is not None


def _session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_load_lab_inputs_loads_warmup_bars_and_fx():
    session = _session()
    full_start = date(2024, 1, 2)
    first_bar = full_start - timedelta(days=400)  # inside the warm-up margin
    for symbol, currency in (("RY", "CAD"), ("USDCAD", "CAD")):
        instrument = Instrument(symbol=symbol, exchange="TSX", currency=currency)
        session.add(instrument)
        session.flush()
        for i in range(500):
            ts = datetime.combine(first_bar + timedelta(days=i), datetime.min.time(), UTC)
            session.add(
                PriceBar(
                    instrument_id=instrument.id,
                    ts=ts,
                    bar_size="1 day",
                    source="yahoo",
                    what_to_show="ADJUSTED_LAST",
                    open=100.0,
                    high=100.0,
                    low=100.0,
                    close=100.0,
                    volume=1_000.0,
                )
            )
    session.flush()
    last_bar = first_bar + timedelta(days=499)
    inputs = lab.load_lab_inputs(session, ["RY"], full_start, last_bar)
    (series,) = inputs.universe.values()
    assert series.symbol == "RY" and series.dates[0] == first_bar
    assert series.dates[-1] == last_bar  # the end day itself is included
    assert inputs.fx is not None and inputs.fx.symbol == "USDCAD"
    assert set(inputs.corporate or {}) == set(inputs.universe)


def test_lab_eligibility_has_no_penny_stocks_and_two_years_of_history():
    assert lab.LAB_ELIGIBILITY.min_price >= 5.0
    assert lab.LAB_ELIGIBILITY.min_history_days >= 504
    assert lab.LAB_ELIGIBILITY.exclude_leveraged


def test_run_lab_account_defaults_to_tfsa_with_an_rrsp_comparison():
    universe, fx = _synthetic_universe()
    windows = lab.recent_windows(max(universe[1].dates), date(2025, 1, 1), weights={None: 1.0})
    result = lab.run_lab(lab.LabInputs(universe, fx), lab.default_specs()[:1], windows, OPEN)
    assert result.account == AccountType.TFSA.value
    assert result.compare_account == AccountType.RRSP.value
    assert lab.LabSettings().base_config.eligibility == lab.LAB_ELIGIBILITY


def test_current_holdings_is_a_fresh_day_one_book_largest_first():
    universe, fx = _synthetic_universe()
    day = max(universe[1].dates)
    spec = next(s for s in lab.default_specs() if s.name == lab.REFERENCE)
    book = lab.current_holdings(lab.LabInputs(universe, fx), day, spec, OPEN_CONFIG)
    assert [symbol for symbol, _ in book] == ["SPY", "EFA", "XIC", "EEM"]  # ties by symbol
    early = universe[1].dates[10]  # nothing has 260 days of history yet: all cash
    assert lab.current_holdings(lab.LabInputs(universe, fx), early, spec, OPEN_CONFIG) == []
