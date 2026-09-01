from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.models.document import Document
from app.models.document_import_job import DocumentImportJob
from app.models.library import Library
from app.models.organization import Organization
from app.models.sync_source import SyncSource
from app.schemas.v02_m4 import SyncDocumentUpsertRequest
from app.services import import_uploads, sync_documents
from app.workers import importer


_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable fully migrated PostgreSQL database",
)


def _async_dsn() -> str:
    return make_url(_DSN).set(drivername="postgresql+asyncpg").render_as_string(
        hide_password=False
    )


async def _wait_for_lock_wait(
    observer_engine: AsyncEngine,
    application_name: str,
) -> str:
    async with asyncio.timeout(5):
        while True:
            async with observer_engine.connect() as connection:
                wait_event = await connection.scalar(
                    text(
                        "SELECT wait_event FROM pg_stat_activity "
                        "WHERE application_name = :application_name "
                        "AND wait_event_type = 'Lock'"
                    ),
                    {"application_name": application_name},
                )
            if wait_event is not None:
                return str(wait_event)
            await asyncio.sleep(0.01)


def _sqlstate(error: BaseException) -> str | None:
    return getattr(getattr(error, "orig", None), "sqlstate", None) or getattr(
        error, "sqlstate", None
    )


@pytest.mark.asyncio
async def test_importer_and_sync_upsert_use_one_folder_document_lock_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    marker = uuid.uuid4().hex
    organization_id = uuid.uuid4()
    library_id = uuid.uuid4()
    sync_source_id = uuid.uuid4()
    document_id = uuid.uuid4()
    job_id = uuid.uuid4()
    content = b"same text"
    staging_key = f"{job_id.hex}.upload"
    importer_application = f"importer-lock-order-{marker}"
    sync_application = f"sync-lock-order-{marker}"

    importer_engine = create_async_engine(
        _async_dsn(),
        connect_args={"server_settings": {"application_name": importer_application}},
    )
    sync_engine = create_async_engine(
        _async_dsn(),
        connect_args={"server_settings": {"application_name": sync_application}},
    )
    observer_engine = create_async_engine(_async_dsn(), poolclass=NullPool)
    importer_sessions = async_sessionmaker(importer_engine, expire_on_commit=False)
    sync_sessions = async_sessionmaker(sync_engine, expire_on_commit=False)
    target_locked = asyncio.Event()
    release_importer = asyncio.Event()
    importer_task: asyncio.Task | None = None
    sync_task: asyncio.Task | None = None
    original_target_document = importer._target_document

    async def paused_target_document(db, job, source_path):
        target = await original_target_document(db, job, source_path)
        target_locked.set()
        await release_importer.wait()
        return target

    async def unchanged_reingest(**_kwargs):
        return None, 0, False

    async def prepared_file(*_args, **_kwargs):
        return object()

    monkeypatch.setattr(settings, "import_staging_dir", str(tmp_path / "staging"))
    monkeypatch.setattr(importer, "async_session_factory", importer_sessions)
    monkeypatch.setattr(importer, "_target_document", paused_target_document)
    monkeypatch.setattr(importer, "_prepare_revision_file", prepared_file)
    monkeypatch.setattr(
        importer,
        "ingest_service",
        SimpleNamespace(reingest_document=unchanged_reingest),
    )

    try:
        async with importer_sessions() as db:
            db.add(
                Organization(
                    id=organization_id,
                    slug=f"lock-order-{marker[:12]}",
                    name="Importer folder lock order",
                )
            )
            await db.flush()
            db.add(
                Library(
                    id=library_id,
                    organization_id=organization_id,
                    slug=f"lock-order-{marker[:12]}",
                    name="Importer folder lock order",
                    embedding_model="test-embedding",
                    embedding_dim=8,
                    qdrant_collection=f"lock_order_{marker}",
                    chunk_size=1000,
                    chunk_overlap=120,
                )
            )
            await db.flush()
            db.add(
                SyncSource(
                    id=sync_source_id,
                    library_id=library_id,
                    source_key="crm",
                    display_name="CRM",
                    status="active",
                )
            )
            db.add(
                Document(
                    id=document_id,
                    library_id=library_id,
                    sync_source_id=sync_source_id,
                    external_id="contract-1",
                    title="Contract",
                    content_hash=hashlib.sha256(content).hexdigest(),
                    status="pending",
                )
            )
            await db.flush()
            db.add(
                DocumentImportJob(
                    id=job_id,
                    library_id=library_id,
                    batch_id=uuid.uuid4(),
                    file_name="contract.txt",
                    relative_path="import-folder/contract.txt",
                    content_type="text/plain",
                    size_bytes=len(content),
                    upload_offset=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    staging_key=staging_key,
                    replace_document_id=document_id,
                    status="processing",
                    current_stage="validating",
                    attempt_count=1,
                )
            )
            await db.commit()

        import_uploads.staging_path(staging_key).write_bytes(content)

        async def run_sync_upsert():
            await target_locked.wait()
            async with sync_sessions() as db:
                async with db.begin():
                    library = await db.get(Library, library_id)
                    assert library is not None
                    return await sync_documents.upsert_sync_document(
                        db,
                        library,
                        "crm",
                        SyncDocumentUpsertRequest(
                            external_id="contract-1",
                            title="Contract",
                            text=content.decode("utf-8"),
                            folder_path="/sync-folder",
                        ),
                        created_by=None,
                    )

        importer_task = asyncio.create_task(importer._process_claimed_job(job_id))
        sync_task = asyncio.create_task(run_sync_upsert())
        await asyncio.wait_for(target_locked.wait(), timeout=5)
        wait_event = await _wait_for_lock_wait(observer_engine, sync_application)
        release_importer.set()
        importer_result, sync_result = await asyncio.gather(
            importer_task,
            sync_task,
            return_exceptions=True,
        )

        errors = [
            result
            for result in (importer_result, sync_result)
            if isinstance(result, BaseException)
        ]
        deadlocks = [error for error in errors if _sqlstate(error) == "40P01"]
        assert not deadlocks, f"unexpected PostgreSQL deadlock: {deadlocks!r}"
        assert not errors, errors
        assert wait_event == "advisory"
        assert sync_result.operation == "unchanged"

        async with importer_sessions() as db:
            imported_job = await db.get(DocumentImportJob, job_id)
            imported_document = await db.get(Document, document_id)
            assert imported_job is not None
            assert imported_document is not None
            assert (imported_job.status, imported_job.current_stage) == (
                "succeeded",
                "completed",
            )
            assert imported_document.source_path == "/import-folder/contract.txt"
            assert imported_document.folder_id is not None
    finally:
        release_importer.set()
        pending_tasks = [
            task
            for task in (importer_task, sync_task)
            if task is not None and not task.done()
        ]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        async with observer_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM document_import_jobs WHERE library_id = :library_id"),
                {"library_id": library_id},
            )
            await connection.execute(
                text("DELETE FROM documents WHERE library_id = :library_id"),
                {"library_id": library_id},
            )
            await connection.execute(
                text("DELETE FROM sync_sources WHERE library_id = :library_id"),
                {"library_id": library_id},
            )
            await connection.execute(
                text("DELETE FROM folders WHERE library_id = :library_id"),
                {"library_id": library_id},
            )
            await connection.execute(
                text("DELETE FROM sys_libraries WHERE id = :library_id"),
                {"library_id": library_id},
            )
            await connection.execute(
                text("DELETE FROM sys_organizations WHERE id = :organization_id"),
                {"organization_id": organization_id},
            )
        await importer_engine.dispose()
        await sync_engine.dispose()
        await observer_engine.dispose()
