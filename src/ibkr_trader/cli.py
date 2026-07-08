"""Typer CLI — entry point `ibkr-trader` (see [project.scripts] in pyproject.toml)."""

import sys

import typer

# Output uses "→"/"·", which legacy Windows consoles (cp1252) can't encode — degrade those
# characters to "?" there instead of crashing. UTF-8 terminals are unaffected.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

app = typer.Typer(help="ibkr-trader: ingestion, backtesting and (paper) trading via IBKR.")
ingest_app = typer.Typer(help="Run one ingestion connector.")
app.add_typer(ingest_app, name="ingest")
backtest_app = typer.Typer(help="Backtest strategies over stored data and compare runs.")
app.add_typer(backtest_app, name="backtest")
train_app = typer.Typer(help="Train the ML long-term model on stored data (needs the [ml] extra).")
app.add_typer(train_app, name="train")


def _read_universe(universe_file: str, symbols: str) -> list[str]:
    """Universe symbols from --symbols (comma-separated) or one-per-line --universe-file."""
    if symbols.strip():
        universe = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    else:
        try:
            with open(universe_file) as handle:
                universe = [line.strip().upper() for line in handle if line.strip()]
        except OSError as exc:
            raise typer.BadParameter(f"cannot read {universe_file!r}: {exc}") from None
    if not universe:
        raise typer.BadParameter("empty universe")
    return universe


@app.command()
def ibkr_check():
    """Connect to TWS/IB Gateway, print server time + account summary (read-only smoke test)."""
    from ibkr_trader.config import get_settings

    settings = get_settings()
    typer.echo(
        f"Connecting to {settings.ibkr_host}:{settings.ibkr_port} "
        f"(clientId={settings.ibkr_client_id}, env={settings.environment})"
    )
    # TODO(skeleton): IB().connect(...); echo ib.reqCurrentTime(), managed accounts,
    # and one delayed quote (reqMarketDataType(3)).
    raise typer.Exit(code=1)


@ingest_app.command("news")
def ingest_news(query: str = typer.Option("", help="search query, e.g. a company name")):
    from ibkr_trader.ingestion.news.newsapi import NewsApiConnector

    count = NewsApiConnector().fetch(query=query)
    typer.echo(f"upserted {count} articles")


@ingest_app.command("reddit")
def ingest_reddit(limit: int = 100):
    from ibkr_trader.ingestion.social.reddit import RedditConnector

    count = RedditConnector().fetch(limit=limit)
    typer.echo(f"upserted {count} posts")


@ingest_app.command("trends")
def ingest_trends(keywords: list[str] = typer.Option([], help="up to 5 keywords")):
    from ibkr_trader.ingestion.social.google_trends import GoogleTrendsConnector

    count = GoogleTrendsConnector().fetch(keywords=keywords)
    typer.echo(f"upserted {count} trend points")


@ingest_app.command("prices")
def ingest_prices(
    symbol: str, source: str = typer.Option("fmp", help="fmp|yahoo|alpha_vantage|ibkr")
):
    connectors = {
        "fmp": "ibkr_trader.ingestion.market.fmp:FmpConnector",
        "yahoo": "ibkr_trader.ingestion.market.yahoo:YahooConnector",
        "alpha_vantage": "ibkr_trader.ingestion.market.alpha_vantage:AlphaVantageConnector",
        "ibkr": "ibkr_trader.ingestion.market.ibkr_historical:IbkrHistoricalConnector",
    }
    if source not in connectors:
        raise typer.BadParameter(f"unknown source {source!r}")
    module_path, class_name = connectors[source].split(":")
    module = __import__(module_path, fromlist=[class_name])
    try:
        count = getattr(module, class_name)().fetch(symbol=symbol)
    except RuntimeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"upserted {count} bars")


