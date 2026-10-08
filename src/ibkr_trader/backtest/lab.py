"""Strategy lab — run the registered-account strategies side by side, weighted to recent data.

One backtest says little; the owner's question is *which of these would I actually want in my
TFSA/RRSP, judged mostly on how the world behaves now?* The lab answers it by running every
strategy through the same engine (``simulate``: no look-ahead, costs, FX, withholding, trade
budget) over several **fresh-start windows** ending today — since the owner's 2010 floor, the
last 5, 3 and 1 years — and scoring each strategy by its CAGR edge over the do-nothing
``couch_potato`` reference, averaged with weights that favour the recent windows.

Each window is a separate run that *starts* at the window's first day (with warm-up bars for
the features), i.e. "if I had opened the account then", not a slice of one long run. Short
windows also carry less survivorship bias: the curated universe is today's survivors, and
the further back a run starts, the more that flatters it.

Pure over in-memory ``Series`` panels like the engine; ``load_lab_inputs`` is the thin DB
wrapper. Rendering lives in ``dashboard.lab_report``.
"""

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from ibkr_trader.accounts import AccountType
from ibkr_trader.backtest.costs import RegisteredAccountCostModel
from ibkr_trader.backtest.engine import (
    RegisteredStrategyConfig,
    Series,
    _build_candidates,
    _features_asof,
    _load_series,
    _load_universe,
    simulate,
)
from ibkr_trader.backtest.universe import SP500, Coverage, coverage, load_index_universe
from ibkr_trader.signals.eligibility import EligibilityLimits, screen
from ibkr_trader.signals.features import CorporateData, load_corporate_inputs
from ibkr_trader.signals.portfolio import (
    BROAD_ETF_SYMBOLS,
    Allocator,
    CoreSatelliteAllocator,
    CouchPotatoAllocator,
    MoodLookup,
    MoodTiltAllocator,
    RecentMomentumAllocator,
    SteadyCompoundersAllocator,
)

#: Owner policy: decisions start 2010-01-04 at the earliest (skips the 2008-09 recession).
DEFAULT_FULL_START = date(2010, 1, 4)
#: Recency weights for the composite score, by trailing-window length in years (None = the
#: full window). They sum to 1; windows that cannot be formed are dropped and the rest
#: renormalized.
WINDOW_WEIGHTS: dict[int | None, float] = {1: 0.4, 3: 0.3, 5: 0.2, None: 0.1}
#: The strategy every other one is measured against.
REFERENCE = "couch_potato"
#: Calendar days of bars loaded before the earliest decision so 12-month features are warm.
WARMUP_DAYS = 550

#: Universe labels: a hand-picked file of today's survivors, or an index as it stood each day.
CURATED = "curated file (today's survivors)"
POINT_IN_TIME = "S&P 500 point-in-time + broad ETFs"

#: Registered-account screen for the lab: the default no-penny-stock / liquidity floors, but
#: two years of listing history — a buy-and-hold book has no business holding a fresh IPO.
LAB_ELIGIBILITY = EligibilityLimits(min_history_days=504, min_avg_dollar_volume=2_000_000.0)


@dataclass(frozen=True)
class StrategySpec:
    """One strategy as the lab runs it: how to build it and how often it may trade."""

    name: str
    label: str
    description: str
    build: Callable[[], Allocator]
    rebalance_months: int
    rebalance_band: float


@dataclass(frozen=True)
class Window:
    label: str
    eval_start: date
    weight: float


@dataclass
class LabRun:
    strategy: str
    window: str
    account: str
    metrics: dict
    equity_curve: list[tuple[date, float]]


@dataclass
class LabResult:
    specs: list[StrategySpec]
    windows: list[Window]
    account: str
    runs: list[LabRun] = field(default_factory=list)
    holdings: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    asof: date | None = None
    compare_account: str | None = None
    mood_start: date | None = None
    universe_n: int = 0
    #: What the universe is — "SP500 point-in-time" or "curated file" — and, for an index
    #: universe, how much of it could be priced.
    universe_label: str = CURATED
    coverage: Coverage | None = None

    def run(self, strategy: str, window: str, account: str | None = None) -> LabRun | None:
        account = account or self.account
        return next(
            (
                r
                for r in self.runs
                if r.strategy == strategy and r.window == window and r.account == account
            ),
            None,
        )


