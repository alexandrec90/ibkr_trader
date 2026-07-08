"""Feature core contract: no look-ahead, pre-refactor engine parity, value sanity, and
idempotent versioned snapshots in the ``features`` table."""

import math
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

import numpy as np
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ibkr_trader.accounts import AccountType, get_profile
from ibkr_trader.backtest.costs import RegisteredAccountCostModel
from ibkr_trader.backtest.engine import RegisteredStrategyConfig, Series, simulate
from ibkr_trader.db.models import Base, Dividend, Feature, Instrument, PriceBar, ShareCount
from ibkr_trader.signals import features as features_mod
from ibkr_trader.signals.eligibility import Candidate, EligibilityLimits
from ibkr_trader.signals.features import (
    FEATURE_SET_VERSION,
    CorporateData,
    FeatureInputs,
    build_daily_features,
    build_feature_payload,
    build_features_asof,
)
from ibkr_trader.signals.portfolio import Allocator, Weights


def _dates(n: int, start: date = date(2020, 1, 6)) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def _wavy(n: int, base: float = 100.0) -> list[float]:
    """Deterministic, non-trivial price path: gentle trend + oscillation."""
    return [base + 0.05 * i + 5.0 * math.sin(i / 7.0) for i in range(n)]


def _inputs(n: int = 400, **overrides) -> FeatureInputs:
    dates = _dates(n)
    fields = {
        "dates": dates,
        "closes": _wavy(n),
        "volumes": [1_000_000.0 + 1_000.0 * i for i in range(n)],
        "dividends": tuple(
            (date(2016, 1, 15) + timedelta(days=91 * q), 0.20 + 0.01 * q) for q in range(24)
        ),
        "share_counts": ((dates[0], 1.0e9), (dates[100], 1.02e9)),
        "sector": "Technology",
        "benchmark_dates": dates,
        "benchmark_closes": _wavy(n, base=50.0),
    }
    fields.update(overrides)
    return FeatureInputs(**fields)


# --- the no-look-ahead contract -------------------------------------------------------------


def test_appending_future_data_never_changes_features_at_t():
    inputs = _inputs(400)
    t = inputs.dates[380]
    before = build_features_asof(inputs, t)

    # extend every input stream with post-t data: bars that crash, a fat dividend, a split-like
    # share-count jump, and a benchmark melt-up — none of it may leak backward.
    n_ext = 460
    ext_dates = _dates(n_ext)
    extended = FeatureInputs(
        dates=ext_dates,
        closes=list(inputs.closes) + [10.0] * 60,
        volumes=list(inputs.volumes) + [9e9] * 60,
        dividends=tuple(inputs.dividends) + ((t + timedelta(days=5), 50.0),),
        share_counts=tuple(inputs.share_counts) + ((t + timedelta(days=3), 5.0e9),),
        sector=inputs.sector,
        benchmark_dates=ext_dates,
        benchmark_closes=list(inputs.benchmark_closes) + [500.0] * 60,
    )
    after = build_features_asof(extended, t)

    assert before == after
    # and the fixture exercised the full v1 set, so the property covers every feature
    assert set(before) >= {
        "return_1m",
        "return_3m",
        "return_6m",
        "return_12m",
        "momentum_12_1",
        "volatility",
        "volatility_60d",
        "downside_deviation_252d",
        "max_drawdown_252d",
        "pct_off_52w_high",
        "volume_zscore_60d",
        "excess_return_3m",
        "excess_return_12m",
        "dividend_yield_ttm",
        "dividend_growth_3y",
        "log_market_cap",
    }


# --- parity with the pre-ML-02 engine (guards the momentum_lt baseline) ----------------------


def _legacy_engine_features(
    closes: Sequence[float], i: int, momentum_lookback: int = 252, vol_lookback: int = 252
) -> dict[str, float]:
    """Verbatim copy of the engine's `_features_asof` body before the ML-02 refactor."""
    feats: dict[str, float] = {}
    back = i - momentum_lookback
    if back >= 0 and closes[back] > 0:
        feats["return_12m"] = closes[i] / closes[back] - 1.0
    lo = max(0, i - vol_lookback + 1)
    window = closes[lo : i + 1]
    if len(window) >= 20:
        rets = np.diff(window) / np.asarray(window[:-1])
        feats["volatility"] = float(rets.std() * np.sqrt(252))
    return feats


