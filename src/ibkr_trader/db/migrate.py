"""Bring the database to the newest migration -- what ``ibkr-trader serve --migrate`` runs first.

The deployed ``app`` container is rebuilt onto merged code by ``docker compose up -d --build``,
and that path ran no ``alembic upgrade head``: a merged migration never reached the database,
and the scheduler ran models ahead of their schema. So the container migrates itself on start.

The config is built in code rather than read from ``alembic.ini`` for two reasons:

- ``script_location = migrations`` in the ini is relative to the *working directory*, so the
  same command finds the chain in the image's ``/app`` and in a checkout only by accident.
  Resolving it from this file holds in both: the project is installed editable, so this module
  sits at ``<root>/src/ibkr_trader/db/`` either way.
- ``migrations/env.py`` calls ``logging.config.fileConfig`` whenever the config has a file,
  and ``fileConfig`` disables every logger that already exists. Run inside ``serve``, after
  the scheduler module is imported, that would silence its job log. With no file it is skipped.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config

#: The checkout, or the image's ``/app``: ``src/ibkr_trader/db/migrate.py`` is three below it.
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def alembic_config(root: Path = PROJECT_ROOT) -> Config:
    """An alembic ``Config`` naming ``root``'s migration chain by absolute path."""
    migrations = root / "migrations"
    if not (migrations / "env.py").is_file():
        raise FileNotFoundError(
            f"no alembic migrations at {migrations}; the image must COPY migrations/ beside src/"
        )
    config = Config()
    config.set_main_option("script_location", str(migrations))
    return config


def upgrade_head(root: Path = PROJECT_ROOT) -> None:
    """``alembic upgrade head`` against the configured ``DATABASE_URL``."""
    command.upgrade(alembic_config(root), "head")
