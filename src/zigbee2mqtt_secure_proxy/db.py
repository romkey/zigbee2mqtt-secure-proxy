"""Database engines and migrations."""

from importlib.resources import files

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import URL, Connection, Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Parent of every model; its metadata is what Alembic compares against."""


def _psycopg_url(database_url: str) -> URL:
    return make_url(database_url).set(drivername="postgresql+psycopg")


def make_engine(database_url: str) -> Engine:
    """An engine for a postgresql:// URL, using the psycopg 3 driver."""
    return create_engine(_psycopg_url(database_url))


def make_async_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(_psycopg_url(database_url))


def make_sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def alembic_config(connection: Connection | None = None) -> Config:
    """Alembic settings, built in code so an installed wheel needs no alembic.ini.

    https://alembic.sqlalchemy.org/en/latest/api/config.html
    """
    config = Config()
    config.set_main_option(
        "script_location", str(files("zigbee2mqtt_secure_proxy") / "migrations")
    )
    config.attributes["connection"] = connection
    return config


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def migrate(database_url: str) -> None:
    """Bring the schema up to date. Only the `migrate` command calls this."""
    engine = make_engine(database_url)
    try:
        with engine.begin() as connection:
            command.upgrade(alembic_config(connection), "head")
    finally:
        engine.dispose()
