"""Pytest fixtures — PostgreSQL test database.

Each test gets a fresh engine + session. Tables created/dropped per test.
Avoids asyncpg connection-sharing issues across event loops.
"""

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base

# Import models so they register on Base.metadata
from app.modules.channel.model import Channel  # noqa: F401
from app.modules.rawmessage.model import RawMessage  # noqa: F401


@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    """Function-scoped: fresh engine, create tables, yield session, drop tables."""
    engine = create_async_engine(settings.TEST_DATABASE_URL, echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()
