from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.config import Settings, settings
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.document_revision_file import DocumentRevisionFile
from app.models.library import Library
from app.models.revision_retention import RevisionRetentionRecord
from app.services.revision_cleanup import claim_revision_cleanup


_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL admin connection",
)
_ADMIN_URL = make_url(_DSN).set(drivername="postgresql+asyncpg") if _DSN else None


def _admin_url() -> URL:
    assert _ADMIN_URL is not None
    return _ADMIN_URL


async def _admin(sql: str) -> None:
    import asyncpg

    url = _admin_url()
    connection = await asyncpg.connect(
        host=url.host,
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        database=url.database or "postgres",
    )
    try:
        await connection.execute(sql)
    finally:
        await connection.close()


def _database_url(name: str) -> URL:
    return _admin_url().set(database=name)


async def _execute(name: str, sql: str):
    engine = create_async_engine(_database_url(name))
    try:
        async with engine.begin() as connection:
            result = await connection.execute(text(sql))
            return result.fetchall() if result.returns_rows else None
    finally:
        await engine.dispose()


def _configure_alembic(monkeypatch, name: str) -> None:
    url = _database_url(name)
    monkeypatch.setattr(settings, "db_host", url.host)
    monkeypatch.setattr(settings, "db_port", int(url.port or 5432))
    monkeypatch.setattr(settings, "db_user", url.username)
    monkeypatch.setattr(settings, "db_password", url.password)
    monkeypatch.setattr(settings, "db_name", name)


def _create_database(monkeypatch, prefix: str) -> tuple[str, URL]:
    name = f"{prefix}_{uuid.uuid4().hex[:8]}"
    _configure_alembic(monkeypatch, name)
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    return name, _database_url(name)


def _drop_database(name: str) -> None:
    asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


def test_0028_upgrades_and_downgrades_exactly(monkeypatch):
    name, _ = _create_database(monkeypatch, "vkt_v08_cleanup_migration")
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "0027")
        command.upgrade(config, "0028")
        retention_columns = asyncio.run(
            _execute(
                name,
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'revision_retention_records'
                  AND column_name IN (
                    'attempt_count', 'available_at', 'worker_id', 'claim_token',
                    'claimed_at', 'lease_expires_at', 'finished_at', 'last_error_code'
                  )
                ORDER BY column_name
                """,
            )
        )
        assert {row[0] for row in retention_columns} == {
            "attempt_count",
            "available_at",
            "worker_id",
            "claim_token",
            "claimed_at",
            "lease_expires_at",
            "finished_at",
            "last_error_code",
        }
        file_columns = asyncio.run(
            _execute(
                name,
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'document_revision_files'
                  AND column_name IN (
                    'lifecycle_status', 'deleted_at', 'delete_verified_at'
                  )
                """,
            )
        )
        assert {row[0] for row in file_columns} == {
            "lifecycle_status",
            "deleted_at",
            "delete_verified_at",
        }
        command.downgrade(config, "0027")
        assert asyncio.run(
            _execute(
                name,
                "SELECT version_num FROM alembic_version",
            )
        )[0][0] == "0027"
        assert asyncio.run(
            _execute(
                name,
                """
                SELECT count(*) FROM information_schema.columns
                WHERE table_name = 'revision_retention_records'
                  AND column_name = 'claim_token'
                """,
            )
        )[0][0] == 0
        assert asyncio.run(
            _execute(
                name,
                """
                SELECT count(*) FROM information_schema.columns
                WHERE table_name = 'document_revision_files'
                  AND column_name = 'lifecycle_status'
                """,
            )
        )[0][0] == 0
    finally:
        _drop_database(name)


