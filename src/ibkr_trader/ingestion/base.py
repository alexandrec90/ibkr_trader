"""Connector contract shared by all data sources.

Connectors are deliberately dumb: fetch from one provider, upsert into Postgres, return a
count. No feature engineering here (that lives in `signals/`). Idempotency comes from the
unique constraints in db/models.py — always upsert, never blind-insert.

Configuration is *injected*, not reached for: pass a ``Settings`` to the constructor, or let
``self.settings`` fall back to the process-wide one. Keeping ``get_settings`` behind a lazy
import leaves this module free of an import-time dependency on ``ibkr_trader.config``, which is
what lets the ``data-lake`` package take it in Phase 2 (docs/plans/active/data-lake.md) — a
foreign consumer supplies its own settings object instead.
"""

import abc
import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ibkr_trader.config import Settings


class Connector(abc.ABC):
    """One external data source."""

    #: short identifier stored in the `source`/`platform` columns
    name: str = "override-me"

    def __init__(self, settings: "Settings | None" = None) -> None:
        self._settings = settings

    @property
    def settings(self) -> "Settings":
        """The injected settings, else the process-wide ones (resolved once, on first use)."""
        if self._settings is None:
            from ibkr_trader.config import get_settings

            self._settings = get_settings()
        return self._settings

    @abc.abstractmethod
    def fetch(self, **kwargs) -> int:
        """Pull new data and persist it. Returns number of rows upserted."""


def stable_hash(value: str) -> str:
    """sha256 hex digest — used for author pseudonymization and URL-based external ids."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
