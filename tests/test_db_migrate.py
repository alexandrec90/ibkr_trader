"""`ibkr_trader.db.migrate`: what `serve --migrate` runs in the deployed container."""

from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

from ibkr_trader.db import migrate

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_the_project_root_is_the_checkout_holding_the_migrations():
    """The image lays out `/app/src/ibkr_trader` and `/app/migrations` as the checkout does,
    so resolving from the module finds the chain in both."""
    assert migrate.PROJECT_ROOT == REPO_ROOT


def test_the_config_names_the_real_chain_by_absolute_path():
    """A relative `script_location` only works from the right working directory."""
    config = migrate.alembic_config()
    location = Path(config.get_main_option("script_location"))

    assert location.is_absolute()
    assert location == REPO_ROOT / "migrations"
    assert ScriptDirectory.from_config(config).get_current_head()


def test_the_config_has_no_file_so_env_py_leaves_logging_alone():
    """`migrations/env.py` runs `fileConfig` when the config has a file, and that disables
    every logger already created -- inside `serve`, the scheduler's job log."""
    assert migrate.alembic_config().config_file_name is None


def test_a_root_without_migrations_is_refused_by_name(tmp_path):
    with pytest.raises(FileNotFoundError, match="no alembic migrations"):
        migrate.alembic_config(tmp_path)


def test_upgrade_head_upgrades_the_chain_to_head(monkeypatch):
    seen = {}

    def fake_upgrade(config, revision):
        seen["location"] = config.get_main_option("script_location")
        seen["revision"] = revision

    monkeypatch.setattr(migrate.command, "upgrade", fake_upgrade)

    migrate.upgrade_head()

    assert seen == {"location": str(REPO_ROOT / "migrations"), "revision": "head"}
