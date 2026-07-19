from importlib import import_module

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_migration_backfills_only_previously_scored_rows(monkeypatch):
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    for name in ("news_articles", "social_posts"):
        sa.Table(
            name,
            metadata,
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("sentiment", sa.Float, nullable=True),
        )
    metadata.create_all(engine)

    migration = import_module("migrations.versions.f6a7b8c9d0e1_sentiment_model_provenance")
    with engine.begin() as connection:
        for table in ("news_articles", "social_posts"):
            connection.execute(
                sa.text(f"INSERT INTO {table} (id, sentiment) VALUES (1, 0.5), (2, NULL)")
            )
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.upgrade()

        for table in ("news_articles", "social_posts"):
            rows = connection.execute(
                sa.text(f"SELECT id, sentiment_model FROM {table} ORDER BY id")
            ).all()
            assert rows == [(1, "vader"), (2, None)]
