"""``ibkr-trader questrade ...``: read-only Questrade sync and order recommendations.

Registered on the root app by ``ibkr_trader.cli``. Heavy imports stay inside each
command, as everywhere else in the CLI.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import typer

if TYPE_CHECKING:
    from ibkr_trader.advisory.questrade import QuestradeClient

questrade_app = typer.Typer(
    help="Read-only Questrade sync and limit-order recommendations. Never places an order."
)

#: The dotenv file the rotated token is written back to: the same relative path
#: ``Settings`` reads, so the next run picks up the replacement.
ENV_FILE = Path(".env")
DEFAULT_CSV = "reports/questrade-plan.csv"


def _fail(message: str) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code=1)


def _connect() -> "QuestradeClient":
    from ibkr_trader.advisory.questrade import (
        TOKEN_HELP,
        EnvTokenStore,
        QuestradeClient,
        QuestradeError,
    )
    from ibkr_trader.config import get_settings

    store = EnvTokenStore(ENV_FILE)
    token = get_settings().questrade_token.strip()
    if not token:
        _fail(f"QUESTRADE_TOKEN is not set; {TOKEN_HELP}")
    if store.load() != token:
        _fail(
            f"QUESTRADE_TOKEN must be set in {ENV_FILE.resolve()}, not only in the shell: it "
            "is replaced on every use and only that file can be rewritten"
        )
    try:
        return QuestradeClient.connect(store, token)
    except (QuestradeError, OSError) as exc:
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
    """Recommend laddered limit orders for every Questrade account. Advisory only."""
    from sqlalchemy.exc import OperationalError

    from ibkr_trader.advisory.planner import PlanConfig, plan_all
    from ibkr_trader.advisory.questrade import QuestradeError
    from ibkr_trader.advisory.report import format_plans, plan_notes, write_csv
    from ibkr_trader.advisory.sync import account_states, resolve_targets
    from ibkr_trader.advisory.targets import latest_targets
    from ibkr_trader.db.session import get_session

    today = datetime.now(tz=UTC).date()
    try:
        with get_session() as session:
            targets = latest_targets(session, strategy, today=today)
    except ValueError as exc:
        _fail(str(exc))
    except OperationalError:
        _fail("cannot reach the database; start it with `docker compose up -d db`")
    client = _connect()
    try:
        resolved, notes = resolve_targets(client, targets)
        states = account_states(client)
    except QuestradeError as exc:
        _fail(str(exc))
    config = PlanConfig(
        min_order_cad=min_order, max_position_pct=max_position, max_new_names=max_new, exits=exits
    )
    plans = plan_all(states, resolved, config, today)
    title = f"Questrade order plan · {strategy} (snapshot {targets.asof}) · {today}"
    for line in format_plans(plans, title, plan_notes(targets, today) + notes):
        typer.echo(line)
    count = write_csv(plans, Path(csv_path))
    typer.echo(f"\n{count} order ticket(s) written to {csv_path}")
