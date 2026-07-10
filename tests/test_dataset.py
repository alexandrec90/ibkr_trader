"""Dataset contract (ML-03): rank label uniform per date, label reads only t→t+12m,
features read only ≤ t, forward-window exclusion, CAD conversion, eligibility reuse."""

import math
from datetime import UTC, date, datetime, timedelta

import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ibkr_trader.backtest.engine import Series
from ibkr_trader.db.models import Base, Dividend, Instrument, PriceBar, ShareCount
from ibkr_trader.signals.dataset import (
    LABEL_HORIZON_MONTHS,
    META_COLUMNS,
    build_dataset,
    build_dataset_from_panel,
    feature_columns,
    month_end_rebalance_dates,
)
from ibkr_trader.signals.eligibility import EligibilityLimits
from ibkr_trader.signals.features import CorporateData, FeatureInputs, build_features_asof

OPEN_LIMITS = EligibilityLimits(min_price=0.0, min_avg_dollar_volume=0.0, min_history_days=1)


def _daily_dates(n: int, start: date = date(2018, 1, 1)) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def _path(n: int, drift: float, wobble: float, base: float = 100.0) -> list[float]:
    """Deterministic price path: exponential drift + a sinusoid, always positive."""
    return [base * math.exp(drift * i) * (1.0 + 0.05 * math.sin(i / wobble)) for i in range(n)]


def _series(
    iid: int,
    symbol: str,
    dates: list[date],
    closes: list[float],
    *,
    currency: str = "CAD",
) -> Series:
    return Series(
        instrument_id=iid,
        symbol=symbol,
        currency=currency,
        exchange="TSX" if currency == "CAD" else "NYSE",
        dates=list(dates),
        opens=list(closes),
        closes=list(closes),
        volumes=[1e6] * len(dates),
    )


def _panel(n_days: int = 1200, n_names: int = 4) -> tuple[dict[int, Series], Series]:
    dates = _daily_dates(n_days)
    universe = {
        iid: _series(iid, f"S{iid}", dates, _path(n_days, 0.0001 * iid, 6.0 + iid))
        for iid in range(1, n_names + 1)
    }
    benchmark = _series(99, "XEQT", dates, _path(n_days, 0.0002, 9.0, base=50.0))
    return universe, benchmark


def _build(universe, benchmark, **kwargs):
    defaults = {
        "start": date(2018, 1, 1),
        "end": date(2030, 1, 1),
        "limits": OPEN_LIMITS,
    }
    defaults.update(kwargs)
    return build_dataset_from_panel(universe, benchmark, **defaults)


# --- rank label ------------------------------------------------------------------------------


def test_rank_label_is_uniform_in_0_1_per_date():
    universe, benchmark = _panel()
    df = _build(universe, benchmark)
    assert not df.empty
    for _, block in df.groupby("date"):
        n = len(block)
        if n > 1:
            assert sorted(block["label"]) == [k / (n - 1) for k in range(n)]
        # rank order must follow the raw forward excess return
        ordered = block.sort_values("fwd_excess_return")["label"].tolist()
        assert ordered == sorted(ordered)


def test_single_name_date_gets_midpoint_label():
    universe, benchmark = _panel(n_names=1)
    df = _build(universe, benchmark)
    assert not df.empty
    assert set(df["label"]) == {0.5}


# --- forward-window exclusion ----------------------------------------------------------------


def test_dataset_ends_exactly_one_horizon_before_the_last_bar():
    universe, benchmark = _panel()
    df = _build(universe, benchmark)
    rebalance = month_end_rebalance_dates(next(iter(universe.values())).dates)
    assert max(df["date"]) == rebalance[-(LABEL_HORIZON_MONTHS + 1)]


# --- no look-ahead ---------------------------------------------------------------------------


