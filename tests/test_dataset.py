"""Dataset contract (ML-03): rank label uniform per date, label reads only t→t+12m,
features read only ≤ t, forward-window exclusion, CAD conversion, eligibility reuse."""

import math
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest
from pandera.errors import SchemaErrors
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ibkr_trader.backtest.engine import Series
from ibkr_trader.backtest.universe import SP500, UniverseSpec
from ibkr_trader.db.models import Base, Dividend, IndexMembership, Instrument, PriceBar, ShareCount
from ibkr_trader.signals.dataset import (
    LABEL_HORIZON_MONTHS,
    META_COLUMNS,
    TRAINING_LIMITS,
    DatasetBuild,
    build_dataset,
    build_dataset_from_panel,
    dataset_window,
    feature_columns,
    month_end_rebalance_dates,
)
from ibkr_trader.signals.eligibility import EligibilityLimits
from ibkr_trader.signals.features import CorporateData, FeatureInputs, build_features_asof
from ibkr_trader.signals.schemas import NUMERIC_FEATURE_COLUMNS

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
    # and an explicit 252-day listing minimum delays the first labeled row past year one
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


# --- young listings: the history floor is a holding rule, not a training filter ---------------


def test_default_training_screen_keeps_a_fresh_listing_from_its_first_month():
    n = 1200
    dates = _daily_dates(n)
    ipo_dates = dates[400:]
    universe = {
        1: _series(1, "ESTABLISHED", dates, _path(n, 0.0002, 7.0)),
        2: _series(2, "IPO", ipo_dates, _path(len(ipo_dates), 0.0004, 8.0)),
    }
    benchmark = _series(99, "XEQT", dates, [50.0] * n)

    df = build_dataset_from_panel(universe, benchmark, start=dates[0], end=dates[-1])

    assert TRAINING_LIMITS.min_history_days == 1
    ipo = df[df["symbol"] == "IPO"]
    first = ipo.sort_values("date").iloc[0]
    assert first["date"] == month_end_rebalance_dates(ipo_dates)[0]  # its very first month
    assert first["history_days"] < 31
    assert np.isnan(first["return_12m"])  # too young for the long window: NaN, not dropped


def test_a_series_starting_with_the_data_never_poses_as_a_fresh_listing():
    """Bars that begin when the loaded data begins say nothing about the listing's age."""
    universe, benchmark = _panel()  # every series starts on the panel's first day
    df = _build(universe, benchmark)  # min_history_days=1
    assert df["history_days"].min() >= 252


def _listed_panel(first_bar: int, listed_on: date | None) -> tuple[dict[int, Series], Series]:
    """ESTABLISHED from day 0 plus NEW, whose bars begin at ``first_bar`` (``listed_on`` set)."""
    n = 1200
    dates = _daily_dates(n)
    new_dates = dates[first_bar:]
    new = _series(2, "NEW", new_dates, _path(len(new_dates), 0.0004, 8.0))
    new.listed_on = listed_on
    universe = {1: _series(1, "ESTABLISHED", dates, _path(n, 0.0002, 7.0)), 2: new}
    return universe, _series(99, "XEQT", dates, [50.0] * n)


def _new_rows(universe: dict[int, Series], benchmark: Series):
    dates = benchmark.dates
    df = build_dataset_from_panel(universe, benchmark, start=dates[0], end=dates[-1])
    return df[df["symbol"] == "NEW"].sort_values("date")


def test_a_stored_listing_date_makes_a_series_at_the_data_start_a_fresh_listing():
    universe, benchmark = _listed_panel(0, listed_on=date(2018, 1, 1))  # listed with the data
    rows = _new_rows(universe, benchmark)
    assert rows.iloc[0]["date"] == month_end_rebalance_dates(benchmark.dates)[0]
    assert rows.iloc[0]["history_days"] <= 31  # its first month: January's 31 daily bars


def test_a_listing_long_before_the_first_bar_means_the_bars_begin_late():
    """An old company whose stored history starts late is not an IPO: it waits a year."""
    universe, benchmark = _listed_panel(400, listed_on=date(2001, 6, 1))
    assert _new_rows(universe, benchmark)["history_days"].min() >= 252


def test_a_listing_date_the_bars_contradict_is_ignored():
    """Bars before the stored date prove it wrong; the first bar is judged as without one."""
    late_claim = _daily_dates(1200)[900]
    universe, benchmark = _listed_panel(400, listed_on=late_claim)  # first bar is day 400
    assert _new_rows(universe, benchmark)["history_days"].min() < 31  # a listing, as before
    at_data_start, benchmark = _listed_panel(0, listed_on=late_claim)
    assert _new_rows(at_data_start, benchmark)["history_days"].min() >= 252


