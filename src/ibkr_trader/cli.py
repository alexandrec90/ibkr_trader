"""Typer CLI — entry point `ibkr-trader` (see [project.scripts] in pyproject.toml)."""

import csv
import sys
from enum import StrEnum
from pathlib import Path
from typing import cast

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
snapshot_app = typer.Typer(help="Record and score broker-free forward strategy snapshots.")
app.add_typer(snapshot_app, name="snapshot")
archive_app = typer.Typer(
    help="Offload cold data (intraday bars, scored raw payloads) to object storage as Parquet, "
    "and restore it (docs/operations/remote-archive.md; needs the [archive] extra + "
    "ARCHIVE_* in .env)."
)
app.add_typer(archive_app, name="archive")
sentiment_app = typer.Typer(help="Manage local sentiment models and re-score stored text.")
app.add_typer(sentiment_app, name="sentiment")


@app.callback()
def _configure() -> None:
    """Runs before every subcommand: hand the shared data-lake package our config + engine.

    The connectors and the archive live in the ``data_lake`` package now and own neither, so
    without this any ``ingest``/``archive`` command would raise rather than reach for a
    database (docs/plans/active/data-lake.md, Phase 2).
    """
    from ibkr_trader.lake import configure_lake

    configure_lake()


class TrainSearch(StrEnum):
    """Supported LightGBM capacity selectors exposed by ``train run``."""

    optuna = "optuna"
    grid = "grid"


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


@app.command("check-data")
def check_data(
    symbols: str = typer.Option("", help="comma-separated symbols to validate"),
    universe_file: str = typer.Option("", help="symbols to validate, one per line"),
    days: int = typer.Option(365, min=1, help="recent calendar days to inspect"),
):
    """Validate recent downstream price inputs without repairing or deleting bad rows."""
    from datetime import UTC, datetime, timedelta

    from pandera.errors import SchemaErrors

    from ibkr_trader.backtest.engine import _load_series
    from ibkr_trader.db.session import get_session
    from ibkr_trader.signals.schemas import format_schema_errors

    if bool(symbols.strip()) == bool(universe_file.strip()):
        raise typer.BadParameter("provide exactly one of --symbols or --universe-file")
    universe = _read_universe(universe_file, symbols)
    now = datetime.now(UTC)
    start = now - timedelta(days=days)
    # Include provider-glitch rows dated after now so ts_not_future can report them.
    far_future = datetime.max.replace(tzinfo=UTC)
    checked_rows = 0
    failures = 0

    with get_session() as session:
        for symbol in universe:
            try:
                series = _load_series(session, symbol, start, far_future, validate=True)
            except SchemaErrors as exc:
                failures += 1
                typer.echo(f"{symbol}: data-quality violations", err=True)
                for line in format_schema_errors(exc):
                    typer.echo(f"  {line}", err=True)
                continue
            if series is None:
                typer.echo(f"{symbol}: no recent daily bars")
                continue
            checked_rows += len(series.dates)
            typer.echo(f"{symbol}: ok ({len(series.dates)} rows)")

    if failures:
        typer.echo(f"FAILED: {failures} instrument(s) violated the price schema", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"OK: {checked_rows} recent price rows passed")


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
def ingest_news(
    query: str = typer.Option("", help="search query, e.g. a company name"),
    symbol: str = typer.Option("", help="optional ticker to tag the stored articles with"),
    date_from: str = typer.Option("", help="YYYY-MM-DD (free tier only reaches ~1 month back)"),
    date_to: str = typer.Option("", help="YYYY-MM-DD"),
    mapping_file: str = typer.Option(
        "", help="batch mode: 'TICKER,search query' file (see news-keywords.txt)"
    ),
    refresh_after_hours: float = typer.Option(
        12.0,
        help="batch mode: skip symbols whose newsapi articles were fetched within this "
        "(free-tier articles are 24 h delayed → nothing new inside ~12 h; 0 = force)",
    ),
    max_requests: int = typer.Option(
        90, help="batch mode: request budget per run (free tier is 100/day)"
    ),
):
    """Upsert NewsAPI headlines. Ad-hoc: --query (+ --symbol tag). Batch: --mapping-file runs
    one request per stale symbol, skips freshly-fetched ones, and stops on the request budget
    or a key/quota failure — so re-runs are cheap and a dead key can't burn the day's quota.
    Free tier: ~100 req/day, articles delayed 24 h, first 100 results per search."""
    if mapping_file:
        from data_lake.ingestion.social.google_trends import read_mapping_file

        from ibkr_trader.scheduler import poll_newsapi_pairs

        try:
            pairs = read_mapping_file(mapping_file)
        except (OSError, ValueError) as exc:
            raise typer.BadParameter(str(exc)) from None
        count = poll_newsapi_pairs(
            pairs, refresh_after_hours=refresh_after_hours, max_requests=max_requests
        )
        typer.echo(f"upserted {count} articles")
        return

    from data_lake.ingestion.news.newsapi import NewsApiConnector

    try:
        count = NewsApiConnector().fetch(
            query=query, symbol=symbol, date_from=date_from, date_to=date_to
        )
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"upserted {count} articles")


