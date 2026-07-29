"""The data-lake seam: which tables may leave this repo, and which never may.

``db/models.py`` was split into ``lake_models`` (shareable: market/corporate/news/social/
features) and ``trading_models`` (orders, executions, predictions, backtests, strategy state)
so Phase 2 of docs/plans/active/data-lake.md can extract the lake half into a standalone
package. The plan's guardrail — *orders, executions and risk state never leave this repo's
Postgres* — is only real if something enforces it, which is what this module does.

These tests are deliberately structural (module membership, import direction) rather than
behavioural: they fail when a future change puts a trading table on the shareable side, which
is exactly the mistake that would leak the audit trail into a shared bucket.
"""

import ast
import pathlib

import pytest

from ibkr_trader.archive.catalog import DATASET_SPECS
from ibkr_trader.db import lake_models, trading_models
from ibkr_trader.db.base import Base
from ibkr_trader.db.models import LAKE_TABLES, TRADING_TABLES

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


def _mapped_tables_to_classes(module) -> set[str]:
    """Class names of the declarative classes actually defined in ``module``."""
    return {
        obj.__name__
        for obj in vars(module).values()
        if isinstance(obj, type)
        and issubclass(obj, Base)
        and obj is not Base
        and obj.__module__ == module.__name__
    }


def test_partition_is_exhaustive_and_disjoint():
    """Every mapped table is classified exactly once — no table escapes the split."""
    assert LAKE_TABLES & TRADING_TABLES == set()
    assert LAKE_TABLES | TRADING_TABLES == set(Base.metadata.tables)


def test_declared_sets_match_where_the_classes_actually_live():
    """The constants describe reality: a class moved between modules fails here."""
    assert _mapped_tables(lake_models) == set(LAKE_TABLES)
    assert _mapped_tables(trading_models) == set(TRADING_TABLES)


def test_audit_trail_tables_are_never_lake_side():
    """The plan's hard guardrail: orders/executions/risk state stay in this repo."""
    assert NEVER_SHAREABLE <= set(TRADING_TABLES)
    assert NEVER_SHAREABLE & set(LAKE_TABLES) == set()


def test_lake_models_does_not_import_trading_models():
    """Dependency direction is trading -> lake. The reverse would drag orders into the package."""
    tree = ast.parse((SRC / "db" / "lake_models.py").read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not any("trading_models" in name for name in imported), (
        f"lake_models must not import trading_models; found {sorted(imported)}"
    )


def test_lake_models_only_depends_on_the_shared_base():
    """The lake half's only intra-package import is db.base — what makes extraction mechanical."""
    tree = ast.parse((SRC / "db" / "lake_models.py").read_text(encoding="utf-8"))
    internal = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("ibkr_trader")
    }
    assert internal == {"ibkr_trader.db.base"}


def _model_names_imported_by(module_path: pathlib.Path) -> set[str]:
    """Names a module pulls out of ``db.models`` / ``db.lake_models`` / ``db.trading_models``."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("ibkr_trader.db.")
        and node.module.endswith("models")
        for alias in node.names
    }


@pytest.mark.parametrize(
    "module_path",
    sorted(
        p.relative_to(SRC).as_posix()
        for tree in ("ingestion", "archive")
        for p in (SRC / tree).rglob("*.py")
    ),
)
def test_extracted_trees_only_import_lake_side_model_classes(module_path):
    """``ingestion/`` and ``archive/`` move into the shared package in Phase 2.

    They import through the ``db.models`` façade (which registers every table, so Alembic and
    SQLite ``create_all`` stay complete), but the *classes* they name must all be lake-side —
    naming ``Order`` or ``Execution`` here is how the audit trail would follow them into the
    package a foreign consumer imports.
    """
    lake_classes = _mapped_tables_to_classes(lake_models)
    trading_classes = _mapped_tables_to_classes(trading_models)
    imported = _model_names_imported_by(SRC / module_path)
    offenders = imported & trading_classes
    assert offenders == set(), f"{module_path} imports trading-side models {sorted(offenders)}"
    # every remaining model name must be something the lake half actually defines
    unknown = imported - lake_classes - {"Base", "JsonVariant", "SqliteFriendlyBigInt"}
    assert unknown == set(), f"{module_path} imports unknown model names {sorted(unknown)}"


@pytest.mark.parametrize("spec", DATASET_SPECS, ids=lambda s: s.name)
def test_archive_only_publishes_lake_datasets(spec):
    """Nothing the archive writes to the bucket may correspond to a trading table."""
    assert spec.name in LAKE_TABLES
    assert spec.name not in TRADING_TABLES


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
