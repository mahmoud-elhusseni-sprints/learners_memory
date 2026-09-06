"""Async engine + Unit of Work."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from learner_memory.core.config import get_settings


@lru_cache
def get_engine():
    s = get_settings()
    return create_async_engine(s.database_url, pool_pre_ping=True, pool_size=10, max_overflow=20)


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


@asynccontextmanager
async def unit_of_work() -> AsyncIterator[AsyncSession]:
    """One transaction per use case. Commit on success, roll back on anything else."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with unit_of_work() as session:
        yield session