def test_return_12m_and_volatility_match_pre_refactor_engine_exactly():
    dates = _dates(400)
    closes = _wavy(400)
    for i in (5, 19, 20, 100, 251, 252, 300, 399):
        legacy = _legacy_engine_features(closes, i)
        new = build_features_asof(FeatureInputs(dates=dates, closes=closes), dates[i])
        assert new.get("return_12m") == legacy.get("return_12m")
        assert new.get("volatility") == legacy.get("volatility")


# --- value sanity ----------------------------------------------------------------------------


def test_insufficient_history_yields_no_features_not_nans():
    inputs = FeatureInputs(dates=_dates(10), closes=[100.0] * 10, volumes=[1e6] * 10)
    assert build_features_asof(inputs, inputs.dates[-1]) == {}
    # day before the first bar → nothing at all
    assert build_features_asof(_inputs(400), date(2019, 1, 1)) == {}


def test_flat_series_features_are_all_zero():
    n = 300
    inputs = FeatureInputs(
        dates=_dates(n),
        closes=[100.0] * n,
        volumes=[1e6] * n,
        benchmark_dates=_dates(n),
        benchmark_closes=[50.0] * n,
    )
    feats = build_features_asof(inputs, inputs.dates[-1])
    for name in (
        "return_1m",
        "return_12m",
        "momentum_12_1",
        "volatility",
        "volatility_60d",
        "downside_deviation_252d",
        "max_drawdown_252d",
        "pct_off_52w_high",
        "volume_zscore_60d",
        "excess_return_12m",
    ):
        assert feats[name] == 0.0


def test_trailing_returns_and_momentum_arithmetic():
    n = 300
    closes = [100.0 * 1.001**i for i in range(n)]  # 0.1%/day compounding
    inputs = FeatureInputs(dates=_dates(n), closes=closes)
    i = n - 1
    feats = build_features_asof(inputs, inputs.dates[i])
    assert feats["return_1m"] == closes[i] / closes[i - 21] - 1.0
    assert feats["return_3m"] == closes[i] / closes[i - 63] - 1.0
    assert feats["return_6m"] == closes[i] / closes[i - 126] - 1.0
    assert feats["return_12m"] == closes[i] / closes[i - 252] - 1.0
    assert feats["momentum_12_1"] == closes[i - 21] / closes[i - 252] - 1.0
    # monotonic uptrend → today's close is the 52-week high
    assert feats["pct_off_52w_high"] == 0.0


def test_excess_returns_against_flat_benchmark_equal_own_returns():
    n = 300
    inputs = FeatureInputs(
        dates=_dates(n),
        closes=[100.0 * 1.001**i for i in range(n)],
        benchmark_dates=_dates(n),
        benchmark_closes=[50.0] * n,
    )
    feats = build_features_asof(inputs, inputs.dates[-1])
    assert feats["excess_return_3m"] == feats["return_3m"]
    assert feats["excess_return_12m"] == feats["return_12m"]


def test_dividend_and_market_cap_features():
    n = 300
    dates = _dates(n)
    day = dates[-1]
    dividends = (
        (day - timedelta(days=1300), 0.25),  # in the 3y-ago TTM window (and sets `earliest`)
        (day - timedelta(days=1150), 0.25),  # in the 3y-ago TTM window
        (day - timedelta(days=500), 0.40),  # in neither window
        (day - timedelta(days=200), 0.50),  # trailing 12 months
        (day - timedelta(days=30), 0.50),  # trailing 12 months
    )
    inputs = FeatureInputs(
        dates=dates,
        closes=[100.0] * n,
        dividends=dividends,
        share_counts=((day - timedelta(days=10), 1.0e9),),
    )
    feats = build_features_asof(inputs, day)
    assert feats["dividend_yield_ttm"] == 1.0 / 100.0
    assert feats["dividend_growth_3y"] == (1.0 / 0.5) ** (1 / 3) - 1.0
    assert feats["log_market_cap"] == math.log(100.0 * 1.0e9)