async def _seed_cleanup(Session):
    now = datetime.now(timezone.utc)
    library_id = uuid.uuid4()
    document_id = uuid.uuid4()
    old_revision_id = uuid.uuid4()
    replacement_revision_id = uuid.uuid4()
    revision_file_id = uuid.uuid4()
    record_id = uuid.uuid4()
    async with Session() as db:
        db.add(
            Library(
                id=library_id,
                slug=f"cleanup-{library_id.hex[:8]}",
                name="Cleanup",
                embedding_model="bge-m3",
                embedding_dim=1024,
                qdrant_collection=f"cleanup_{library_id.hex[:8]}",
                revision_retention_enabled=True,
            )
        )
        db.add(
            Document(
                id=document_id,
                library_id=library_id,
                title="Cleanup",
                content_hash="b" * 64,
                current_revision=2,
                current_revision_id=replacement_revision_id,
                latest_revision_id=replacement_revision_id,
                status="ready",
            )
        )
        db.add_all(
            [
                DocumentRevision(
                    id=old_revision_id,
                    document_id=document_id,
                    library_id=library_id,
                    revision_no=1,
                    content_hash="a" * 64,
                    parser_name="plain",
                    parser_version="v1",
                    chunking_strategy="fixed",
                    chunking_strategy_version="v1",
                    status="superseded",
                ),
                DocumentRevision(
                    id=replacement_revision_id,
                    document_id=document_id,
                    library_id=library_id,
                    revision_no=2,
                    content_hash="b" * 64,
                    parser_name="plain",
                    parser_version="v1",
                    chunking_strategy="fixed",
                    chunking_strategy_version="v1",
                    status="ready",
                    published_at=now - timedelta(days=61),
                ),
            ]
        )
        db.add(
            DocumentRevisionFile(
                id=revision_file_id,
                document_revision_id=old_revision_id,
                document_id=document_id,
                library_id=library_id,
                file_name="old.txt",
                content_type="text/plain",
                storage_path="libraries/old.txt",
                size_bytes=1,
                sha256="a" * 64,
                storage_provider="local",
                endpoint_ref="primary",
                object_key="libraries/old.txt",
                immutability_mode="content_hash",
                managed_snapshot=True,
                lifecycle_status="available",
            )
        )
        db.add(
            RevisionRetentionRecord(
                id=record_id,
                library_id=library_id,
                document_id=document_id,
                document_revision_id=old_revision_id,
                replacement_revision_id=replacement_revision_id,
                revision_file_id=revision_file_id,
                status="queued",
                retention_days=60,
                notice_days=7,
                replacement_ready_at=now - timedelta(days=61),
                cleanup_eligible_at=now - timedelta(days=61),
                cleanup_not_before=now - timedelta(days=1),
                notice_at=now - timedelta(days=8),
                impact_snapshot={
                    "active_entity_mentions": 0,
                    "active_relation_evidence": 0,
                    "current_publication_items": 0,
                    "dependency_evidence_ids": [],
                },
                impact_hash="c" * 64,
                idempotency_key=f"cleanup:{revision_file_id}",
                available_at=now - timedelta(minutes=1),
            )
        )
        await db.commit()
    return record_id, revision_file_id


async def _claim(Session, record_id, worker_id, config):
    async with Session() as db:
        async with db.begin():
            return await claim_revision_cleanup(
                db,
                record_id=record_id,
                worker_id=worker_id,
                config=config,
            )


def test_concurrent_cleanup_claim_has_one_authoritative_winner(monkeypatch):
    name, url = _create_database(monkeypatch, "vkt_v08_cleanup_claim")
    try:
        command.upgrade(Config("alembic.ini"), "0028")
        engine = create_async_engine(url)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        record_id, revision_file_id = asyncio.run(_seed_cleanup(Session))
        config = Settings(
            _env_file=None,
            revision_cleanup_enabled=True,
            revision_retention_enabled=True,
            revision_file_storage_enabled=True,
        )

        async def run_claims():
            return await asyncio.gather(
                _claim(Session, record_id, "worker-a", config),
                _claim(Session, record_id, "worker-b", config),
            )

        claims = asyncio.run(run_claims())
        assert sum(claim is not None for claim in claims) == 1

        async def state():
            async with Session() as db:
                record = await db.get(RevisionRetentionRecord, record_id)
                revision_file = await db.get(DocumentRevisionFile, revision_file_id)
                return record, revision_file

        record, revision_file = asyncio.run(state())
        assert record.status == "processing"
        assert record.attempt_count == 1
        assert record.claim_token is not None
        assert revision_file.lifecycle_status == "deleting"
        asyncio.run(engine.dispose())
    finally:
        _drop_database(name)
