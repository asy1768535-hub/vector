from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import folders


class _StopAfterLock(RuntimeError):
    pass


class _LockProbeDb:
    def __init__(self) -> None:
        self.lock_key: int | None = None

    async def execute(self, statement, params=None):
        assert "pg_advisory_xact_lock" in str(statement).lower()
        self.lock_key = params["lock_key"]
        raise _StopAfterLock


@pytest.mark.parametrize("path", ["/shared", "/shared/left", "/shared/right"])
def test_ensure_folder_path_takes_one_library_lock_before_lookup(path):
    library = SimpleNamespace(id=uuid.uuid4())
    db = _LockProbeDb()

    with pytest.raises(_StopAfterLock):
        asyncio.run(folders.ensure_folder_path(db, library, path))

    assert db.lock_key == folders._folder_advisory_lock_key(library.id)


def test_sibling_paths_with_a_new_shared_root_use_the_same_lock():
    library = SimpleNamespace(id=uuid.uuid4())
    probes = []
    for path in ("/shared/left", "/shared/right"):
        db = _LockProbeDb()
        with pytest.raises(_StopAfterLock):
            asyncio.run(folders.ensure_folder_path(db, library, path))
        probes.append(db.lock_key)

    assert probes[0] == probes[1] == folders._folder_advisory_lock_key(library.id)


def test_empty_path_does_not_take_library_lock():
    library = SimpleNamespace(id=uuid.uuid4())
    db = _LockProbeDb()

    assert asyncio.run(folders.ensure_folder_path(db, library, None)) is None
    assert db.lock_key is None


_PG_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")


@pytest.mark.skipif(
    not _PG_DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL database",
)
def test_postgres_concurrent_sibling_paths_create_one_shared_root():
    async def exercise() -> None:
        schema = f"folders_1a_{uuid.uuid4().hex}"
        admin_engine = create_async_engine(_PG_DSN)
        engine = None
        try:
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
                await connection.execute(
                    text(
                        f'''
                        CREATE TABLE "{schema}".folders (
                            id uuid PRIMARY KEY,
                            library_id uuid NOT NULL,
                            parent_id uuid NULL,
                            name varchar(255) NOT NULL,
                            path text NOT NULL,
                            sort_order integer NOT NULL DEFAULT 0,
                            created_at timestamptz NOT NULL DEFAULT now(),
                            updated_at timestamptz NOT NULL DEFAULT now(),
                            deleted_at timestamptz NULL
                        )
                        '''
                    )
                )
                await connection.execute(
                    text(
                        f'''
                        CREATE UNIQUE INDEX uq_folders_library_parent_name_active
                        ON "{schema}".folders (library_id, parent_id, name)
                        WHERE deleted_at IS NULL
                        '''
                    )
                )

            engine = create_async_engine(
                _PG_DSN,
                connect_args={"server_settings": {"search_path": schema}},
            )
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            library = SimpleNamespace(id=uuid.uuid4())

            async def create(path: str) -> None:
                async with sessions() as db:
                    async with db.begin():
                        await folders.ensure_folder_path(db, library, path)

            paths = ["/shared/left", "/shared/right"] * 10
            await asyncio.gather(*(create(path) for path in paths))

            async with sessions() as db:
                rows = (
                    await db.execute(
                        text(
                            "SELECT id, parent_id, name FROM folders "
                            "WHERE library_id = :library_id AND deleted_at IS NULL"
                        ),
                        {"library_id": library.id},
                    )
                ).all()
            roots = [row for row in rows if row.parent_id is None and row.name == "shared"]
            assert len(roots) == 1
            children = [row for row in rows if row.parent_id == roots[0].id]
            assert sorted(row.name for row in children) == ["left", "right"]
            assert len(rows) == 3
        finally:
            if engine is not None:
                await engine.dispose()
            async with admin_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin_engine.dispose()

    asyncio.run(exercise())
