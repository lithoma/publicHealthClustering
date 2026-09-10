"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _REPO_ROOT / ".env"


@dataclass(frozen=True)
class Settings:
    """Local configuration for connecting to PostgreSQL/PostGIS."""

    postgres_host: str
    postgres_port: int
    postgres_db: str
    postgres_user: str
    postgres_password: str = field(repr=False)


def get_settings() -> Settings:
    """Build Settings from environment variables, loading the repo's .env file first."""
    load_dotenv(dotenv_path=_ENV_FILE)

    raw_port = os.environ.get("POSTGRES_PORT", "5432")
    try:
        postgres_port = int(raw_port)
    except ValueError as exc:
        raise ValueError(f"POSTGRES_PORT must be an integer, got {raw_port!r}") from exc

    return Settings(
        postgres_host=os.environ.get("POSTGRES_HOST", "localhost"),
        postgres_port=postgres_port,
        postgres_db=os.environ.get("POSTGRES_DB", "public_health_clustering"),
        postgres_user=os.environ.get("POSTGRES_USER", "public_health"),
        postgres_password=os.environ.get("POSTGRES_PASSWORD", "public_health_dev"),
    )