@ingest_app.command("finnhub-news")
def ingest_finnhub_news(
    symbol: str = typer.Argument("", help="ticker (e.g. AAPL); omit and pass --universe-file"),
    date_from: str = typer.Option("", help="YYYY-MM-DD (default: last 7 days)"),
    date_to: str = typer.Option("", help="YYYY-MM-DD (default: today)"),
    universe_file: str = typer.Option(
        "", help="batch mode: pull news for every symbol in this file (one per line)"
    ),
    spacing_seconds: float = typer.Option(
        1.1, help="batch mode: delay between calls (free tier is 60/min)"
    ),
):
    """Upsert Finnhub company news. Pass a SYMBOL for one ticker, or --universe-file to batch a
    whole list (last-7-days window, spaced to respect the rate limit). Articles arrive tagged by
    symbol. Note: Finnhub's free company-news coverage is US-centric — TSX names may return
    nothing."""
    if universe_file:
        # Reuse the scheduler's batch helper: it spaces calls and skips a failing symbol.
        # Re-runs cost the same 1 call/symbol regardless of window; pass --date-from ~1 year
        # back for the initial backfill.
        from ibkr_trader.scheduler import poll_finnhub_news

        count = poll_finnhub_news(
            universe_file, spacing_seconds, date_from=date_from, date_to=date_to
        )
        typer.echo(f"upserted {count} articles")
        return

    if not symbol.strip():
        raise typer.BadParameter("provide a SYMBOL or --universe-file")

    from data_lake.ingestion.news.finnhub_news import FinnhubNewsConnector

    try:
        count = FinnhubNewsConnector().fetch(symbol=symbol, date_from=date_from, date_to=date_to)
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"upserted {count} articles")


@ingest_app.command("finnhub-backfill")
def ingest_finnhub_backfill(
    universe_file: str = typer.Option(
        "tickers.txt", help="symbols to backfill, one per line (same file the serve poll uses)"
    ),
    days: int = typer.Option(365, help="rolling floor depth (Finnhub free tier serves ~1 year)"),
    chunk_days: int = typer.Option(30, help="window size; capped-looking windows split in half"),
    max_requests: int = typer.Option(
        2000, help="per-run API budget; the remainder resumes on the next run"
    ),
    spacing_seconds: float = typer.Option(1.1, help="delay between calls (free tier is 60/min)"),
):
    """Walk Finnhub company-news history backwards to the rolling floor for a whole universe.

    Resumable and idempotent: the cursor is each symbol's oldest stored article, so re-running
    (or the `serve` job, which runs this daily) only fetches what is still missing. The first
    full run takes a while — ~12 spaced calls per symbol, more where windows split.
    """
    from ibkr_trader.scheduler import backfill_finnhub_news

    try:
        count = backfill_finnhub_news(
            universe_file,
            backfill_days=days,
            chunk_days=chunk_days,
            max_requests=max_requests,
            request_spacing_seconds=spacing_seconds,
        )
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"upserted {count} articles")


@ingest_app.command("reddit")
def ingest_reddit(limit: int = 100):
    from data_lake.ingestion.social.reddit import RedditConnector

    try:
        count = RedditConnector().fetch(limit=limit)
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"upserted {count} posts")


@ingest_app.command("trends")
def ingest_trends(
    keywords: list[str] = typer.Option([], help="ad-hoc mode: up to 5 search terms"),
    mapping_file: str = typer.Option(
        "", help="batch mode: 'TICKER,search term' file (see trends-keywords.txt)"
    ),
    geo: str = typer.Option("", help='Trends region code; "" = worldwide, "CA" = Canada'),
    timeframe: str = typer.Option(
        "", help="pytrends timeframe (defaults: batch 'today 5-y', ad-hoc 'now 7-d')"
    ),
    refresh_after_days: float = typer.Option(
        14.0,
        help="batch mode: skip keywords whose stored series is younger than this "
        "(weekly buckets → nothing new inside 14 days; 0 = force full re-fetch)",
    ),
):
    """Upsert Google Trends interest. --mapping-file runs one request per keyword (~1 min each,
    own 0-100 scale per series) and defaults to a 5-year weekly window, so the first batch run
    doubles as backfill. Fresh keywords are skipped (see --refresh-after-days), so re-runs cost
    seconds and a partially-failed batch resumes from the failures."""
    if mapping_file:
        from data_lake.ingestion.social.google_trends import read_mapping_file

        from ibkr_trader.scheduler import poll_trends_pairs

        try:
            pairs = read_mapping_file(mapping_file)
        except (OSError, ValueError) as exc:
            raise typer.BadParameter(str(exc)) from None
        typer.echo(f"polling {len(pairs)} keywords (~1 min per stale keyword, fresh skipped) ...")
        count = poll_trends_pairs(
            pairs,
            geo=geo,
            timeframe=timeframe or "today 5-y",
            refresh_after_days=refresh_after_days,
        )
        typer.echo(f"upserted {count} trend points")
        return

    from data_lake.ingestion.social.google_trends import DEFAULT_TIMEFRAME, GoogleTrendsConnector

    try:
        count = GoogleTrendsConnector().fetch(
            keywords=keywords, geo=geo, timeframe=timeframe or DEFAULT_TIMEFRAME
        )
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"upserted {count} trend points")