def test_features_use_only_data_asof_t():
    universe, benchmark = _panel()
    df = _build(universe, benchmark)
    features = feature_columns(df)
    row = df[df["date"] == max(df["date"])].iloc[0]
    t: date = row["date"]
    series = universe[int(row["instrument_id"])]
    cut = sum(1 for d in series.dates if d <= t)
    truncated = FeatureInputs(
        dates=series.dates[:cut],
        closes=series.closes[:cut],
        volumes=series.volumes[:cut],
        benchmark_dates=benchmark.dates,
        benchmark_closes=benchmark.closes,
    )
    expected = build_features_asof(truncated, t)
    for name in features:
        if name == "sector":
            continue
        if name in expected:
            assert row[name] == expected[name]
        else:
            assert np.isnan(row[name])


def test_label_reads_only_the_t_to_t_plus_12m_window():
    # 1186 days ends exactly on 2021-03-31, so extending the panel doesn't move any shared
    # month-end rebalance date (a partial final month would legitimately shift its endpoint)
    n_days = 1186
    universe, benchmark = _panel(n_days=n_days)
    assert next(iter(universe.values())).dates[-1] == date(2021, 3, 31)
    base = _build(universe, benchmark)

    # append 6 months of wild post-window bars to every series: existing rows must not move
    extra = 180
    dates_ext = _daily_dates(n_days + extra)
    universe_ext = {
        iid: _series(iid, series.symbol, dates_ext, list(series.closes) + [1000.0 + iid] * extra)
        for iid, series in universe.items()
    }
    benchmark_ext = _series(99, "XEQT", dates_ext, list(benchmark.closes) + [5.0] * extra)
    extended = _build(universe_ext, benchmark_ext)

    merged = base.merge(
        extended, on=["instrument_id", "date"], suffixes=("_base", "_ext"), how="left"
    )
    assert len(merged) == len(base)
    assert np.allclose(
        merged["fwd_excess_return_base"], merged["fwd_excess_return_ext"], atol=0, rtol=0
    )
    assert np.allclose(merged["label_base"], merged["label_ext"], atol=0, rtol=0)
    # and the extension added new, later rows (the horizon window shifted forward)
    assert max(extended["date"]) > max(base["date"])


# --- CAD conversion + label arithmetic --------------------------------------------------------


def test_usd_names_convert_through_the_fx_series():
    n = 800
    dates = _daily_dates(n)
    usd = _series(1, "USDNAME", dates, [100.0] * n, currency="USD")  # flat in USD
    benchmark = _series(99, "XEQT", dates, [50.0] * n)  # flat benchmark → excess = own return
    fx = _series(50, "USDCAD", dates, [1.30 + 0.0002 * i for i in range(n)])
    fx.currency = "CAD"

    df = _build({1: usd}, benchmark, fx=fx)
    assert not df.empty
    rebalance = month_end_rebalance_dates(dates)
    t = df.iloc[0]["date"]
    i = rebalance.index(t)
    t_fwd = rebalance[i + LABEL_HORIZON_MONTHS]
    fx_by_date = dict(zip(dates, fx.closes, strict=True))
    expected = fx_by_date[t_fwd] / fx_by_date[t] - 1.0  # pure FX appreciation, in CAD
    assert math.isclose(df.iloc[0]["fwd_excess_return"], expected, rel_tol=1e-12)


# --- eligibility reuse -----------------------------------------------------------------------


def test_ineligible_names_never_reach_the_dataset():
    n = 1200
    dates = _daily_dates(n)
    universe = {
        1: _series(1, "GOOD", dates, _path(n, 0.0002, 7.0)),
        2: _series(2, "PENNY", dates, [2.0] * n),  # below the default 5.00 price floor
    }
    benchmark = _series(99, "XEQT", dates, [50.0] * n)
    df = build_dataset_from_panel(
        universe, benchmark, start=dates[0], end=dates[-1], limits=EligibilityLimits()
    )
    assert set(df["symbol"]) == {"GOOD"}
    # and the default 252-day listing minimum delays the first labeled row past year one
    assert min(df["date"]) >= dates[0] + timedelta(days=252)