@ingest_app.command("fundamentals")
def ingest_fundamentals(symbol: str):
    """Upsert Yahoo corporate data (dividends, share counts, statements, sector, earnings dates)
    for one symbol (e.g. AAPL, RY.TO). ETFs ingest dividends only, gracefully."""
    from ibkr_trader.ingestion.market.yahoo_fundamentals import YahooFundamentalsConnector

    try:
        count = YahooFundamentalsConnector().fetch(symbol=symbol)
    except RuntimeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"upserted {count} rows")


@ingest_app.command("fx")
def ingest_fx(
    pair: str = typer.Option("USDCAD", help="currency pair, e.g. USDCAD (close = CAD per 1 USD)"),
    date_from: str = typer.Option("", help="YYYY-MM-DD"),
    date_to: str = typer.Option("", help="YYYY-MM-DD"),
):
    """Ingest daily FX rates (the simulator converts US holdings to CAD via these)."""
    from ibkr_trader.ingestion.market.fmp_fx import FmpFxConnector

    try:
        count = FmpFxConnector().fetch(pair=pair, date_from=date_from, date_to=date_to)
    except RuntimeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"upserted {count} FX bars for {pair.upper()}")


@backtest_app.command("run")
def backtest_run(
    strategy: str = typer.Option("momentum_lt", help="registered allocator name"),
    universe_file: str = typer.Option("tickers.txt", help="one symbol per line"),
    symbols: str = typer.Option("", help="comma-separated symbols (overrides --universe-file)"),
    account: str = typer.Option("", help="rrsp|tfsa|fhsa|lira|nonreg (default: config)"),
    start: str = typer.Option("2015-01-01", help="window start YYYY-MM-DD"),
    end: str = typer.Option("2025-01-01", help="window end YYYY-MM-DD"),
    start_capital: float = typer.Option(100_000.0, help="starting capital (CAD)"),
    no_persist: bool = typer.Option(False, "--no-persist", help="don't write a backtest_runs row"),
):
    """Simulate a long-term registered-account strategy over stored bars and report the P&L."""
    from datetime import UTC, datetime

    from ibkr_trader.accounts import AccountType
    from ibkr_trader.backtest.costs import RegisteredAccountCostModel
    from ibkr_trader.backtest.engine import BacktestEngine, RegisteredStrategyConfig
    from ibkr_trader.config import get_settings
    from ibkr_trader.signals.portfolio import get_allocator

    try:
        # fail fast: unknown name lists the known allocators; ml_lt additionally needs the
        # [ml] extra (RuntimeError) and a trained artifact (FileNotFoundError)
        get_allocator(strategy)
    except (KeyError, RuntimeError, FileNotFoundError) as exc:
        raise typer.BadParameter(str(exc)) from None

    settings = get_settings()
    account_type = AccountType((account or settings.default_account).lower())

    universe = _read_universe(universe_file, symbols)

    start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=UTC)
    end_dt = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=UTC)
    config = RegisteredStrategyConfig(
        account=account_type,
        start_capital=start_capital,
        annual_trade_budget=settings.annual_trade_budget,
        rebalance_band=settings.rebalance_band,
        benchmark_symbol=settings.benchmark_symbol,
    )
    cost_model = RegisteredAccountCostModel(
        churn_penalty_bps=settings.churn_penalty_bps,
        fx_conversion_bps=settings.fx_conversion_bps,
        assumed_us_dividend_yield=settings.us_dividend_yield_assumption,
    )

    try:
        result = BacktestEngine(cost_model=cost_model).run(
            strategy, universe, start_dt, end_dt, config=config, persist=not no_persist
        )
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    _print_backtest_result(result, account_type.value)