@ingest_app.command("prices")
def ingest_prices(
    symbol: str = typer.Argument("", help="ticker; omit and pass --universe-file for batch"),
    source: str = typer.Option("fmp", help="fmp|yahoo|alpha_vantage|ibkr"),
    universe_file: str = typer.Option(
        "", help="batch mode (alpha_vantage only): one symbol per line (see tickers-av.txt)"
    ),
    refresh_after_days: float = typer.Option(
        1.0,
        help="batch mode: skip symbols whose newest stored bar is younger than this "
        "(1 = same-day reruns are free, 3 = also skip weekend runs, 0 = force)",
    ),
    max_requests: int = typer.Option(
        20, help="batch mode: fetch budget per run (Alpha Vantage free tier is ~25/day)"
    ),
):
    """Upsert daily bars for one SYMBOL, or --universe-file to batch a list through Alpha
    Vantage under its tiny free budget (fresh symbols are skipped, quota errors abort)."""
    if universe_file:
        if source != "alpha_vantage":
            raise typer.BadParameter(
                "--universe-file batching is alpha_vantage-only "
                "(FMP/Yahoo have their own batch tasks)"
            )
        from data_lake.ingestion.market.alpha_vantage import fetch_universe, read_tickers_file

        try:
            symbols = read_tickers_file(universe_file)
        except OSError as exc:
            raise typer.BadParameter(f"cannot read {universe_file!r}: {exc}") from None
        count = fetch_universe(
            symbols, refresh_after_days=refresh_after_days, max_requests=max_requests
        )
        typer.echo(f"upserted {count} bars")
        return

    if not symbol.strip():
        raise typer.BadParameter("provide a SYMBOL or --universe-file")

    connectors = {
        "fmp": "data_lake.ingestion.market.fmp:FmpConnector",
        "yahoo": "data_lake.ingestion.market.yahoo:YahooConnector",
        "alpha_vantage": "data_lake.ingestion.market.alpha_vantage:AlphaVantageConnector",
        "ibkr": "data_lake.ingestion.market.ibkr_historical:IbkrHistoricalConnector",
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
    from data_lake.ingestion.market.yahoo_fundamentals import YahooFundamentalsConnector

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
    source: str = typer.Option(
        "fmp", help="fmp (official-ish, ~5y free history) | yahoo (deep history, ~2003+)"
    ),
):
    """Ingest daily FX rates (the simulator converts US holdings to CAD via these)."""
    from data_lake.ingestion.base import Connector

    connector: Connector
    if source == "fmp":
        from data_lake.ingestion.market.fmp_fx import FmpFxConnector

        connector = FmpFxConnector()
    elif source == "yahoo":
        from data_lake.ingestion.market.yahoo_fx import YahooFxConnector

        connector = YahooFxConnector()
    else:
        raise typer.BadParameter(f"unknown source {source!r} (fmp|yahoo)")

    try:
        count = connector.fetch(pair=pair, date_from=date_from, date_to=date_to)
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
    eval_start: str = typer.Option(
        "",
        help="first decision date YYYY-MM-DD; bars from --start onward warm up features "
        "(e.g. --start 2008-06-01 --eval-start 2010-01-01 gives 2010 decisions a full "
        "momentum/history lookback instead of a year of forced cash)",
    ),
    start_capital: float = typer.Option(100_000.0, help="starting capital (CAD)"),
    min_history_days: int = typer.Option(
        252, min=1, help="minimum as-of listing history in trading days"
    ),
    no_persist: bool = typer.Option(False, "--no-persist", help="don't write a backtest_runs row"),
):
    """Simulate a long-term registered-account strategy over stored bars and report the P&L."""
    from datetime import UTC, datetime

    from ibkr_trader.accounts import AccountType
    from ibkr_trader.backtest.costs import RegisteredAccountCostModel
    from ibkr_trader.backtest.engine import BacktestEngine, RegisteredStrategyConfig
    from ibkr_trader.config import get_settings
    from ibkr_trader.signals.eligibility import EligibilityLimits
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
    try:
        eval_start_date = datetime.strptime(eval_start, "%Y-%m-%d").date() if eval_start else None
    except ValueError:
        raise typer.BadParameter("--eval-start must be YYYY-MM-DD") from None
    if eval_start_date is not None and eval_start_date <= start_dt.date():
        raise typer.BadParameter("--eval-start must be after --start (it needs warm-up bars)")
    config = RegisteredStrategyConfig(
        account=account_type,
        start_capital=start_capital,
        annual_trade_budget=settings.annual_trade_budget,
        rebalance_band=settings.rebalance_band,
        benchmark_symbol=settings.benchmark_symbol,
        eligibility=EligibilityLimits(min_history_days=min_history_days),
        eval_start=eval_start_date,
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
    universe = result.params.get("universe", {})
    if universe.get("survivorship") == "curated-current":
        typer.echo(
            "  WARNING: curated-current universe is survivorship-biased; absolute returns "
            "are upper bounds."
        )


@snapshot_app.command("run")
def snapshot_run(
    strategy: str | None = typer.Option(None, help="one allocator (default: all that resolve)"),
    all_strategies: bool = typer.Option(False, "--all", help="snapshot all resolvable allocators"),
    asof: str = typer.Option("", help="YYYY-MM-DD; only today/one-day retry is allowed"),
):
    """Record target weights now; historical backfills are deliberately refused."""
    from datetime import datetime

    from ibkr_trader.backtest.snapshot import STALE_BAR_DAYS, run_snapshots
    from ibkr_trader.db.session import get_session

    if strategy and all_strategies:
        raise typer.BadParameter("use either --strategy or --all, not both")
    try:
        day = datetime.strptime(asof, "%Y-%m-%d").date() if asof else None
    except ValueError:
        raise typer.BadParameter("--asof must be YYYY-MM-DD") from None
    try:
        with get_session() as session:
            result = run_snapshots(session, strategies=[strategy] if strategy else None, asof=day)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if result.stale_days > STALE_BAR_DAYS:
        typer.echo(
            f"WARNING: latest daily bar is {result.latest_bar_date} "
            f"({result.stale_days} days stale); run ingestion first",
            err=True,
        )
    for name, reason in result.skipped.items():
        typer.echo(f"warning: skipped {name}: {reason}", err=True)
    typer.echo(
        f"saved {len(result.snapshots)} forward snapshot(s) for {result.snapshots[0].ts:%Y-%m-%d}"
        if result.snapshots
        else "no strategies resolved"
    )


@snapshot_app.command("report")
def snapshot_report(
    horizon_months: int = typer.Option(1, min=1, help="realization horizon in calendar months"),
):
    """Report mature forward returns after approximate turnover costs, versus XEQT."""
    from ibkr_trader.backtest.snapshot import build_snapshot_report
    from ibkr_trader.db.session import get_session

    try:
        with get_session() as session:
            rows = build_snapshot_report(session, horizon_months=horizon_months)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if not rows:
        typer.echo(f"no snapshots have matured for the {horizon_months}-month horizon")
        return
    typer.echo(f"forward shadow · {horizon_months}-month realized CAD return vs XEQT, after costs")
    for row in rows:
        typer.echo(
            f"  {row.strategy}: mean excess {row.mean_excess * 100:+.1f}% · "
            f"hit rate {row.hit_rate * 100:.0f}% · {row.n_snapshots} snapshot(s)"
        )


@backtest_app.command("oos")
def backtest_oos(
    end: str = typer.Option(..., help="dataset window end YYYY-MM-DD (labels stop 12m earlier)"),
    start: str = typer.Option("2015-01-01", help="dataset window start YYYY-MM-DD"),
    sim_start: str = typer.Option(
        "2021-08-01",
        help="simulation bar-load start YYYY-MM-DD (USDCAD coverage starts 2003-09-17; "
        "owner policy floor for decisions is 2010)",
    ),
    universe_file: str = typer.Option("tickers.txt", help="one symbol per line"),
    symbols: str = typer.Option("", help="comma-separated symbols (overrides --universe-file)"),
    account: str = typer.Option("", help="rrsp|tfsa|fhsa|lira|nonreg (default: config)"),
    start_capital: float = typer.Option(100_000.0, help="starting capital (CAD)"),
    seed: int = typer.Option(42, help="random seed for LightGBM/ridge"),
    test_size: int = typer.Option(6, help="months per walk-forward test block"),
    min_train: int = typer.Option(24, help="months of history before the first test block"),
    min_history_days: int = typer.Option(
        252, min=1, help="minimum as-of listing history in trading days"
    ),
    no_persist: bool = typer.Option(False, "--no-persist", help="don't write backtest_runs rows"),
):
    """Per-fold out-of-sample backtest — the honest number. Trains one model per walk-forward
    fold in memory (never the deployed artifact) so every decision comes from a model that
    never saw its own test months, then runs the baselines over the identical decision dates."""
    from datetime import UTC, datetime

    from ibkr_trader.accounts import AccountType
    from ibkr_trader.backtest.costs import RegisteredAccountCostModel
    from ibkr_trader.backtest.engine import RegisteredStrategyConfig
    from ibkr_trader.backtest.oos import run_oos_backtest
    from ibkr_trader.config import get_settings
    from ibkr_trader.db.session import get_session
    from ibkr_trader.signals.eligibility import EligibilityLimits

    settings = get_settings()
    account_type = AccountType((account or settings.default_account).lower())
    universe = _read_universe(universe_file, symbols)

    start_dt = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=UTC)
    end_dt = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=UTC)
    sim_start_dt = datetime.strptime(sim_start, "%Y-%m-%d").replace(tzinfo=UTC)
    config = RegisteredStrategyConfig(
        account=account_type,
        start_capital=start_capital,
        annual_trade_budget=settings.annual_trade_budget,
        rebalance_band=settings.rebalance_band,
        benchmark_symbol=settings.benchmark_symbol,
        eligibility=EligibilityLimits(min_history_days=min_history_days),
    )
    cost_model = RegisteredAccountCostModel(
        churn_penalty_bps=settings.churn_penalty_bps,
        fx_conversion_bps=settings.fx_conversion_bps,
        assumed_us_dividend_yield=settings.us_dividend_yield_assumption,
    )

    try:
        with get_session() as session:
            oos = run_oos_backtest(
                session,
                universe,
                start_dt,
                end_dt,
                sim_start=sim_start_dt,
                config=config,
                cost_model=cost_model,
                seed=seed,
                test_size=test_size,
                min_train=min_train,
                persist=not no_persist,
            )
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    typer.echo(
        f"\nper-fold OOS backtest · {oos.n_folds} folds · decisions "
        f"{oos.eval_start} → {oos.test_end} · every decision from a model that never saw "
        f"its own test months"
    )
    for result in oos.results:
        _print_backtest_result(result, account_type.value)


