from alembic import context
from sqlalchemy import engine_from_config, pool

from public_health_clustering.config import get_settings
from public_health_clustering.db import build_database_url
from public_health_clustering.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", build_database_url(get_settings()).render_as_string(hide_password=False))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata,
                      literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section),
                                     prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_offline() if context.is_offline_mode() else run_migrations_online()
