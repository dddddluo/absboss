from __future__ import annotations

import asyncio

from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from absbot.models import Base


def create_engine(mysql_dsn: str) -> AsyncEngine:
    return create_async_engine(mysql_dsn, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def run_migrations(mysql_dsn: str) -> None:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", mysql_dsn)
    await asyncio.to_thread(command.upgrade, config, "head")

