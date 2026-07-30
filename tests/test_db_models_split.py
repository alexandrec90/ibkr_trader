"""The data-lake seam, from the consumer's side: which tables may leave this repo.

Since Phase 2 the two halves of the schema live in different repositories — the shareable
tables in the ``data_lake`` package, the audit trail and risk state here in
``db/trading_models.py`` — but they still map onto **one** ``Base``, because Alembic's
``target_metadata`` has to describe the whole database.

That single ``Base`` is the thing worth guarding. If this repo ever ends up on a different
``Base`` from the package's (a stale install, a copied ``declarative_base()``, a rename), the
metadata silently splits in two and the next ``alembic revision --autogenerate`` proposes
dropping every table it can no longer see. Nothing else in the test suite would notice.

The package enforces its own half of the rule (``tests/test_lake_seam.py`` there: nothing
account-shaped may be declared in the lake). These tests are the consumer's half.
"""

import ast
import pathlib

import pytest
from data_lake.db import models as lake_models
from data_lake.db.base import Base as LakeBase

from ibkr_trader.db import trading_models
from ibkr_trader.db.models import LAKE_TABLES, TRADING_TABLES, Base

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "ibkr_trader"

#: Tables that are the audit trail / live risk state. Spelled out literally rather than
#: derived, so deleting one from TRADING_TABLES cannot silently pass this file.
NEVER_SHAREABLE = {"orders", "executions", "strategy_snapshots", "predictions", "backtest_runs"}


def _mapped_tables(module) -> set[str]:
    """Table names of the declarative classes actually defined in ``module``."""
    return {
        obj.__tablename__
        for obj in vars(module).values()
        if isinstance(obj, type)
        and issubclass(obj, Base)
        and obj is not Base
        and obj.__module__ == module.__name__
    }


def test_both_halves_share_one_declarative_base():
    """The cross-package invariant: two ``Base`` objects would split ``Base.metadata`` in two."""
    assert Base is LakeBase


def test_partition_is_exhaustive_and_disjoint():
    """Every mapped table is classified exactly once — no table escapes the split."""
    assert LAKE_TABLES & TRADING_TABLES == set()
    assert LAKE_TABLES | TRADING_TABLES == set(Base.metadata.tables)


def test_declared_sets_match_where_the_classes_actually_live():
    """The constants describe reality: a class moved across the repo boundary fails here."""
    assert _mapped_tables(lake_models) == set(LAKE_TABLES)
    assert _mapped_tables(trading_models) == set(TRADING_TABLES)


def test_audit_trail_tables_are_never_lake_side():
    """The plan's hard guardrail: orders/executions/risk state stay in this repo."""
    assert NEVER_SHAREABLE <= set(TRADING_TABLES)
    assert NEVER_SHAREABLE & set(LAKE_TABLES) == set()


def test_the_installed_package_declares_no_trading_table():
    """Defence against a bad package version, not just against our own edits.

    ``data_lake`` is a dependency now, so a table could arrive here without any change in this
    repo. Upgrading into a version that declares ``orders`` must fail loudly.
    """
    assert NEVER_SHAREABLE & _mapped_tables(lake_models) == set()


def test_trading_models_are_declared_only_in_this_repo():
    """The reverse leak: a trading class must not be importable from the shared package."""
    for name in ("Order", "Execution", "Prediction", "BacktestRun", "StrategySnapshot"):
        assert not hasattr(lake_models, name), (
            f"{name} is exported by the shared package — the audit trail must stay consumer-side"
        )


@pytest.mark.parametrize(
    "module_path",
    sorted(
        p.relative_to(SRC).as_posix()
        for tree in ("signals", "backtest", "execution")
        for p in (SRC / tree).rglob("*.py")
    ),
)
def test_the_hot_path_never_imports_the_research_lens(module_path):
    """CLAUDE.md's rule: archived data is restored into Postgres before it feeds the pipeline.

    The lens reads Parquet straight from the bucket, with no look-ahead protection and no
    guarantee the partitions are complete; a model or an order path reading it directly would
    silently bypass both. Moving the lens into a dependency made this *easier* to do by
    accident, so the guard moved here with it.
    """
    tree = ast.parse((SRC / module_path).read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    offenders = {name for name in imported if "archive.lens" in name}
    assert offenders == set(), f"{module_path} imports the research lens: {sorted(offenders)}"


def test_models_facade_reexports_every_mapped_class():
    """``from ibkr_trader.db.models import X`` keeps working for both halves."""
    from ibkr_trader.db import models

    for module in (lake_models, trading_models):
        for name, obj in vars(module).items():
            if (
                isinstance(obj, type)
                and issubclass(obj, Base)
                and obj is not Base
                and obj.__module__ == module.__name__
            ):
                assert getattr(models, name) is obj
                assert name in models.__all__


def test_metadata_is_complete_when_importing_only_the_facade():
    """Alembic autogenerate reads Base.metadata via db.models — it must see all 15 tables."""
    import importlib

    models = importlib.import_module("ibkr_trader.db.models")
    assert set(models.Base.metadata.tables) == set(LAKE_TABLES) | set(TRADING_TABLES)
