"""Connector contract shared by all data sources.

Connectors are deliberately dumb: fetch from one provider, upsert into Postgres, return a
count. No feature engineering here (that lives in `signals/`). Idempotency comes from the
unique constraints in db/models.py — always upsert, never blind-insert.

Both of a connector's ambient dependencies are *injected*, not reached for:

- **Configuration** — pass a ``Settings`` to the constructor, or let ``self.settings`` fall back
  to the process-wide one.
- **Database access** — pass a ``session_factory`` (any zero-arg callable returning a context
  manager that yields a committed-on-exit ``Session``), or let ``self.session()`` fall back to
  ``db.session.get_session``. Connectors never own an engine.

Both fallbacks sit behind lazy imports, so this module carries no import-time dependency on
``ibkr_trader.config`` or ``ibkr_trader.db`` — which is what lets the ``data-lake`` package take
the connector tree in Phase 2 (docs/plans/active/data-lake.md): a foreign consumer supplies its
own settings object and its own session factory over its own engine.
"""

import abc
import hashlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ibkr_trader.config import Settings

#: Zero-arg callable yielding a short-lived ``Session`` that commits on clean exit.
SessionFactory = Callable[[], AbstractContextManager["Session"]]


def resolve_session_factory(session_factory: "SessionFactory | None") -> "SessionFactory":
    """The injected factory, else this repo's process-wide ``get_session``.

    Module-level batch helpers in the connector tree take a ``session_factory`` argument and
    resolve it through here, so they stay as injectable as the connector classes themselves.
    """
    if session_factory is not None:
        return session_factory
    from ibkr_trader.db.session import get_session

    return get_session


class Connector(abc.ABC):
    """One external data source."""

    #: short identifier stored in the `source`/`platform` columns
    name: str = "override-me"

    def __init__(
        self,
        settings: "Settings | None" = None,
        session_factory: "SessionFactory | None" = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory

    @property
    def settings(self) -> "Settings":
        """The injected settings, else the process-wide ones (resolved once, on first use)."""
        if self._settings is None:
            from ibkr_trader.config import get_settings

            self._settings = get_settings()
        return self._settings

    @property
    def session_factory(self) -> "SessionFactory":
        """The injected session factory, else this repo's (resolved once, on first use)."""
        if self._session_factory is None:
            self._session_factory = resolve_session_factory(None)
        return self._session_factory

    def session(self) -> "AbstractContextManager[Session]":
        """Open one short-lived session — the only way a connector reaches the database."""
        return self.session_factory()

    @abc.abstractmethod
    def fetch(self, **kwargs) -> int:
        """Pull new data and persist it. Returns number of rows upserted."""


def stable_hash(value: str) -> str:
    """sha256 hex digest — used for author pseudonymization and URL-based external ids."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