def default_specs(mood: MoodLookup | None = None) -> list[StrategySpec]:
    """The lab's line-up. ``mood_tilt`` joins only when a mood panel is supplied."""
    specs = [
        StrategySpec(
            name=REFERENCE,
            label="Couch potato (reference)",
            description="Fixed XEQT-like ETF mix: 25% XIC, 45% SPY, 25% EFA, 5% EEM. "
            "Rebalanced once a year at most.",
            build=CouchPotatoAllocator,
            rebalance_months=12,
            rebalance_band=0.05,
        ),
        StrategySpec(
            name="core_satellite",
            label="Core + satellite",
            description="70% couch-potato core, 30% in the five strongest recent-momentum "
            "stocks. Reviewed twice a year.",
            build=CoreSatelliteAllocator,
            rebalance_months=6,
            rebalance_band=0.04,
        ),
        StrategySpec(
            name="recent_momentum",
            label="Recent momentum",
            description="12 stocks with the strongest recency-weighted, volatility-adjusted "
            "uptrend; each kept while it stays in the top 48. Reviewed twice a year.",
            build=RecentMomentumAllocator,
            rebalance_months=6,
            rebalance_band=0.05,
        ),
        StrategySpec(
            name="steady_compounders",
            label="Steady compounders",
            description="15 of the calmest stocks that are still rising (low recent "
            "volatility, shallow drawdown). Reviewed twice a year.",
            build=SteadyCompoundersAllocator,
            rebalance_months=6,
            rebalance_band=0.03,
        ),
    ]
    if mood is not None:
        specs.append(
            StrategySpec(
                name="mood_tilt",
                label="Momentum + news mood",
                description="Recent momentum, tilted toward names the news and social feeds "
                "have been warm on lately and dropping the coldest. Identical to recent "
                "momentum until news data exists.",
                build=lambda: MoodTiltAllocator(mood),
                rebalance_months=6,
                rebalance_band=0.05,
            )
        )
    return specs


