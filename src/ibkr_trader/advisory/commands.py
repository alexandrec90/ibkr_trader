"""``ibkr-trader questrade ...``: read-only Questrade sync and order recommendations.

Registered on the root app by ``ibkr_trader.cli``. Heavy imports stay inside each
command, as everywhere else in the CLI.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import typer

from ibkr_trader.advisory.service import ENV_FILE, PlanError

if TYPE_CHECKING:
    from ibkr_trader.advisory.questrade import QuestradeClient

questrade_app = typer.Typer(
    help="Read-only Questrade sync and limit-order recommendations. Never places an order."
)

DEFAULT_CSV = "reports/questrade-plan.csv"


def _fail(message: str) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code=1)


def _connect() -> "QuestradeClient":
    from ibkr_trader.advisory.service import connect

    try:
        return connect(ENV_FILE)
    except PlanError as exc:
        _fail(str(exc))


@questrade_app.command("check")
def questrade_check() -> None:
    """List each account's equity and spendable cash: proves the token works (and rotates it)."""
    from ibkr_trader.advisory.questrade import QuestradeError
    from ibkr_trader.advisory.report import account_line
    from ibkr_trader.advisory.sync import account_states

    client = _connect()
    try:
        states = account_states(client)
    except QuestradeError as exc:
        _fail(str(exc))
    for state in states:
        typer.echo(account_line(state))
    typer.echo(f"{len(states)} active account(s); the rotated token was saved to {ENV_FILE}")


@questrade_app.command("plan")
def questrade_plan(
    strategy: str = typer.Option(
        ..., help="the strategy whose latest forward snapshot sets the target book"
    ),
    min_order: float = typer.Option(
        1000.0, min=0, help="smallest order worth placing, in CAD; small accounts hold fewer names"
    ),
    max_position: float = typer.Option(
        0.20, min=0.01, max=1.0, help="cap on any single stock's share of an account (ETFs exempt)"
    ),
    max_new: int = typer.Option(3, min=0, help="most names opened per account per run"),
    exits: bool = typer.Option(
        False, "--exits", help="also sell holdings that are outside the strategy"
    ),
    csv_path: str = typer.Option(DEFAULT_CSV, "--csv", help="where the order tickets are written"),
) -> None:
    """Recommend laddered limit orders for every Questrade account. Advisory only.

    The full plan is also written as JSON beside the CSV, which is what `ibkr-trader ui`
    shows without spending the token again.
    """
    from ibkr_trader.advisory.planner import PlanConfig
    from ibkr_trader.advisory.report import format_plans, write_csv, write_json
    from ibkr_trader.advisory.service import make_plan

    config = PlanConfig(
        min_order_cad=min_order, max_position_pct=max_position, max_new_names=max_new, exits=exits
    )
    try:
        result = make_plan(strategy, config, datetime.now(tz=UTC).date())
    except PlanError as exc:
        _fail(str(exc))
    for line in format_plans(result.plans, result.title, result.notes):
        typer.echo(line)
    count = write_csv(result.plans, Path(csv_path))
    json_path = Path(csv_path).with_suffix(".json")
    write_json(result, json_path, config)
    typer.echo(f"\n{count} order ticket(s) written to {csv_path} (full plan: {json_path})")
