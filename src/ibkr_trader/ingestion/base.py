"""Connector contract shared by all data sources.

Connectors are deliberately dumb: fetch from one provider, upsert into Postgres, return a
count. No feature engineering here (that lives in `signals/`). Idempotency comes from the
unique constraints in db/models.py — always upsert, never blind-insert.

Both of a connector's ambient dependencies are *injected*, not reached for:

- **Configuration** — pass a ``Settings`` to the constructor, or let ``self.settings`` fall
  back to the process-wide one.
- **Database access** — pass a ``session_factory`` (any zero-arg callable returning a context
  manager that yields a SQLAlchemy ``Session`` and commits on clean exit), or let
  ``self.session()`` fall back to this repo's ``db.session.get_session``.

Both fallbacks live behind lazy imports, so this module — and the whole connector tree — has
no import-time dependency on ``ibkr_trader.config`` or ``ibkr_trader.db.session``. Those two
lazy imports are the seam the ``data-lake`` package cuts in Phase 2
(docs/plans/active/data-lake.md): a foreign consumer supplies its own settings object and its
own session factory, and owns its engine.

Module-level helpers in the connector tree take the same ``session_factory`` argument and
resolve it through :func:`resolve_session_factory`.
"""

import abc
import hashlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from ibkr_trader.config import Settings

#: Zero-arg callable yielding a transactional ``Session`` context manager (commit on exit).
SessionFactory = Callable[[], AbstractContextManager[Session]]


def resolve_session_factory(session_factory: SessionFactory | None) -> SessionFactory:
    """The injected factory, else this repo's process-wide one (imported lazily).

    The lazy import is deliberate: it keeps ``ibkr_trader.db.session`` — and the engine it
    owns — off the connector tree's import graph, so an extracted package can be handed a
    caller-supplied factory instead.
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
        session_factory: SessionFactory | None = None,
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
    def session_factory(self) -> SessionFactory:
        """The injected session factory, else the default one (resolved once, on first use)."""
        if self._session_factory is None:
            self._session_factory = resolve_session_factory(None)
        return self._session_factory

    def session(self) -> AbstractContextManager[Session]:
        """Open one short-lived transactional session: ``with self.session() as session:``."""
        return self.session_factory()

    @abc.abstractmethod
    def fetch(self, **kwargs) -> int:
        """Pull new data and persist it. Returns number of rows upserted."""


def stable_hash(value: str) -> str:
    """sha256 hex digest — used for author pseudonymization and URL-based external ids."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
