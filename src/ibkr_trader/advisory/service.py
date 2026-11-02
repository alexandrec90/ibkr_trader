"""Build a Questrade order plan without a terminal: the one path the CLI and the UI share.

Failures raise :class:`PlanError` with the message the owner should read; the CLI turns it
into an exit code and the UI into an HTTP error. Heavy imports stay inside each function so
the tests can replace ``sync``/``targets``/``get_session`` at their module seams.
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from ibkr_trader.advisory.planner import AccountPlan, PlanConfig

if TYPE_CHECKING:
    from ibkr_trader.advisory.questrade import QuestradeClient

#: The dotenv file the rotated token is written back to: the same relative path
#: ``Settings`` reads, so the next run picks up the replacement.
ENV_FILE = Path(".env")


class PlanError(Exception):
    """A plan could not be built; the message says why and what to do about it."""


@dataclass(frozen=True)
class PlanResult:
    strategy: str
    snapshot_asof: date
    today: date
    plans: list[AccountPlan]
    #: Warnings about the target book and its symbol resolution, shown above every account.
    notes: list[str]

    @property
    def title(self) -> str:
        return (
            f"Questrade order plan · {self.strategy} (snapshot {self.snapshot_asof}) · {self.today}"
        )


def connect(env_file: Path = ENV_FILE) -> "QuestradeClient":
    """Log in with the token in ``env_file``, which the login rewrites with its replacement."""
    from ibkr_trader.advisory.questrade import (
        TOKEN_HELP,
        EnvTokenStore,
        QuestradeClient,
        QuestradeError,
    )
    from ibkr_trader.config import get_settings

    store = EnvTokenStore(env_file)
    token = get_settings().questrade_token.strip()
    if not token:
        raise PlanError(f"QUESTRADE_TOKEN is not set; {TOKEN_HELP}")
    if store.load() != token:
        raise PlanError(
            f"QUESTRADE_TOKEN must be set in {env_file.resolve()}, not only in the shell: it "
            "is replaced on every use and only that file can be rewritten"
        )
    try:
        return QuestradeClient.connect(store, token)
    except (QuestradeError, OSError) as exc:
        raise PlanError(str(exc)) from exc


def make_plan(strategy: str, config: PlanConfig, today: date) -> PlanResult:
    """Read the strategy's target book, then Questrade's accounts, and plan every account.

    The database is read first, so a missing snapshot fails before the single-use token
    is spent.
    """
    from sqlalchemy.exc import OperationalError

    from ibkr_trader.advisory.planner import plan_all
    from ibkr_trader.advisory.questrade import QuestradeError
    from ibkr_trader.advisory.report import plan_notes
    from ibkr_trader.advisory.sync import account_states, resolve_targets
    from ibkr_trader.advisory.targets import latest_targets
    from ibkr_trader.db.session import get_session

    try:
        with get_session() as session:
            targets = latest_targets(session, strategy, today=today)
    except ValueError as exc:
        raise PlanError(str(exc)) from exc
    except OperationalError as exc:
        raise PlanError(
            "cannot reach the database; start it with `docker compose up -d db`"
        ) from exc
    client = connect()
    try:
        resolved, notes = resolve_targets(client, targets)
        states = account_states(client)
    except QuestradeError as exc:
        raise PlanError(str(exc)) from exc
    return PlanResult(
        strategy=strategy,
        snapshot_asof=targets.asof,
        today=today,
        plans=plan_all(states, resolved, config, today),
        notes=plan_notes(targets, today) + notes,
    )
