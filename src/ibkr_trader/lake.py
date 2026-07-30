"""Wiring the shared ``data_lake`` package to this repo's config and engine.

The package owns no config system and no engine — it is handed both, once, at whichever entry
point this process started from (see docs/plans/active/data-lake.md, Phase 2). That is what
lets the same connectors serve a second project without dragging this repo's ``.env`` schema
or its Postgres engine along.

Both entry points call :func:`configure_lake`: the CLI's root callback (so every
``ibkr-trader`` subcommand is wired) and :func:`ibkr_trader.scheduler.build_scheduler` (so
``serve`` is wired even when a caller builds the scheduler directly). It is idempotent, so
calling it from both costs nothing.

The module attributes are looked up at call time, not bound at import time, so a test that
monkeypatches ``ibkr_trader.config.get_settings`` still wins.
"""

import data_lake

from ibkr_trader import config
from ibkr_trader.db import session as db_session


def configure_lake() -> None:
    """Hand the shared package this repo's settings and session factory."""
    data_lake.configure(
        settings=config.get_settings(),
        session_factory=db_session.get_session,
    )