def _print_backtest_result(result, account: str) -> None:
    m = result.metrics
    window = f"{result.start:%Y-%m-%d}→{result.end:%Y-%m-%d}"
    typer.echo(f"\n{result.strategy}  ·  account={account}  ·  {window}")
    start_v = m.get("start_value_cad", 0.0)
    end_v = m.get("end_value_cad", 0.0)
    bench_v = m.get("benchmark_end_value_cad")
    typer.echo(
        f"  If you'd followed this model: ${start_v:,.0f} → ${end_v:,.0f} CAD "
        f"({m.get('total_return', 0) * 100:+.1f}%)"
    )
    if bench_v is not None:
        typer.echo(
            f"  Buy-and-hold {result.params.get('benchmark', '?')}: ${bench_v:,.0f} CAD "
            f"({m.get('benchmark_total_return', 0) * 100:+.1f}%)  ·  "
            f"excess {m.get('excess_return', 0) * 100:+.1f}%"
        )
    typer.echo(
        f"  CAGR {m.get('cagr', 0) * 100:.1f}%  ·  Sharpe {m.get('sharpe', 0):.2f}  ·  "
        f"Sortino {m.get('sortino', 0):.2f}  ·  max DD {m.get('max_drawdown', 0) * 100:.1f}%"
    )
    typer.echo(
        f"  {result.trades} trades ({m.get('trades_per_year', 0):.1f}/yr)  ·  "
        f"costs ${result.costs_cad:,.0f}  ·  FX conversion ${result.fx_cost_cad:,.0f}  ·  "
        f"US-dividend tax ${result.tax_cad:,.0f} CAD"
    )


@backtest_app.command("compare")
def backtest_compare(
    strategy: str | None = typer.Option(None, help="filter to one strategy (default: all)"),
    sort_by: str = typer.Option("sharpe", help="metric key from metrics JSON to rank by"),
    ascending: bool = typer.Option(False, "--ascending", help="worst-first (e.g. max_drawdown)"),
    limit: int = typer.Option(0, help="max rows to show (0 = all)"),
):
    """Rank persisted backtest_runs by a metric — the model leaderboard."""
    from ibkr_trader.backtest.compare import compare_runs
    from ibkr_trader.db.session import get_session

    with get_session() as session:
        runs = compare_runs(
            session,
            strategy=strategy,
            sort_by=sort_by,
            descending=not ascending,
            limit=limit or None,
        )
    if not runs:
        typer.echo("no backtest runs found")
        return

    def cell(value: object, places: int = 3) -> str:
        return f"{value:.{places}f}" if isinstance(value, int | float) else "-"

    header = (
        f"{'#':>2}  {'strategy':<22} {'ver':<5} {'horizon':<8} {'window':<23}"
        f" {'sharpe':>8} {'max_dd':>8} {'cagr':>8} {'days':>6}"
    )
    typer.echo(f"ranked by {sort_by} ({'asc' if ascending else 'desc'}), {len(runs)} run(s)")
    typer.echo(header)
    typer.echo("-" * len(header))
    for rank, run in enumerate(runs, start=1):
        params = run.params or {}
        metrics = run.metrics or {}
        window = f"{run.start:%Y-%m-%d}→{run.end:%Y-%m-%d}"
        typer.echo(
            f"{rank:>2}  {run.strategy[:22]:<22}"
            f" {str(params.get('model_version', '-')):<5}"
            f" {str(params.get('horizon', '-')):<8} {window:<23}"
            f" {cell(metrics.get('sharpe')):>8} {cell(metrics.get('max_drawdown')):>8}"
            f" {cell(metrics.get('cagr')):>8} {cell(metrics.get('n_days'), 0):>6}"
        )


