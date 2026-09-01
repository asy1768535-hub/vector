from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import socket
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import db as app_db
from app.api.import_uploads import router as import_uploads_router
from app.auth.backend import current_active_user
from app.config import settings
from app.db import get_db
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_import_job import DocumentImportJob
from app.models.embedding_job import EmbeddingJob
from app.models.library import Library
from app.models.organization import Organization
from app.models.user import User
from app.services import import_uploads
from app.workers import importer


_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable fully migrated PostgreSQL database",
)
_ROOT = Path(__file__).resolve().parents[1]


def _async_dsn() -> str:
    return make_url(_DSN).set(drivername="postgresql+asyncpg").render_as_string(
        hide_password=False
    )


async def _wait_for_server(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    for _ in range(500):
        if server.started:
            return
        if task.done():
            await task
            pytest.fail("uvicorn exited before accepting connections")
        await asyncio.sleep(0.01)
    pytest.fail("uvicorn did not start within five seconds")


@pytest.mark.asyncio
async def test_closed_http_client_leaves_queued_upload_for_later_importer_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(_async_dsn())
    observer_engine = create_async_engine(_async_dsn(), poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    observers = async_sessionmaker(observer_engine, expire_on_commit=False)
    marker = uuid.uuid4().hex
    user_id = uuid.uuid4()
    organization_id = uuid.uuid4()
    library_id = uuid.uuid4()
    library_slug = f"lifecycle-{marker[:12]}"
    payload = b"alpha beta gamma\n"

    monkeypatch.setattr(settings, "organization_authorization_enabled", False)
    monkeypatch.setattr(settings, "enable_evidence_write_path", False)
    monkeypatch.setattr(settings, "revision_file_storage_enabled", False)
    monkeypatch.setattr(settings, "import_worker_batch_size", 1)
    monkeypatch.setattr(settings, "import_staging_dir", str(tmp_path / "staging"))
    monkeypatch.setattr(settings, "document_files_dir", str(tmp_path / "files"))
    monkeypatch.setattr(app_db, "async_session_factory", sessions)
    monkeypatch.setattr(importer, "async_session_factory", sessions)

    server: uvicorn.Server | None = None
    server_task: asyncio.Task[None] | None = None
    listening_socket: socket.socket | None = None
    try:
        expected_heads = set(
            ScriptDirectory.from_config(
                AlembicConfig(str(_ROOT / "alembic.ini"))
            ).get_heads()
        )
        async with observer_engine.connect() as connection:
            migrated_heads = set(
                (await connection.execute(text("SELECT version_num FROM alembic_version")))
                .scalars()
                .all()
            )
            existing_jobs = await connection.scalar(
                select(func.count()).select_from(DocumentImportJob)
            )
        assert migrated_heads == expected_heads
        assert existing_jobs == 0, "test requires an empty disposable migrated database"

        async with sessions() as db:
            db.add_all(
                [
                    User(
                        id=user_id,
                        email=f"{marker}@example.test",
                        hashed_password="not-used",
                        is_active=True,
                        is_superuser=True,
                        is_verified=True,
                    ),
                    Organization(
                        id=organization_id,
                        slug=f"org-{marker[:12]}",
                        name="Upload lifecycle acceptance",
                    ),
                ]
            )
            await db.flush()
            db.add(
                Library(
                    id=library_id,
                    organization_id=organization_id,
                    slug=library_slug,
                    name="Upload lifecycle acceptance",
                    embedding_model="test-embedding",
                    embedding_dim=8,
                    qdrant_collection=f"test_{marker}",
                    chunk_size=1000,
                    chunk_overlap=120,
                    created_by=user_id,
                )
            )
            await db.commit()

        actor = SimpleNamespace(id=user_id, is_active=True, is_superuser=True)

        async def override_db():
            async with sessions() as db:
                yield db

        async def override_user():
            return actor

        api = FastAPI()
        api.include_router(import_uploads_router)
        api.dependency_overrides[get_db] = override_db
        api.dependency_overrides[current_active_user] = override_user

        listening_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listening_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listening_socket.bind(("127.0.0.1", 0))
        listening_socket.listen(128)
        port = listening_socket.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                api,
                host="127.0.0.1",
                port=port,
                lifespan="off",
                access_log=False,
                log_level="error",
            )
        )
        server_task = asyncio.create_task(server.serve(sockets=[listening_socket]))
        await _wait_for_server(server, server_task)

        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            timeout=10,
        ) as client:
            created = await client.post(
                f"/libraries/{library_slug}/import-sessions",
                json={
                    "batch_id": str(uuid.uuid4()),
                    "file_name": "client-closed.txt",
                    "content_type": "text/plain",
                    "size_bytes": len(payload),
                },
            )
            assert created.status_code == 201, created.text
            job_id = uuid.UUID(created.json()["id"])

            appended = await client.put(
                f"/libraries/{library_slug}/import-sessions/{job_id}/content",
                headers={"Upload-Offset": "0"},
                content=payload,
            )
            assert appended.status_code == 204, appended.text
            assert appended.headers["Upload-Offset"] == str(len(payload))

            completed = await client.post(
                f"/libraries/{library_slug}/import-sessions/{job_id}/complete"
            )
            assert completed.status_code == 202, completed.text
            assert completed.json()["status"] == "queued"

        async with observers() as observer:
            queued = await observer.get(DocumentImportJob, job_id)
            assert queued is not None
            assert (queued.status, queued.current_stage) == ("queued", "queued")
            assert queued.upload_offset == len(payload) == queued.size_bytes
            assert queued.sha256 == hashlib.sha256(payload).hexdigest()
            assert queued.worker_id is None
            assert queued.claimed_at is None
            staged_path = import_uploads.staging_path(queued.staging_key)
            assert staged_path.read_bytes() == payload
            assert (
                await observer.scalar(
                    select(func.count()).select_from(Document).where(
                        Document.library_id == library_id
                    )
                )
            ) == 0

        assert await importer.run_once() == 1

        async with observers() as observer:
            imported = await observer.get(DocumentImportJob, job_id)
            assert imported is not None
            assert (imported.status, imported.current_stage) == (
                "processing",
                "embedding",
            )
            assert imported.worker_id is None
            assert imported.claimed_at is None
            assert imported.document_id is not None
            assert imported.embedding_job_id is not None

            document = await observer.get(Document, imported.document_id)
            embedding_job = await observer.get(EmbeddingJob, imported.embedding_job_id)
            assert document is not None
            assert document.status == "pending"
            assert embedding_job is not None
            assert embedding_job.status == "pending"
            assert embedding_job.document_id == document.id
            assert (
                await observer.scalar(
                    select(func.count()).select_from(Chunk).where(
                        Chunk.document_id == document.id
                    )
                )
            ) > 0
    finally:
        if server is not None:
            server.should_exit = True
        try:
            if server_task is not None:
                await asyncio.wait_for(server_task, timeout=10)
        finally:
            if listening_socket is not None:
                listening_socket.close()
            async with observer_engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM sys_libraries WHERE id = :library_id"),
                    {"library_id": library_id},
                )
                await connection.execute(
                    text("DELETE FROM sys_organizations WHERE id = :organization_id"),
                    {"organization_id": organization_id},
                )
                await connection.execute(
                    text("DELETE FROM sys_users WHERE id = :user_id"),
                    {"user_id": user_id},
                )
            await observer_engine.dispose()
            await engine.dispose()
            shutil.rmtree(tmp_path, ignore_errors=True)
