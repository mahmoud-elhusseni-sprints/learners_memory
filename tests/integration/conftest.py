"""Real Postgres for behaviour that lives in SQL.

One Postgres 16 container per session, migrated with the project's Alembic
chain (so the migrations are exercised too). Each test gets a fresh engine on
its own event loop and empty tables. Skipped only when Docker is unavailable.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[2]
POSTGRES_IMAGE = "postgres:16-alpine"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


@pytest.fixture(scope="session")
def postgres_url():
    if not _docker_available():
        pytest.skip("Docker is not available; integration tests need a Postgres container")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer(POSTGRES_IMAGE, driver="asyncpg") as pg:
        url = pg.get_connection_url()
        _migrate(url)
        yield url


def _migrate(url: str) -> None:
    from alembic import command
    from alembic.config import Config

    from learner_memory.core.config import get_settings

    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    command.upgrade(cfg, "head")


@pytest.fixture
async def db(postgres_url, monkeypatch):
    """Point the app's session factory at the container; truncate afterwards."""
    from learner_memory.core.config import get_settings
    from learner_memory.db import session as session_module
    from learner_memory.db.models import Base

    monkeypatch.setenv("DATABASE_URL", postgres_url)
    get_settings.cache_clear()
    session_module.get_engine.cache_clear()
    session_module.get_sessionmaker.cache_clear()
    engine = session_module.get_engine()
    try:
        yield session_module
    finally:
        tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {tables} CASCADE"))
        await engine.dispose()
        session_module.get_engine.cache_clear()
        session_module.get_sessionmaker.cache_clear()
        get_settings.cache_clear()
