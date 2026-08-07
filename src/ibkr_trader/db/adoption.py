"""Which parts of the shared lake schema this project materialises.

Importing :mod:`data_lake.db.models` registers the lake's **entire** schema on the one shared
``Base.metadata``. That is the point of a shared lake — and it has a consequence that only
shows up the first time a *second* consumer adds a table. When apt-finder put ``listings``
in the lake, that table appeared in this repo's metadata too, without a single line changing
here. Left alone, the next ``alembic revision --autogenerate`` would propose creating a
housing-classifieds table in the trading database.

Seeing the whole schema is not the same as owning it. This module names the tables this
project keeps and hands them to :func:`data_lake.db.adoption.include_only`, which builds the
Alembic hook. The mechanism lives upstream because every consumer needs it; only the *list*
is a local decision. The filter applies to both sides of the comparison, so an unadopted
table is never created from the metadata and never proposed for deletion because it is
absent from the database.

It lives here rather than inside ``migrations/env.py`` because that file runs migrations as
an import side effect and so cannot be imported by a test.
"""

from data_lake.db.adoption import include_only

from ibkr_trader.db.models import LAKE_TABLES, TRADING_TABLES

__all__ = ["ADOPTED_LAKE_TABLES", "ADOPTED_TABLES", "include_object"]

#: Lake tables belonging to another consumer. This repo's migrations created every other
#: lake table — the package was carved out of this schema — so the adoption list is easier
#: to state and to keep honest as the exceptions rather than the inclusions. Add a name here
#: when a new consumer puts its own dataset in the lake.
FOREIGN_LAKE_TABLES = frozenset({"listings"})

#: Lake tables this project materialises: everything the lake declares that isn't somebody
#: else's. ``tests/test_db_adoption.py`` pins the exceptions literally, so a lake table that
#: quietly stops being ours cannot slip through as a silently shrinking set.
ADOPTED_LAKE_TABLES = LAKE_TABLES - FOREIGN_LAKE_TABLES

#: What autogenerate may see. The trading tables are unconditional — they are declared in
#: this repo and there is no consumer they could belong to.
ADOPTED_TABLES = ADOPTED_LAKE_TABLES | TRADING_TABLES

#: The Alembic ``include_object`` hook, wired in ``migrations/env.py``.
include_object = include_only(ADOPTED_TABLES)