def _years_before(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 29 February
        return day.replace(year=day.year - years, day=28)


def recent_windows(
    end: date, full_start: date = DEFAULT_FULL_START, weights: dict | None = None
) -> list[Window]:
    """Full window plus trailing 5/3/1-year windows ending at ``end``, oldest first.

    A trailing window that would start on or before ``full_start`` duplicates the full run
    and is dropped; the surviving windows' weights are renormalized to sum to 1.
    """
    weights = weights or WINDOW_WEIGHTS
    windows: list[tuple[str, date, float]] = []
    if None in weights:
        windows.append((f"Since {full_start.year}", full_start, weights[None]))
    for years in sorted((y for y in weights if y is not None), reverse=True):
        start = _years_before(end, years)
        if start > full_start:
            windows.append((f"Last {years}y", start, weights[years]))
    total = sum(w for _, _, w in windows)
    return [Window(label, start, w / total) for label, start, w in windows]


def calendar_year_returns(equity_curve: list[tuple[date, float]]) -> dict[int, float]:
    """Return per calendar year (a partial first/last year covers only its own days)."""
    out: dict[int, float] = {}
    previous_close: float | None = None
    year_start: float | None = None
    current_year: int | None = None
    last_value = 0.0
    for day, value in equity_curve:
        if day.year != current_year:
            if current_year is not None and year_start:
                out[current_year] = last_value / year_start - 1.0
            current_year = day.year
            year_start = previous_close if previous_close is not None else value
        previous_close = value
        last_value = value
    if current_year is not None and year_start:
        out[current_year] = last_value / year_start - 1.0
    return out


def recency_scores(result: LabResult) -> dict[str, float]:
    """Recency-weighted CAGR edge over ``REFERENCE`` per strategy (reference itself = 0).

    Windows where either side has no run are skipped and the remaining weights renormalized.
    """
    scores: dict[str, float] = {}
    for spec in result.specs:
        num = den = 0.0
        for window in result.windows:
            run = result.run(spec.name, window.label)
            ref = result.run(REFERENCE, window.label)
            if run is None or ref is None:
                continue
            edge = run.metrics.get("cagr", 0.0) - ref.metrics.get("cagr", 0.0)
            num += window.weight * edge
            den += window.weight
        if den > 0:
            scores[spec.name] = num / den
    return scores


@dataclass
class LabInputs:
    """The in-memory panels every lab run shares: bars, USDCAD, and corporate data."""

    universe: dict[int, Series]
    fx: Series | None = None
    corporate: dict[int, CorporateData] | None = None
    label: str = CURATED
    coverage: Coverage | None = None


@dataclass
class LabSettings:
    """Which account(s) to simulate and under which costs/screen."""

    account: AccountType = AccountType.TFSA
    #: Re-run the first window in this account too, to show the tax drag (None = skip).
    compare_account: AccountType | None = AccountType.RRSP
    cost_model: RegisteredAccountCostModel | None = None
    base_config: RegisteredStrategyConfig = field(
        default_factory=lambda: RegisteredStrategyConfig(eligibility=LAB_ELIGIBILITY)
    )


def current_holdings(
    inputs: LabInputs, day: date, spec: StrategySpec, config: RegisteredStrategyConfig
) -> list[tuple[str, float]]:
    """What ``spec`` would buy if the account were opened on ``day``: (symbol, weight), largest
    first. A fresh allocator, so rank buffers start empty — this is the day-one book."""
    universe = inputs.universe
    candidates = _build_candidates(universe, inputs.fx, day, config)
    screened = screen(candidates, config.eligibility)
    features = _features_asof(universe, screened.eligible_ids, day, corporate=inputs.corporate)
    allocator = spec.build()
    allocator.asof(day)
    weights = allocator.allocate(screened.eligible, features)
    return sorted(
        ((universe[iid].symbol, weight) for iid, weight in weights.items()),
        key=lambda kv: (-kv[1], kv[0]),
    )


def run_lab(
    inputs: LabInputs,
    specs: list[StrategySpec],
    windows: list[Window],
    settings: LabSettings | None = None,
) -> LabResult:
    """Run every spec over every window for the account (plus the first window again for the
    compare account, to show the tax drag), and the day-one holdings."""
    if not inputs.universe:
        raise ValueError("empty universe")
    settings = settings or LabSettings()
    base, account, compare = settings.base_config, settings.account, settings.compare_account
    calendar = sorted({day for series in inputs.universe.values() for day in series.dates})
    result = LabResult(
        specs=specs,
        windows=windows,
        account=account.value,
        compare_account=compare.value if compare else None,
        asof=calendar[-1],
        universe_n=len(inputs.universe),
        universe_label=inputs.label,
        coverage=inputs.coverage,
    )
    jobs = [(spec, window, account) for window in windows for spec in specs]
    if compare is not None and windows:
        jobs += [(spec, windows[0], compare) for spec in specs]
    for spec, window, acct in jobs:
        config = replace(
            base,
            account=acct,
            eval_start=window.eval_start,
            rebalance_months=spec.rebalance_months,
            rebalance_band=spec.rebalance_band,
        )
        sim = simulate(
            inputs.universe,
            calendar,
            spec.build(),
            fx=inputs.fx,
            cost_model=settings.cost_model,
            config=config,
            strategy_name=spec.name,
            corporate=inputs.corporate,
        )
        result.runs.append(
            LabRun(spec.name, window.label, acct.value, sim.metrics, sim.equity_curve)
        )
    holdings_config = replace(base, account=account)
    for spec in specs:
        result.holdings[spec.name] = current_holdings(inputs, calendar[-1], spec, holdings_config)
    return result


def _bar_window(full_start: date, end: date) -> tuple[datetime, datetime]:
    """The bar window behind a lab run: ``WARMUP_DAYS`` before the first decision to ``end``."""
    start = datetime.combine(full_start - timedelta(days=WARMUP_DAYS), datetime.min.time(), UTC)
    return start, datetime.combine(end, datetime.max.time(), UTC)


def load_lab_inputs(session: Session, symbols: list[str], full_start: date, end: date) -> LabInputs:
    """A curated universe file's bars (with warm-up), USDCAD and corporate data, from Postgres."""
    start_dt, end_dt = _bar_window(full_start, end)
    universe = _load_universe(session, symbols, start_dt, end_dt)
    fx = _load_series(session, "USDCAD", start_dt, end_dt)
    corporate = {iid: load_corporate_inputs(session, iid) for iid in universe}
    return LabInputs(universe, fx, corporate)


def load_index_inputs(session: Session, full_start: date, end: date) -> LabInputs:
    """The point-in-time S&P 500 universe plus the broad ETFs the couch potato holds.

    Members are gated day by day by their index membership (``backtest.universe``); the ETFs
    are always candidates. Coverage — how much of the index the free sources could price —
    is measured over the decision window and travels with the inputs into the report.
    Canadian stocks are not included: no free point-in-time TSX source exists, so Canada is
    held through the ETFs.
    """
    window = _bar_window(full_start, end)
    universe, spans = load_index_universe(session, SP500, window, sorted(BROAD_ETF_SYMBOLS))
    fx = _load_series(session, "USDCAD", *window)
    corporate = {iid: load_corporate_inputs(session, iid) for iid in universe}
    measured = coverage(spans, universe, full_start, end)
    return LabInputs(universe, fx, corporate, label=POINT_IN_TIME, coverage=measured)
