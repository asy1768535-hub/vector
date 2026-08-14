from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.import_uploads import _lock_staging_quota


_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL database",
)


def test_library_quota_lock_serializes_only_the_same_library() -> None:
    async def exercise() -> None:
        engine = create_async_engine(_DSN)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        library_id = uuid.uuid4()
        other_library_id = uuid.uuid4()
        first = sessions()
        second = sessions()
        other = sessions()
        try:
            await first.begin()
            await _lock_staging_quota(first, library_id=library_id)

            await other.begin()
            await asyncio.wait_for(
                _lock_staging_quota(other, library_id=other_library_id),
                timeout=1,
            )
            await other.commit()

            await second.begin()
            waiting = asyncio.create_task(
                _lock_staging_quota(second, library_id=library_id)
            )
            await asyncio.sleep(0.1)
            assert not waiting.done()
            await first.commit()
            await asyncio.wait_for(waiting, timeout=1)
            await second.commit()
        finally:
            for session in (first, second, other):
                await session.close()
            await engine.dispose()

    asyncio.run(exercise())
