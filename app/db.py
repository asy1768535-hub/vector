"""SQLAlchemy 2.0 async engine / session 与 Declarative Base。"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """所有 ORM 模型继承此基类。"""


_engine: AsyncEngine = create_async_engine(
    settings.db_dsn_async,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    echo=settings.app_debug,
)

async_session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session


def get_engine() -> AsyncEngine:
    return _engine
