"""Database package: models (``db.models``) and the engine/session factory (``db.session``).

``Base`` and the session helpers are re-exported lazily (PEP 562). Importing them eagerly here
would mean that *any* ``import ibkr_trader.db.<anything>`` — including a connector importing
``db.models`` — also built the ``db.session`` module and its ``ibkr_trader.config`` dependency.
The connector tree is meant to carry neither (see ``ingestion/base.py`` and
docs/plans/active/data-lake.md), so the package root resolves them on first attribute access
instead.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from data_lake.db.base import Base

    from ibkr_trader.db.session import get_engine, get_session

__all__ = ["Base", "get_engine", "get_session"]

_LAZY = {
    "Base": "ibkr_trader.db.models",
    "get_engine": "ibkr_trader.db.session",
    "get_session": "ibkr_trader.db.session",
}


def __getattr__(name: str):
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(module_name), name)
