"""Runs against the real PostgreSQL from DATABASE_URL (rules §4)."""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

from zigbee2mqtt_secure_proxy import db, models

_ = models  # imported so the tables register on Base.metadata


def current_revision(database_url: str) -> str:
    engine = db.make_engine(database_url)
    try:
        with engine.connect() as connection:
            revision = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
    finally:
        engine.dispose()
    return str(revision)


def test_migrate_reaches_head(migrated: str) -> None:
    assert current_revision(migrated) == db.head_revision()


def test_migrate_is_repeatable(migrated: str) -> None:
    db.migrate(migrated)
    assert current_revision(migrated) == db.head_revision()


def test_models_match_the_migrations(migrated: str) -> None:
    engine = db.make_engine(migrated)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            differences = compare_metadata(context, db.Base.metadata)
    finally:
        engine.dispose()
    assert differences == []