def test_training_screen_still_applies_price_and_liquidity_floors():
    n = 1200
    dates = _daily_dates(n)
    universe = {
        1: _series(1, "GOOD", dates, _path(n, 0.0002, 7.0)),
        2: _series(2, "PENNY", dates[300:], [2.0] * (n - 300)),  # young *and* a penny stock
    }
    benchmark = _series(99, "XEQT", dates, [50.0] * n)
    df = build_dataset_from_panel(universe, benchmark, start=dates[0], end=dates[-1])
    assert set(df["symbol"]) == {"GOOD"}


# --- right-censored labels: names that stop trading stay in ------------------------------------


def _dying_panel(last_bar: int, *, resume_at: int | None = None):
    """Two healthy names plus DEAD, whose bars stop after ``last_bar`` (optionally resuming)."""
    n = 1200
    dates = _daily_dates(n)
    universe = {
        1: _series(1, "ALIVE", dates, _path(n, 0.0002, 7.0)),
        2: _series(2, "ALSO", dates, _path(n, 0.0001, 9.0)),
    }
    keep = list(range(last_bar + 1)) + (list(range(resume_at, n)) if resume_at else [])
    dead_closes = _path(n, -0.0005, 5.0)
    universe[3] = _series(3, "DEAD", [dates[i] for i in keep], [dead_closes[i] for i in keep])
    benchmark = _series(99, "XEQT", dates, _path(n, 0.0002, 9.0, base=50.0))
    return universe, benchmark, dates


def test_a_delisted_name_keeps_its_rows_at_its_last_close():
    universe, benchmark, dates = _dying_panel(last_bar=700)
    dead = universe[3]
    df = _build(universe, benchmark)
    rows = df[df["symbol"] == "DEAD"]

    censored = rows[rows["label_censored"]]
    assert not censored.empty  # without censoring these rows silently vanish
    bench = dict(zip(benchmark.dates, benchmark.closes, strict=True))
    rebalance = month_end_rebalance_dates(dates)
    for row in censored.itertuples():
        t = row.date
        fwd = rebalance[rebalance.index(t) + LABEL_HORIZON_MONTHS]
        assert fwd > dead.dates[-1]
        own = dead.closes[-1] / dead.closes[dead.idx_asof(t)] - 1.0  # cashed out, then idle
        expected = own - (bench[fwd] / bench[t] - 1.0)
        assert math.isclose(row.fwd_excess_return, expected, rel_tol=1e-12)
    # labels are still ranked against the survivors on those dates
    assert censored["label"].between(0.0, 1.0).all()
    # rows whose window closes before the last bar are ordinary
    assert not rows[rows["date"] < dates[700] - timedelta(days=400)]["label_censored"].any()
    assert not df[df["symbol"] != "DEAD"]["label_censored"].any()


def test_an_ingestion_hole_that_resumes_is_unknown_not_censored():
    universe, benchmark, dates = _dying_panel(last_bar=700, resume_at=760)
    df = _build(universe, benchmark)
    rows = df[df["symbol"] == "DEAD"]
    hole = (dates[700], dates[760])
    rebalance = month_end_rebalance_dates(dates)
    fwd_in_hole = {
        t
        for i, t in enumerate(rebalance[:-LABEL_HORIZON_MONTHS])
        if hole[0] + timedelta(days=20) < rebalance[i + LABEL_HORIZON_MONTHS] < hole[1]
    }
    assert fwd_in_hole  # the fixture puts at least one label endpoint inside the hole
    assert not set(rows["date"]) & fwd_in_hole
    assert not rows["label_censored"].any()


def test_censored_flag_is_meta_not_a_feature():
    universe, benchmark, _ = _dying_panel(last_bar=700)
    df = _build(universe, benchmark)
    assert "label_censored" in META_COLUMNS
    assert "label_censored" not in feature_columns(df)
    assert df["label_censored"].dtype == bool


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
    assert set(NUMERIC_FEATURE_COLUMNS) <= set(feature_columns(df))


def test_dataset_rejects_nonfinite_assembled_features_by_default(monkeypatch):
    from ibkr_trader.signals import dataset as dataset_module

    universe, benchmark = _panel()

    def bad_features(universe, eligible_ids, day, **kwargs):
        return {iid: {"history_days": np.inf} for iid in eligible_ids}

    monkeypatch.setattr(dataset_module, "_features_asof", bad_features)
    with pytest.raises(SchemaErrors, match="finite_values"):
        _build(universe, benchmark)


