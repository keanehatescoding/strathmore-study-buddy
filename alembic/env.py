"""Alembic environment. Canonical source of metadata: app.models."""

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

import app.models  # noqa: F401 -- ensures metadata is populated
from alembic import context
from app.dburl import normalize_database_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Allow DATABASE_URL env override (psycopg sync driver for Alembic).
db_url = os.getenv("DATABASE_URL")
if db_url:
    # Alembic runs sync; convert async URL if ever used.
    db_url = db_url.replace("+asyncpg", "").replace("+async_psycopg", "")
    db_url = normalize_database_url(db_url)  # a host's postgres:// -> psycopg
    config.set_main_option("sqlalchemy.url", db_url)

target_metadata = SQLModel.metadata

# The web service and every job run `alembic upgrade head` on start (see
# DEPLOY.md), so two may migrate at once; the loser waits here, then finds the
# schema already at head.
MIGRATION_LOCK_ID = 0x5B_0A1E  # arbitrary, app-wide


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            if connection.dialect.name == "postgresql":
                connection.exec_driver_sql(
                    f"SELECT pg_advisory_xact_lock({MIGRATION_LOCK_ID})")
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