def test_missing_corporate_data_means_absent_but_empty_means_zero_yield():
    n = 300
    base = FeatureInputs(dates=_dates(n), closes=[100.0] * n)
    feats = build_features_asof(base, base.dates[-1])
    assert "dividend_yield_ttm" not in feats  # dividends=None → not ingested
    assert "log_market_cap" not in feats

    ingested_no_divs = FeatureInputs(
        dates=base.dates, closes=base.closes, dividends=(), share_counts=()
    )
    feats = build_features_asof(ingested_no_divs, base.dates[-1])
    assert feats["dividend_yield_ttm"] == 0.0  # a stock that genuinely pays nothing
    assert "dividend_growth_3y" not in feats  # no history → absent
    assert "log_market_cap" not in feats  # empty share counts → nothing ≤ day


def test_volume_zscore_flags_a_spike():
    n = 300
    volumes = [1e6] * n
    volumes[-1] = 3e6
    inputs = FeatureInputs(dates=_dates(n), closes=[100.0] * n, volumes=volumes)
    feats = build_features_asof(inputs, inputs.dates[-1])
    assert feats["volume_zscore_60d"] > 5.0


def test_sector_is_in_the_payload_but_not_the_numeric_dict():
    inputs = _inputs(400)
    day = inputs.dates[-1]
    numeric = build_features_asof(inputs, day)
    payload = build_feature_payload(inputs, day)
    assert "sector" not in numeric
    assert payload["sector"] == "Technology"
    assert {k: v for k, v in payload.items() if k != "sector"} == numeric


# --- engine integration: the simulator hands allocators feature set v1 -----------------------

OPEN_LIMITS = EligibilityLimits(
    min_price=0.0, min_avg_dollar_volume=0.0, min_history_days=1, exclude_leveraged=True
)
CHEAP = RegisteredAccountCostModel(
    commission_per_share=0.0,
    min_commission=0.0,
    slippage_bps=0.0,
    spread_bps=0.0,
    churn_penalty_bps=0.0,
    fx_conversion_bps=0.0,
)


class SpyAllocator(Allocator):
    """Records the features the engine hands it; allocates nothing."""

    name = "spy_test"
    version = "1"

    def __init__(self):
        self.seen: list[dict[int, dict[str, float]]] = []

    def allocate(self, candidates: Sequence[Candidate], features: dict) -> Weights:
        self.seen.append(features)
        return {}


def _series(iid: int, symbol: str, calendar: list[date], closes: list[float]) -> Series:
    return Series(
        instrument_id=iid,
        symbol=symbol,
        currency="CAD",
        exchange="TSX",
        dates=list(calendar),
        opens=list(closes),
        closes=list(closes),
        volumes=[1e6] * len(calendar),
    )


def test_simulate_features_degrade_without_corporate_and_extend_with_it():
    cal = _dates(300)
    universe = {
        1: _series(1, "AAA", cal, [100.0 * 1.001**i for i in range(300)]),
        2: _series(2, "XEQT", cal, [50.0] * 300),
    }
    config = RegisteredStrategyConfig(
        account=AccountType.RRSP, eligibility=OPEN_LIMITS, benchmark_symbol="XEQT"
    )

    spy = SpyAllocator()
    simulate(
        universe,
        cal,
        spy,
        cost_model=CHEAP,
        profile=get_profile(AccountType.RRSP),
        config=config,
    )
    last = spy.seen[-1][1]
    # price-only degradation: full price + benchmark-relative set, no corporate keys
    assert {"return_12m", "momentum_12_1", "volatility", "excess_return_12m"} <= set(last)
    assert "dividend_yield_ttm" not in last
    assert "log_market_cap" not in last

    corporate = {
        1: CorporateData(
            dividends=((cal[100], 1.0),),
            share_counts=((cal[50], 1.0e9),),
            sector="Technology",
        )
    }
    spy = SpyAllocator()
    simulate(
        universe,
        cal,
        spy,
        cost_model=CHEAP,
        profile=get_profile(AccountType.RRSP),
        config=config,
        corporate=corporate,
    )
    last = spy.seen[-1][1]
    assert "dividend_yield_ttm" in last
    # log(close_asof_rebalance × shares); closes span [100, ~135] so bound it instead of
    # pinning the engine's rebalance date
    assert math.log(100.0 * 1.0e9) < last["log_market_cap"] < math.log(140.0 * 1.0e9)
    assert "sector" not in last  # numeric dict only; sector is a payload concern


