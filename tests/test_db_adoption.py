"""The other side of the data-lake seam: which of the lake's tables our Alembic may touch.

``tests/test_db_models_split.py`` guards what may *leave* this repo. This file guards what
may **arrive**: the lake is shared, so another consumer adding a table changes this repo's
``Base.metadata`` with no commit here. ``listings`` did exactly that in August 2026 — it is
apt-finder's housing data, and without a filter the next ``revision --autogenerate`` would
have proposed creating it in the trading database.
"""

from data_lake.db import models as lake_models

from ibkr_trader.db.adoption import (
    ADOPTED_LAKE_TABLES,
    ADOPTED_TABLES,
    FOREIGN_LAKE_TABLES,
    include_object,
)
from ibkr_trader.db.models import LAKE_TABLES, TRADING_TABLES, Base


def _admits(table_name: str) -> bool:
    """Run the hook the way Alembic runs it for a table object."""
    return include_object(Base.metadata.tables[table_name], table_name, "table", False, None)


def test_foreign_tables_are_named_literally():
    """Spelled out rather than derived, so a table quietly leaving our set cannot pass here."""
    assert FOREIGN_LAKE_TABLES == {"listings"}


def test_foreign_tables_really_are_lake_tables():
    """A name that no longer exists upstream is a stale exception, not a working filter."""
    assert FOREIGN_LAKE_TABLES <= LAKE_TABLES


def test_adoption_covers_every_table_we_own():
    """The partition, restated for migrations: ours is everything except another consumer's."""
    assert ADOPTED_TABLES == (LAKE_TABLES - FOREIGN_LAKE_TABLES) | TRADING_TABLES
    assert ADOPTED_TABLES == set(Base.metadata.tables) - FOREIGN_LAKE_TABLES


def test_trading_tables_are_adopted_unconditionally():
    """The regression the filter itself could cause: filtering out our own audit trail.

    ``include_only`` is applied to both sides of the comparison, so a trading table missing
    from the adoption list would be silently dropped from autogenerate — the exact failure
    mode ``db/models.py`` warns about, arriving by a different route.
    """
    assert TRADING_TABLES <= ADOPTED_TABLES
    for name in sorted(TRADING_TABLES):
        assert _admits(name), f"{name} is invisible to autogenerate"


def test_every_adopted_lake_table_is_admitted():
    assert ADOPTED_LAKE_TABLES == LAKE_TABLES - FOREIGN_LAKE_TABLES
    for name in sorted(ADOPTED_LAKE_TABLES):
        assert _admits(name), f"{name} is invisible to autogenerate"


def test_another_consumers_table_is_rejected():
    """The regression this module exists for: no CREATE TABLE listings in our migrations."""
    assert not _admits("listings")


def test_columns_and_indexes_inherit_their_tables_decision():
    """A half-adopted table would migrate columns for a table that is never created."""
    listings = Base.metadata.tables["listings"]
    ours = Base.metadata.tables["orders"]

    assert not include_object(listings.c.external_id, "external_id", "column", False, None)
    assert include_object(ours.c.id, "id", "column", False, None)

    for index in listings.indexes:
        assert not include_object(index, index.name, "index", False, None)


def test_a_new_foreign_table_is_excluded_the_moment_it_is_classified():
    """The maintenance path: naming a table foreign is all it takes to keep it out."""
    from data_lake.db.adoption import include_only

    hook = include_only(ADOPTED_TABLES - {"price_bars"})
    bars = Base.metadata.tables["price_bars"]
    assert not hook(bars, "price_bars", "table", False, None)
    assert not hook(bars.c.close, "close", "column", False, None)


def test_the_filter_tracks_the_lake_rather_than_a_frozen_copy():
    """If the package adds a table, it must land in our metadata *and* be classified.

    This is the assertion that fails the next time a consumer does what apt-finder did — the
    point being that it fails here, in a test naming the seam, rather than four tests deep in
    the model-split file with no hint about migrations.
    """
    declared_upstream = {
        obj.__tablename__
        for obj in vars(lake_models).values()
        if isinstance(obj, type)
        and issubclass(obj, Base)
        and obj is not Base
        and obj.__module__ == lake_models.__name__
    }
    assert declared_upstream == set(LAKE_TABLES)
    assert declared_upstream - FOREIGN_LAKE_TABLES == set(ADOPTED_LAKE_TABLES)
