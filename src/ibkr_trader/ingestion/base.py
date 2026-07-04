"""Connector contract shared by all data sources.

Connectors are deliberately dumb: fetch from one provider, upsert into Postgres, return a
count. No feature engineering here (that lives in `signals/`). Idempotency comes from the
unique constraints in db/models.py — always upsert, never blind-insert.
"""

import abc
import hashlib


class Connector(abc.ABC):
    """One external data source."""

    #: short identifier stored in the `source`/`platform` columns
    name: str = "override-me"

    @abc.abstractmethod
    def fetch(self, **kwargs) -> int:
        """Pull new data and persist it. Returns number of rows upserted."""


def stable_hash(value: str) -> str:
    """sha256 hex digest — used for author pseudonymization and URL-based external ids."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