def test_dataset_validation_escape_hatch_is_explicit(monkeypatch):
    from ibkr_trader.signals import dataset as dataset_module

    universe, benchmark = _panel()

    def bad_features(universe, eligible_ids, day, **kwargs):
        return {iid: {"history_days": np.inf} for iid in eligible_ids}

    monkeypatch.setattr(dataset_module, "_features_asof", bad_features)
    frame = _build(universe, benchmark, validate=False)
    assert np.isinf(frame["history_days"]).all()


# --- the DB wrapper --------------------------------------------------------------------------


def _sqlite_session() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _seed_instrument(
    session: Session,
    symbol: str,
    closes: list[float],
    *,
    currency: str = "CAD",
    first: datetime = datetime(2018, 1, 1, tzinfo=UTC),
) -> Instrument:
    instrument = Instrument(symbol=symbol, exchange="TSX", currency=currency)
    session.add(instrument)
    session.flush()
    session.add_all(
        PriceBar(
            instrument_id=instrument.id,
            ts=first + timedelta(days=i),
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

    built = build_dataset(
        session,
        ["AAA", "BBB", "XEQT"],
        datetime(2018, 1, 1, tzinfo=UTC),
        datetime(2019, 6, 1, tzinfo=UTC),
        limits=OPEN_LIMITS,
        horizon_months=3,  # small horizon keeps the fixture tiny; semantics identical
    )
    assert isinstance(built, DatasetBuild)
    df = built.frame
    assert set(df["symbol"]) == {"AAA", "BBB", "XEQT"}
    aaa_rows = df[df["symbol"] == "AAA"]
    assert set(aaa_rows["sector"]) == {"Financials"}
    assert "dividend_yield_ttm" in feature_columns(df)
    # share count is dated 2018-02-01, so market cap exists from the Feb rebalance onward
    assert aaa_rows["log_market_cap"].notna().any()
    assert built.universe["survivorship"] == "curated-current"
    assert built.universe["n_symbols"] == 3


def test_build_dataset_loads_warm_up_bars_so_rows_start_at_start():
    """Names alive before ``start`` are rows from its first month, with full lookbacks."""
    session = _sqlite_session()
    n = 1000
    early = datetime(2017, 1, 1, tzinfo=UTC)
    _seed_instrument(session, "AAA", _path(n, 0.0003, 7.0), first=early)
    _seed_instrument(session, "XEQT", [50.0] * n, first=early)
    session.commit()

    df = build_dataset(
        session,
        ["AAA", "XEQT"],
        datetime(2018, 3, 1, tzinfo=UTC),
        datetime(2019, 9, 1, tzinfo=UTC),
        limits=OPEN_LIMITS,
        horizon_months=3,
    ).frame
    assert min(df["date"]) == date(2018, 3, 31)  # nothing before start, nothing withheld
    assert df[df["date"] == date(2018, 3, 31)]["return_12m"].notna().all()


def test_dataset_window_adds_the_feature_warm_up_before_start():
    start, end = datetime(2018, 3, 1, tzinfo=UTC), datetime(2019, 9, 1, tzinfo=UTC)
    first, last = dataset_window(start, end)
    assert last == end
    assert (start - first).days >= 365  # a full year of bars for return_12m at start


def test_build_dataset_on_a_point_in_time_index_keeps_the_dead_member():
    session = _sqlite_session()
    n = 900
    live = _seed_instrument(session, "LIVE", _path(n, 0.0003, 7.0))
    dead = _seed_instrument(session, "DEAD", _path(500, -0.0004, 6.0))  # bars stop 2019-05-16
    _seed_instrument(session, "TODAY", _path(n, 0.0005, 8.0))  # never in the index
    _seed_instrument(session, "XEQT", [50.0] * n)
    for instrument, end in ((live, None), (dead, date(2019, 5, 16))):
        session.add(
            IndexMembership(
                index_code=SP500,
                symbol=instrument.symbol,
                start_date=date(2018, 1, 1),
                end_date=end,
                source="fja05680",
                instrument_id=instrument.id,
                resolution="yahoo",
            )
        )
    session.commit()

    built = build_dataset(
        session,
        UniverseSpec(("XEQT",), SP500),  # the index, with XEQT ungated beside it
        datetime(2018, 1, 1, tzinfo=UTC),
        datetime(2020, 6, 1, tzinfo=UTC),
        limits=OPEN_LIMITS,
        horizon_months=3,
    )
    df = built.frame
    assert set(df["symbol"]) == {"LIVE", "DEAD", "XEQT"}  # TODAY's survival earns it nothing
    assert df[df["symbol"] == "DEAD"]["label_censored"].any()
    assert built.universe["survivorship"] == "point-in-time"
    assert built.universe["extra_symbols"] == ["XEQT"]