@backtest_app.command("factor-report")
def backtest_factor_report(
    run_id: int | None = typer.Option(
        None, help="persisted ml_lt_oos/ml_lt_ridge_oos run (default: latest with predictions)"
    ),
    output_dir: str = typer.Option(
        "", help="optional local directory for an HTML summary and PNG tear sheet"
    ),
    max_loss: float = typer.Option(
        0.35, min=0.0, max=1.0, help="maximum factor rows Alphalens may drop"
    ),
):
    """Diagnose persisted fold-OOS scores with IC decay, quantiles, and turnover."""
    from pathlib import Path

    from ibkr_trader.backtest.factor_report import generate_factor_report
    from ibkr_trader.db.session import get_session

    try:
        with get_session() as session:
            report = generate_factor_report(
                session,
                run_id,
                max_loss=max_loss,
                output_dir=Path(output_dir) if output_dir else None,
            )
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    metadata = report["metadata"]
    dropped = report["diagnostics"]
    typer.echo(
        f"\nOOS factor report · run {metadata['run_id']} · {metadata['strategy']} · "
        f"{dropped['clean_rows']}/{dropped['input_rows']} rows retained"
    )
    typer.echo(
        "  returns: native-currency close-to-close research diagnostic; portfolio promotion "
        "still uses the CAD/total-return after-cost engine"
    )
    for title, key in (
        ("mean rank IC and decay", "mean_ic"),
        ("mean return by factor quantile", "quantile_returns"),
        ("quantile shape", "quantile_diagnostics"),
        ("quantile turnover (monthly factor observations)", "turnover"),
    ):
        typer.echo(f"\n{title}")
        typer.echo(report[key].to_string(float_format=lambda value: f"{value:+.4f}"))
    for artifact in report["artifacts"]:
        typer.echo(f"\nsaved: {artifact}")


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
    typer.echo(
        "\nNOTE: curated-current universes are survivorship-biased; absolute excess returns "
        "are upper bounds. Compare strategies only on the identical universe."
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
    min_history_days: int = typer.Option(
        252, min=1, help="minimum as-of listing history in trading days"
    ),
    search: TrainSearch = typer.Option(
        TrainSearch.optuna, help="capacity search strategy (optuna is deterministic TPE)"
    ),
    n_trials: int = typer.Option(50, min=1, help="Optuna trial budget (ignored by grid)"),
    track_mlflow: bool = typer.Option(
        False,
        "--track-mlflow",
        help="project metadata into a local MLflow file store (needs [tracking])",
    ),
    mlflow_dir: str = typer.Option("mlruns", help="local MLflow store used with --track-mlflow"),
):
    """Build the supervised dataset, walk-forward validate (LightGBM + linear floor), fit the
    final model and write a versioned artifact under models/ml_lt/."""
    from datetime import UTC, datetime
    from pathlib import Path

    from ibkr_trader.db.session import get_session
    from ibkr_trader.signals.eligibility import EligibilityLimits
    from ibkr_trader.signals.tracking import log_training_run
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
                limits=EligibilityLimits(min_history_days=min_history_days),
                search=search.value,
                n_trials=n_trials,
            )
        tracking_run = (
            log_training_run(result.artifact_dir, Path(mlflow_dir)) if track_mlflow else None
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None

    _print_train_summary(result.metadata)
    typer.echo(f"\nartifact: {result.artifact_dir}")
    if tracking_run is not None:
        typer.echo(f"MLflow: run {tracking_run.run_id} · local store {tracking_run.tracking_uri}")


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
    search = metadata.get("lgbm_search") or {}
    if search:
        detail = f" · {search.get('duration_seconds', 0.0):.1f}s"
        if search.get("method") == "optuna":
            detail += (
                f" · {search.get('completed_count', '?')} completed / "
                f"{search.get('pruned_count', '?')} pruned"
            )
        typer.echo(f"  parameter search: {search.get('method', '?')}{detail}")
    typer.echo(f"  {metadata.get('note', '')}")


def _archive_store():
    """The configured backend, with config errors surfaced as clean CLI failures."""
    from data_lake.archive import store_from_settings

    try:
        return store_from_settings()
    except RuntimeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None


def _parse_date(value: str, option: str):
    from datetime import datetime

    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise typer.BadParameter(f"{option} must be YYYY-MM-DD") from None


def _print_query_result(connection, *, csv_output: bool) -> None:
    columns = [item[0] for item in connection.description or []]
    rows = connection.fetchall()
    if csv_output:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows(rows)
        return
    display_rows = [["NULL" if value is None else str(value) for value in row] for row in rows]
    widths = [
        max(len(str(column)), *(len(row[index]) for row in display_rows))
        for index, column in enumerate(columns)
    ]
    typer.echo(" | ".join(str(column).ljust(widths[index]) for index, column in enumerate(columns)))
    typer.echo("-+-".join("-" * width for width in widths))
    for row in display_rows:
        typer.echo(" | ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


@archive_app.command("query")
def archive_query(
    sql: str = typer.Argument(..., help="one read-only SELECT statement"),
    csv_output: bool = typer.Option(False, "--csv", help="emit CSV with a header"),
):
    """Run one research-only SELECT against the bars and raw_payloads archive views."""
    from data_lake.archive.lens import connect_lens

    from ibkr_trader.config import get_settings

    if not sql.lstrip().lower().startswith(("select", "with")):
        typer.echo("error: archive query accepts one SELECT statement only", err=True)
        raise typer.Exit(code=1)

    connection = None
    try:
        connection = connect_lens(get_settings())
        duckdb_module = __import__("duckdb")
        statements = connection.extract_statements(sql)
        if len(statements) != 1 or statements[0].type != duckdb_module.StatementType.SELECT:
            raise ValueError("archive query accepts one SELECT statement only")
        connection.execute(sql)
        _print_query_result(connection, csv_output=csv_output)
    except Exception as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    finally:
        if connection is not None:
            connection.close()


@archive_app.command("bars")
def archive_bars(
    older_than_days: int = typer.Option(365, min=0, help="only archive bars older than this"),
    bar_size: list[str] = typer.Option(
        [], help='bar sizes to archive, e.g. "1 min" (default: every size except "1 day")'
    ),
):
    """Upload old intraday bars as Parquet, verify the upload, then delete the local rows.
    Daily bars never leave Postgres — they are the training/backtest input."""
    from data_lake.archive import archive_price_bars

    from ibkr_trader.db.session import get_session

    store = _archive_store()
    try:
        with get_session() as session:
            result = archive_price_bars(
                session, store, older_than_days=older_than_days, bar_sizes=bar_size or None
            )
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    for key, count in result.objects.items():
        typer.echo(f"  {key} ← {count} bars")
    typer.echo(f"archived {result.rows_archived} bars → deleted {result.rows_removed} local rows")


@archive_app.command("raw")
def archive_raw(
    min_age_days: int = typer.Option(
        0, min=0, help="fetched_at grace period before a scored raw blob is offloaded"
    ),
):
    """Upload scored raw provider payloads (news/social) as Parquet, verify, then NULL them
    locally. The rows themselves (title, body, sentiment, hashed author) stay in Postgres."""
    from data_lake.archive import archive_raw_payloads

    from ibkr_trader.db.session import get_session

    store = _archive_store()
    try:
        with get_session() as session:
            result = archive_raw_payloads(session, store, min_age_days=min_age_days)
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    for key, count in result.objects.items():
        typer.echo(f"  {key} ← {count} payloads")
    typer.echo(f"archived {result.rows_archived} payloads → NULLed {result.rows_removed} local")


@archive_app.command("restore-bars")
def archive_restore_bars(
    start: str = typer.Option(..., help="restore window start YYYY-MM-DD"),
    end: str = typer.Option(..., help="restore window end YYYY-MM-DD"),
    bar_size: list[str] = typer.Option([], help="bar sizes to restore (default: all archived)"),
):
    """Pull archived bars for a date range back into Postgres (idempotent), e.g. before
    training an intraday model — the trainer itself stays DB-only."""
    from data_lake.archive import restore_price_bars

    from ibkr_trader.db.session import get_session

    store = _archive_store()
    try:
        with get_session() as session:
            count = restore_price_bars(
                session,
                store,
                start=_parse_date(start, "--start"),
                end=_parse_date(end, "--end"),
                bar_sizes=bar_size or None,
            )
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"restored {count} bars")


@archive_app.command("restore-raw")
def archive_restore_raw(
    start: str = typer.Option(..., help="restore window start YYYY-MM-DD"),
    end: str = typer.Option(..., help="restore window end YYYY-MM-DD"),
):
    """Refill NULLed raw payloads from the archive for reprocessing (idempotent; never
    overwrites a populated payload, never inserts rows)."""
    from data_lake.archive import restore_raw_payloads

    from ibkr_trader.db.session import get_session

    store = _archive_store()
    try:
        with get_session() as session:
            counts = restore_raw_payloads(
                session, store, start=_parse_date(start, "--start"), end=_parse_date(end, "--end")
            )
    except (RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    for table, count in counts.items():
        typer.echo(f"  {table}: refilled {count} payloads")


@archive_app.command("status")
def archive_status():
    """List archived data objects and their sizes (catalog metadata is shown by `catalog`)."""
    from data_lake.archive.catalog import CATALOG_PREFIX

    try:
        objects = [
            obj for obj in _archive_store().list_objects() if not obj.key.startswith(CATALOG_PREFIX)
        ]
    except Exception as exc:  # backend/network problems should print, not traceback
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if not objects:
        typer.echo("archive is empty")
        return
    total = sum(obj.size for obj in objects)
    for obj in objects:
        typer.echo(f"  {obj.size / 1_048_576:>9.2f} MiB  {obj.key}")
    typer.echo(f"{len(objects)} object(s), {total / 1_048_576:.2f} MiB total")


@archive_app.command("catalog")
def archive_catalog(
    rebuild: bool = typer.Option(
        False, "--rebuild", help="recompute manifests by scanning every archived partition"
    ),
):
    """Show the self-describing catalog: per dataset, its natural key, row count, and span.

    ``--rebuild`` re-reads every partition to regenerate the manifests (needs the [archive]
    extra) — use it to backfill a bucket archived before catalogging, or after a manual change.
    """
    from data_lake.archive.catalog import load_catalog, rebuild_catalog

    store = _archive_store()
    try:
        catalog = rebuild_catalog(store) if rebuild else load_catalog(store)
    except Exception as exc:  # backend/network/parse problems should print, not traceback
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if not catalog:
        hint = "" if rebuild else " (run with --rebuild if the bucket predates catalogging)"
        typer.echo(f"catalog is empty{hint}")
        return
    for name, manifest in sorted(catalog.items()):
        if manifest.min_ts and manifest.max_ts:
            span = f"{manifest.min_ts:%Y-%m-%d} → {manifest.max_ts:%Y-%m-%d}"
        else:
            span = "—"
        typer.echo(
            f"{name}: {manifest.total_rows} rows in {len(manifest.partitions)} partition(s), "
            f"{span}  key={'+'.join(manifest.key_columns)}"
        )


@app.command("score-sentiment")
def score_sentiment_command():
    """Score every unscored news/social row with the configured model.

    Scoring marks a row consumed, which lets the prune job drop its raw payload.
    """
    from ibkr_trader.scheduler import run_sentiment_scoring

    counts = run_sentiment_scoring()
    typer.echo(f"scored {counts}")


@sentiment_app.command("download")
def sentiment_download():
    """Download ProsusAI/finbert into the local Hugging Face cache."""
    from ibkr_trader.signals.finbert import download_model

    try:
        download_model()
    except (OSError, RuntimeError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo("FinBERT model cached and ready")


@sentiment_app.command("rescore")
def sentiment_rescore(
    model: str = typer.Option("finbert", help="target model: finbert or vader"),
    limit: int | None = typer.Option(None, min=1, help="maximum rows across both tables"),
):
    """Re-score stored rows not already produced by the selected model."""
    from ibkr_trader.db.session import get_session
    from ibkr_trader.signals.sentiment import SentimentModel, rescore

    normalized = model.lower()
    if normalized not in {"vader", "finbert"}:
        raise typer.BadParameter("must be vader or finbert", param_hint="--model")
    try:
        with get_session() as session:
            counts = rescore(session, model=cast(SentimentModel, normalized), limit=limit)
    except RuntimeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"rescored {sum(counts.values())} rows with {normalized}: {counts}")


@app.command()
def report(
    output: Path = typer.Option(
        Path("report.html"), "--output", "-o", help="path to write the HTML report to"
    ),
    rebase: bool | None = typer.Option(
        None,
        "--rebase/--no-rebase",
        help="rebase each curve to 100 at its first point (default: on when >1 series overlays)",
    ),
    open_browser: bool = typer.Option(
        True, "--open/--no-open", help="open the report in the default browser when done"
    ),
):
    """Render persisted backtest_runs to a self-contained static HTML report — leaderboard plus
    interactive Plotly equity/drawdown charts, no server. Runs briefly and exits (nothing stays
    resident), so it's laptop-friendly. Needs the [report] extra (uv sync --extra report).

    Run new backtests with `ibkr-trader backtest run`, then regenerate this to view them.
    """
    import importlib.util
    import webbrowser

    if importlib.util.find_spec("plotly") is None:
        typer.echo("error: plotly is not installed — run `uv sync --extra report`", err=True)
        raise typer.Exit(code=1)

    from ibkr_trader.dashboard.report import build_report_from_db
    from ibkr_trader.db.session import get_session

    with get_session() as session:
        html = build_report_from_db(session, rebased=rebase)
    output.write_text(html, encoding="utf-8")
    typer.echo(f"wrote {output}")
    if open_browser:
        webbrowser.open(output.resolve().as_uri())


@app.command()
def serve():
    """Long-running mode: APScheduler jobs for periodic ingestion, scoring and raw pruning.

    Polls Reddit / Finnhub news / NewsAPI / Google Trends on the cadence in Settings, backfills
    Finnhub news history to the free-tier floor, scores unscored rows, and drops the ``raw``
    blob on rows already sentiment-scored. No trading loop — that stays out until backtests +
    paper validation justify it. Blocks; Ctrl-C to stop.

    Waits for the database before the startup jobs fire, and records every job's outcome to the
    health artifact — check it with `ibkr-trader health`, since a failing job is deliberately
    not fatal here and will not stand out in the log.
    """
    from ibkr_trader.scheduler import serve as run_scheduler

    run_scheduler()


@app.command()
def health(
    artifact: str = typer.Option("", help="health artifact path (default: from Settings)"),
    ignore: list[str] = typer.Option(
        [], "--ignore", help="job name to exclude from the exit code (repeatable)"
    ),
    as_json: bool = typer.Option(False, "--json", help="print the raw artifact instead"),
):
    """Report what each `serve` job last did — 'is ingestion actually pulling data?'.

    Reads the artifact `serve` writes after every job run. Exits 1 if any job is failing, stale
    or has never run, so it doubles as a check: a scheduler whose jobs all error still logs
    "executed successfully" per run, which is precisely how a dead pipeline went unnoticed for
    six days. Use --ignore for a job that is known-broken and being tracked elsewhere.

    Read the "last wrote" column separately from "last success": a poll whose provider answers
    200 OK with an empty list succeeds forever while ingesting nothing, and only that column
    shows it. It is not part of the exit code — plenty of runs legitimately write zero rows.
    """
    import json
    from datetime import UTC, datetime

    from ibkr_trader import job_health
    from ibkr_trader.config import get_settings

    path = artifact or get_settings().scheduler_health_file
    try:
        payload = job_health.load_artifact(path)
    except (OSError, ValueError) as exc:
        typer.echo(f"error: cannot read the health artifact at {path}: {exc}", err=True)
        typer.echo("is `serve` running? it writes this file after each job.", err=True)
        raise typer.Exit(code=1) from None

    if as_json:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        raise typer.Exit(code=0)

    jobs = payload.get("jobs", {})
    if not jobs:
        typer.echo(f"{path}: no jobs recorded yet")
        raise typer.Exit(code=1)

    def stamp(value):
        """`2026-08-04T22:28:45.608831+00:00` -> `2026-08-04 22:28:45` (the column is 20 wide)."""
        if not value:
            return "never"
        try:
            return datetime.fromisoformat(str(value)).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return str(value)[:19]

    now = datetime.now(UTC)
    ignored = {name.strip() for name in ignore if name.strip()}
    typer.echo(f"health artifact: {path} (written {payload.get('written_at', '?')})")
    typer.echo(
        f"{'job':<18}{'status':<20}{'last success':<21}{'last wrote':<21}"
        f"{'runs':>6}{'fails':>7}  last result/error"
    )
    unhealthy: list[str] = []
    for name, entry in sorted(jobs.items()):
        status = job_health.status_for(entry, now=now)
        if status != "ok" and name not in ignored:
            unhealthy.append(name)
        detail = entry.get("last_error") or entry.get("last_result") or ""
        mark = " (ignored)" if name in ignored and status != "ok" else ""
        typer.echo(
            f"{name:<18}{status + mark:<20}{stamp(entry.get('last_success')):<21}"
            f"{stamp(entry.get('last_wrote')):<21}"
            f"{entry.get('runs', 0):>6}{entry.get('failures', 0):>7}  {str(detail)[:70]}"
        )

    if unhealthy:
        typer.echo(f"\nunhealthy: {', '.join(unhealthy)}", err=True)
        raise typer.Exit(code=1)
    typer.echo("\nall jobs healthy")


if __name__ == "__main__":
    app()
