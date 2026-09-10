"""Database engine, session factory, and health checks.

Run directly to verify connectivity:

    python -m public_health_clustering.db
"""

from __future__ import annotations

import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session, sessionmaker

from public_health_clustering.config import Settings, get_settings


def build_database_url(settings: Settings) -> URL:
    """Assemble a SQLAlchemy URL, safely handling special characters in credentials."""
    return URL.create(
        drivername="postgresql+psycopg2",
        username=settings.postgres_user,
        password=settings.postgres_password,
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
    )


def create_db_engine(settings: Settings | None = None) -> Engine:
    """Create a new engine from the given Settings (or the current environment)."""
    return create_engine(build_database_url(settings or get_settings()), pool_pre_ping=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create a session factory bound to the given engine."""
    return sessionmaker(bind=engine)


def check_database_health(engine: Engine) -> tuple[str, str]:
    """Query the database and return (postgres_version, postgis_version)."""
    with engine.connect() as connection:
        postgres_version = connection.execute(text("SELECT version()")).scalar_one()
        postgis_version = connection.execute(text("SELECT PostGIS_Version()")).scalar_one()
    return postgres_version, postgis_version


def main() -> None:
    try:
        engine = create_db_engine()
        postgres_version, postgis_version = check_database_health(engine)
    except Exception as exc:
        print(f"Database health check failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(f"PostgreSQL version: {postgres_version}")
    print(f"PostGIS version: {postgis_version}")


if __name__ == "__main__":
    main()
