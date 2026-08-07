"""Alembic environment — wired to ibkr_trader settings and models metadata.

``target_metadata`` is ``ibkr_trader.db.models.Base.metadata``, which covers both halves of
the schema: the lake tables the shared package declares and the trading tables declared here.
Import the facade, never ``data_lake.db.models`` directly, or autogenerate stops seeing the
trading tables and proposes dropping them.

The lake is shared, so its metadata also carries tables belonging to *other* consumers.
:mod:`ibkr_trader.db.adoption` holds the filter that keeps those out of our migrations, and
the adoption list itself; it lives there because this file runs migrations as an import side
effect and so cannot be covered by a test.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from ibkr_trader.config import get_settings
from ibkr_trader.db.adoption import include_object
from ibkr_trader.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