# --- the features table: versioned, idempotent snapshots --------------------------------------


def _sqlite_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _seed_instrument(session: Session, symbol: str, closes: list[float]) -> Instrument:
    instrument = Instrument(symbol=symbol, exchange="TSX", currency="CAD")
    session.add(instrument)
    session.flush()
    for i, close in enumerate(closes):
        session.add(
            PriceBar(
                instrument_id=instrument.id,
                ts=datetime(2020, 1, 1, tzinfo=UTC) + timedelta(days=i),
                bar_size="1 day",
                source="yahoo",
                what_to_show="ADJUSTED_LAST",
                open=close,
                high=close,
                low=close,
                close=close,
                volume=2_000_000.0,
            )
        )
    return instrument


def test_build_daily_features_persists_and_reruns_idempotently(monkeypatch):
    session = _sqlite_session()
    aaa = _seed_instrument(session, "AAA", [100.0 + i for i in range(40)])
    aaa.sector = "Financials"
    xeqt = _seed_instrument(session, "XEQT", [50.0] * 40)
    session.add(
        Dividend(instrument_id=aaa.id, ex_date=date(2020, 1, 20), amount=0.25, source="yahoo")
    )
    session.add(
        ShareCount(instrument_id=aaa.id, date=date(2020, 1, 10), shares=1.0e6, source="yahoo")
    )
    session.commit()

    days = [date(2020, 2, 8), date(2020, 2, 5)]  # unsorted on purpose
    count = build_daily_features(session, [aaa.id, xeqt.id], days)
    session.commit()
    assert count == 4  # 2 instruments × 2 days

    rows = session.scalars(select(Feature)).all()
    assert len(rows) == 4
    assert all(row.feature_set_version == FEATURE_SET_VERSION for row in rows)
    by_key = {(row.instrument_id, row.ts.date()) for row in rows}
    assert by_key == {(iid, d) for iid in (aaa.id, xeqt.id) for d in days}

    aaa_payload = next(r.payload for r in rows if r.instrument_id == aaa.id)
    assert aaa_payload["sector"] == "Financials"
    assert "dividend_yield_ttm" in aaa_payload
    assert "log_market_cap" in aaa_payload
    assert "return_1m" in aaa_payload

    # re-run: same count, no duplicate rows
    assert build_daily_features(session, [aaa.id, xeqt.id], days) == 4
    session.commit()
    assert len(session.scalars(select(Feature)).all()) == 4

    # a new feature-set version writes fresh rows instead of clobbering v1 snapshots
    monkeypatch.setattr(features_mod, "FEATURE_SET_VERSION", "999-test")
    build_daily_features(session, [aaa.id], [days[0]])
    session.commit()
    assert len(session.scalars(select(Feature)).all()) == 5


def test_build_daily_features_skips_days_before_first_bar_and_unknown_instruments():
    session = _sqlite_session()
    aaa = _seed_instrument(session, "AAA", [100.0] * 40)
    session.commit()

    count = build_daily_features(session, [aaa.id, 424242], [date(2019, 6, 1), date(2020, 2, 5)])
    session.commit()
    rows = session.scalars(select(Feature)).all()
    assert count == len(rows) == 1  # nothing for the pre-listing day or the unknown id
    assert rows[0].instrument_id == aaa.id
    assert rows[0].ts.date() == date(2020, 2, 5)