@train_app.command("run")
def train_run(
    start: str = typer.Option("2015-01-01", help="training window start YYYY-MM-DD"),
    end: str = typer.Option(..., help="training window end YYYY-MM-DD (labels stop 12m earlier)"),
    universe_file: str = typer.Option("tickers.txt", help="one symbol per line"),
    symbols: str = typer.Option("", help="comma-separated symbols (overrides --universe-file)"),
    models_dir: str = typer.Option("models", help="artifact root (models/ml_lt/<version>/)"),
    seed: int = typer.Option(42, help="random seed for LightGBM/ridge"),
    test_size: int = typer.Option(6, help="months per walk-forward test block"),
    min_train: int = typer.Option(24, help="months of history before the first test block"),
):
    """Build the supervised dataset, walk-forward validate (LightGBM + linear floor), fit the
    final model and write a versioned artifact under models/ml_lt/."""
    from datetime import UTC, datetime
    from pathlib import Path

    from ibkr_trader.db.session import get_session
    from ibkr_trader.signals.train import train_from_db

    start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=UTC)
    end_dt = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=UTC)
    try:
        with get_session() as session:
            result = train_from_db(
                session,
                _read_universe(universe_file, symbols),
                start_dt,
                end_dt,
                models_dir=Path(models_dir),
                seed=seed,
                test_size=test_size,
                min_train=min_train,
            )
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    _print_train_summary(result.metadata)
    typer.echo(f"\nartifact: {result.artifact_dir}")


@train_app.command("report")
def train_report(
    models_dir: str = typer.Option("models", help="artifact root (models/ml_lt/<version>/)"),
):
    """Print the latest trained artifact's metadata and walk-forward ICs."""
    from pathlib import Path

    from ibkr_trader.signals.train import load_latest_metadata

    try:
        metadata = load_latest_metadata(Path(models_dir))
    except FileNotFoundError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    _print_train_summary(metadata)


def _print_train_summary(metadata: dict) -> None:
    window = metadata.get("train_window", {})
    dates = window.get("dataset_dates", ["?", "?"])
    typer.echo(
        f"\n{metadata.get('model')} {metadata.get('version')}  ·  "
        f"feature set v{metadata.get('feature_set_version')}  ·  "
        f"created {metadata.get('created_at', '?')[:19]}"
    )
    typer.echo(f"  label: {metadata.get('label', {}).get('spec', '?')}")
    universe = metadata.get("universe", {})
    typer.echo(
        f"  dataset: {window.get('n_rows', '?')} rows · {window.get('n_dates', '?')} rebalance "
        f"dates ({dates[0]}→{dates[1]}) · {universe.get('n_symbols', '?')} symbols "
        f"(hash {universe.get('sha256_16', '?')})"
    )

    validation = metadata.get("validation", {})
    folds = validation.get("folds", [])
    model_names = sorted(validation.get("overall_ic", {}))

    def ic_cell(ic: dict | None) -> str:
        if not ic or ic.get("mean") is None:
            return f"{'-':>16}"
        return f"{ic['mean']:+.3f} ±{ic['std']:.3f} ({ic['n_dates']:>2})"

    header = f"  {'fold':<4} {'train':<23} {'test':<23}" + "".join(
        f" {name:>18}" for name in model_names
    )
    typer.echo(
        f"  walk-forward ({validation.get('scheme', '?')}, "
        f"purge {validation.get('purge_months', '?')}m):"
    )
    typer.echo(header)
    for fold in folds:
        train_w = "→".join(fold.get("train", ["?", "?"]))
        test_w = "→".join(fold.get("test", ["?", "?"]))
        cells = "".join(f" {ic_cell(fold.get('ic', {}).get(name)):>18}" for name in model_names)
        typer.echo(f"  {fold.get('fold_id', '?'):<4} {train_w:<23} {test_w:<23}{cells}")
    for name in model_names:
        overall = validation.get("overall_ic", {}).get(name) or {}
        typer.echo(f"  overall {name}: {ic_cell(overall).strip()} mean ±std across test dates")
    typer.echo(f"  {metadata.get('note', '')}")


@app.command()
def serve():
    """Long-running mode: APScheduler jobs for periodic ingestion (+ later, trading loop)."""
    # TODO(skeleton): BlockingScheduler with cron jobs per connector, honoring each
    # source's rate limits. Trading loop stays out until
    # backtests + paper validation exist.
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
