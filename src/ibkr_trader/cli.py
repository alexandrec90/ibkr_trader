"""Typer CLI — entry point `ibkr-trader` (see [project.scripts] in pyproject.toml)."""

import typer

app = typer.Typer(help="ibkr-trader: ingestion, backtesting and (paper) trading via IBKR.")
ingest_app = typer.Typer(help="Run one ingestion connector.")
app.add_typer(ingest_app, name="ingest")


@app.command()
def ibkr_check():
    """Connect to TWS/IB Gateway, print server time + account summary (read-only smoke test)."""
    from ibkr_trader.config import get_settings

    settings = get_settings()
    typer.echo(f"Connecting to {settings.ibkr_host}:{settings.ibkr_port} "
               f"(clientId={settings.ibkr_client_id}, env={settings.environment})")
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
def ingest_prices(symbol: str, source: str = typer.Option("fmp", help="fmp|alpha_vantage|ibkr")):
    connectors = {
        "fmp": "ibkr_trader.ingestion.market.fmp:FmpConnector",
        "alpha_vantage": "ibkr_trader.ingestion.market.alpha_vantage:AlphaVantageConnector",
        "ibkr": "ibkr_trader.ingestion.market.ibkr_historical:IbkrHistoricalConnector",
    }
    if source not in connectors:
        raise typer.BadParameter(f"unknown source {source!r}")
    module_path, class_name = connectors[source].split(":")
    module = __import__(module_path, fromlist=[class_name])
    count = getattr(module, class_name)().fetch(symbol=symbol)
    typer.echo(f"upserted {count} bars")


@app.command()
def backtest(strategy: str = "momentum_baseline"):
    """Run a backtest over stored data (no network)."""
    # TODO(skeleton): parse dates/symbols, call BacktestEngine.run, print metrics table.
    raise typer.Exit(code=1)


@app.command()
def serve():
    """Long-running mode: APScheduler jobs for periodic ingestion (+ later, trading loop)."""
    # TODO(skeleton): BlockingScheduler with cron jobs per connector, honoring each
    # source's rate limits (docs/data-sources.md). Trading loop stays out until
    # backtests + paper validation exist.
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