def test_history_floor_is_a_per_dataset_choice_and_history_is_a_feature():
    n = 1200
    dates = _daily_dates(n)
    young_dates = dates[200:]
    universe = {
        1: _series(1, "ESTABLISHED", dates, _path(n, 0.0002, 7.0)),
        2: _series(
            2,
            "YOUNG",
            young_dates,
            _path(len(young_dates), 0.0003, 8.0),
        ),
    }
    benchmark = _series(99, "XEQT", dates, [50.0] * n)
    common = {"start": dates[0], "end": dates[-1]}
    low = build_dataset_from_panel(
        universe, benchmark, limits=EligibilityLimits(min_history_days=63), **common
    )
    core = build_dataset_from_panel(
        universe, benchmark, limits=EligibilityLimits(min_history_days=252), **common
    )

    low_young = low[low["symbol"] == "YOUNG"]
    core_young = core[core["symbol"] == "YOUNG"]
    assert 63 <= low_young["history_days"].min() < 252
    assert core_young["history_days"].min() >= 252
    assert len(low_young) > len(core_young)


# --- sector + frame shape --------------------------------------------------------------------


def test_sector_is_a_column_and_meta_columns_are_not_features():
    universe, benchmark = _panel()
    corporate = {1: CorporateData(sector="Technology")}
    df = _build(universe, benchmark, corporate=corporate)
    assert "sector" in feature_columns(df)
    assert set(df[df["instrument_id"] == 1]["sector"]) == {"Technology"}
    assert df[df["instrument_id"] == 2]["sector"].isna().all()
    assert not set(feature_columns(df)) & set(META_COLUMNS)
    assert {"return_12m", "momentum_12_1", "volatility"} <= set(feature_columns(df))


# --- the DB wrapper --------------------------------------------------------------------------


def _sqlite_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _seed_instrument(
    session: Session, symbol: str, closes: list[float], *, currency: str = "CAD"
) -> Instrument:
    instrument = Instrument(symbol=symbol, exchange="TSX", currency=currency)
    session.add(instrument)
    session.flush()
    session.add_all(
        PriceBar(
            instrument_id=instrument.id,
            ts=datetime(2018, 1, 1, tzinfo=UTC) + timedelta(days=i),
            bar_size="1 day",
            source="yahoo",
            what_to_show="ADJUSTED_LAST",
            open=close,
            high=close,
            low=close,
            close=close,
            volume=2_000_000.0,
        )
        for i, close in enumerate(closes)
    )
    return instrument


def test_build_dataset_loads_bars_and_corporate_data_from_postgres():
    session = _sqlite_session()
    n = 480
    aaa = _seed_instrument(session, "AAA", _path(n, 0.0003, 7.0))
    aaa.sector = "Financials"
    _seed_instrument(session, "BBB", _path(n, 0.0001, 9.0))
    _seed_instrument(session, "XEQT", [50.0] * n)
    session.add(
        Dividend(instrument_id=aaa.id, ex_date=date(2018, 3, 1), amount=0.25, source="yahoo")
    )
    session.add(
        ShareCount(instrument_id=aaa.id, date=date(2018, 2, 1), shares=1.0e9, source="yahoo")
    )
    session.commit()

    df = build_dataset(
        session,
        ["AAA", "BBB", "XEQT"],
        datetime(2018, 1, 1, tzinfo=UTC),
        datetime(2019, 6, 1, tzinfo=UTC),
        limits=OPEN_LIMITS,
        horizon_months=3,  # small horizon keeps the fixture tiny; semantics identical
    )
    assert set(df["symbol"]) == {"AAA", "BBB", "XEQT"}
    aaa_rows = df[df["symbol"] == "AAA"]
    assert set(aaa_rows["sector"]) == {"Financials"}
    assert "dividend_yield_ttm" in feature_columns(df)
    # share count is dated 2018-02-01, so market cap exists from the Feb rebalance onward
    assert aaa_rows["log_market_cap"].notna().any()
