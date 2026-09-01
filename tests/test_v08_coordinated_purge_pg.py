from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import null, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.config import Settings, settings
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.document_revision_file import DocumentRevisionFile
from app.models.graph_publication import GraphPublication
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.revision_purge_operation import RevisionPurgeOperation
from app.models.revision_retention import RevisionRetentionRecord
from app.services.revision_cleanup import (
    claim_revision_cleanup,
    queue_eligible_revision_cleanups,
)
from app.services.revision_coordinated_purge import (
    claim_coordinated_purge_operation,
    finalize_coordinated_purge_activation,
)


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


def _runtime_config() -> Settings:
    return Settings(
        _env_file=None,
        graph_publication_enabled=True,
        revision_retention_enabled=True,
        revision_file_storage_enabled=True,
        revision_cleanup_enabled=True,
        revision_coordinated_purge_enabled=True,
    )


def test_0029_upgrades_and_downgrades_exactly(monkeypatch):
    name, _ = _create_database(monkeypatch, "vkt_v08_coordinated_migration")
    try:
        config = Config("alembic.ini")
        command.upgrade(config, "0028")
        command.upgrade(config, "0029")
        assert asyncio.run(
            _execute(
                name,
                "SELECT to_regclass('public.revision_purge_operations')",
            )
        )[0][0] == "revision_purge_operations"
        source_constraint = asyncio.run(
            _execute(
                name,
                """
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conname = 'ck_graph_publications_source_mode'
                """,
            )
        )[0][0]
        assert "coordinated_purge" in source_constraint
        idempotency_columns = asyncio.run(
            _execute(
                name,
                """
                SELECT array_agg(att.attname ORDER BY key.ordinality)
                FROM pg_constraint con
                CROSS JOIN LATERAL unnest(con.conkey)
                    WITH ORDINALITY AS key(attnum, ordinality)
                JOIN pg_attribute att
                  ON att.attrelid = con.conrelid AND att.attnum = key.attnum
                WHERE con.conname = 'uq_revision_purge_operations_idempotency'
                GROUP BY con.oid
                """,
            )
        )[0][0]
        assert idempotency_columns == ["library_id", "idempotency_key"]

        command.downgrade(config, "0028")
        assert asyncio.run(
            _execute(
                name,
                "SELECT to_regclass('public.revision_purge_operations')",
            )
        )[0][0] is None
        source_constraint = asyncio.run(
            _execute(
                name,
                """
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conname = 'ck_graph_publications_source_mode'
                """,
            )
        )[0][0]
        assert "coordinated_purge" not in source_constraint
    finally:
        _drop_database(name)


async def _seed_operation(Session, *, activated: bool, expired: bool = False):
    now = datetime.now(timezone.utc)
    library_id = uuid.uuid4()
    ontology_id = uuid.uuid4()
    document_id = uuid.uuid4()
    old_revision_id = uuid.uuid4()
    replacement_revision_id = uuid.uuid4()
    revision_file_id = uuid.uuid4()
    retention_id = uuid.uuid4()
    source_publication_id = uuid.uuid4()
    replacement_publication_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    source_manifest = "a" * 64
    replacement_manifest = "b" * 64
    confirmation_hash = "c" * 64
    impact_hash = "d" * 64
    target_evidence_hash = "e" * 64

    async with Session() as db:
        db.add(
            Library(
                id=library_id,
                slug=f"coordinated-{library_id.hex[:8]}",
                name="Coordinated Purge",
                embedding_model="bge-m3",
                embedding_dim=1024,
                qdrant_collection=f"coordinated_{library_id.hex[:8]}",
                revision_retention_enabled=True,
            )
        )
        await db.flush()
        db.add(
            OntologyVersion(
                id=ontology_id,
                library_id=library_id,
                version_key="v1",
                version_no=1,
                status="active",
            )
        )
        db.add(
            Document(
                id=document_id,
                library_id=library_id,
                title="Coordinated Purge",
                content_hash="2" * 64,
                current_revision=2,
                current_revision_id=replacement_revision_id,
                latest_revision_id=replacement_revision_id,
                status="ready",
            )
        )
        await db.flush()
        db.add_all(
            [
                DocumentRevision(
                    id=old_revision_id,
                    document_id=document_id,
                    library_id=library_id,
                    revision_no=1,
                    content_hash="1" * 64,
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
                    content_hash="2" * 64,
                    parser_name="plain",
                    parser_version="v1",
                    chunking_strategy="fixed",
                    chunking_strategy_version="v1",
                    status="ready",
                    published_at=now - timedelta(days=61),
                ),
            ]
        )
        await db.flush()
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
                sha256="1" * 64,
                storage_provider="local",
                endpoint_ref="primary",
                object_key="libraries/old.txt",
                immutability_mode="content_hash",
                managed_snapshot=True,
                lifecycle_status="available",
            )
        )
        await db.flush()
        db.add(
            RevisionRetentionRecord(
                id=retention_id,
                library_id=library_id,
                document_id=document_id,
                document_revision_id=old_revision_id,
                replacement_revision_id=replacement_revision_id,
                revision_file_id=revision_file_id,
                status="blocked",
                block_code="active_graph_dependency",
                retention_days=60,
                notice_days=7,
                replacement_ready_at=now - timedelta(days=61),
                cleanup_eligible_at=now - timedelta(days=1),
                cleanup_not_before=now - timedelta(hours=1),
                notice_at=now - timedelta(days=8),
                impact_snapshot={
                    "active_entity_mentions": 0,
                    "active_relation_evidence": 0,
                    "current_publication_items": 1,
                    "dependency_evidence_ids": [],
                },
                impact_hash="f" * 64,
                idempotency_key=f"retention:{revision_file_id}",
            )
        )
        await db.flush()

        source = GraphPublication(
            id=source_publication_id,
            library_id=library_id,
            ontology_version_id=ontology_id,
            status="superseded" if activated else "active",
            source_mode="manual_plan",
            manifest_hash=source_manifest,
            idempotency_key=f"source:{source_publication_id}",
            superseded_at=now if activated else None,
        )
        db.add(source)
        await db.flush()
        replacement = GraphPublication(
            id=replacement_publication_id,
            library_id=library_id,
            ontology_version_id=ontology_id,
            status="active" if activated else "planned",
            source_mode="coordinated_purge",
            manifest_hash=replacement_manifest,
            idempotency_key=f"replacement:{replacement_publication_id}",
            parent_publication_id=source_publication_id,
            plan_options={
                "retention_record_id": str(retention_id),
                "confirmation_hash": confirmation_hash,
                "target_evidence_hash": target_evidence_hash,
            },
            activated_at=now if activated else None,
        )
        db.add(replacement)
        await db.flush()
        if activated:
            source.superseded_by_publication_id = replacement_publication_id
        db.add(
            RevisionPurgeOperation(
                id=operation_id,
                library_id=library_id,
                retention_record_id=retention_id,
                revision_file_id=revision_file_id,
                document_id=document_id,
                document_revision_id=old_revision_id,
                source_publication_id=source_publication_id,
                replacement_publication_id=replacement_publication_id,
                status="processing" if expired else "planned",
                idempotency_key=f"purge:{operation_id}",
                confirmation_hash=confirmation_hash,
                impact_snapshot={"target_evidence_hash": target_evidence_hash},
                impact_hash=impact_hash,
                source_manifest_hash=source_manifest,
                replacement_manifest_hash=replacement_manifest,
                attempt_count=1 if expired else 0,
                available_at=null() if expired else now - timedelta(minutes=1),
                worker_id="interrupted-worker" if expired else None,
                claim_token=uuid.uuid4() if expired else None,
                claimed_at=now - timedelta(minutes=10) if expired else None,
                lease_expires_at=now - timedelta(minutes=5) if expired else None,
            )
        )
        await db.commit()
    return operation_id, retention_id


async def _claim_purge(Session, operation_id, worker_id, config):
    async with Session() as db:
        async with db.begin():
            return await claim_coordinated_purge_operation(
                db,
                operation_id=operation_id,
                worker_id=worker_id,
                config=config,
            )


def test_concurrent_coordinated_claim_has_one_authoritative_winner(monkeypatch):
    name, url = _create_database(monkeypatch, "vkt_v08_coordinated_claim")
    try:
        command.upgrade(Config("alembic.ini"), "head")
        async def scenario():
            engine = create_async_engine(url)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            try:
                operation_id, _ = await _seed_operation(Session, activated=False)
                config = _runtime_config()
                claims = await asyncio.gather(
                    _claim_purge(Session, operation_id, "worker-a", config),
                    _claim_purge(Session, operation_id, "worker-b", config),
                )
                assert sum(claim is not None for claim in claims) == 1

                async with Session() as db:
                    operation = await db.get(RevisionPurgeOperation, operation_id)
                assert operation.status == "processing"
                assert operation.attempt_count == 1
                assert operation.claim_token is not None
            finally:
                await engine.dispose()

        asyncio.run(scenario())
    finally:
        _drop_database(name)


def test_active_replacement_recovers_before_physical_cleanup(monkeypatch):
    name, url = _create_database(monkeypatch, "vkt_v08_coordinated_recovery")
    try:
        command.upgrade(Config("alembic.ini"), "head")
        async def scenario():
            engine = create_async_engine(url)
            Session = async_sessionmaker(engine, expire_on_commit=False)
            try:
                operation_id, retention_id = await _seed_operation(
                    Session,
                    activated=True,
                    expired=True,
                )
                config = _runtime_config()

                async with Session() as db:
                    async with db.begin():
                        before_release = await queue_eligible_revision_cleanups(
                            db, config=config
                        )
                assert before_release == ()

                claim = await _claim_purge(
                    Session,
                    operation_id,
                    "recovery-worker",
                    config,
                )
                assert claim is not None
                assert claim.attempt_count == 2

                async with Session() as db:
                    async with db.begin():
                        result = await finalize_coordinated_purge_activation(
                            db,
                            claim=claim,
                            config=config,
                        )
                assert result.status == "cleanup_pending"

                async with Session() as db:
                    async with db.begin():
                        queued = await queue_eligible_revision_cleanups(
                            db, config=config
                        )
                async with Session() as db:
                    async with db.begin():
                        cleanup_claim = await claim_revision_cleanup(
                            db,
                            record_id=retention_id,
                            worker_id="cleanup-worker",
                            config=config,
                        )
                assert queued == (retention_id,)
                assert cleanup_claim is not None

                async with Session() as db:
                    operation = await db.get(RevisionPurgeOperation, operation_id)
                assert operation.status == "cleanup_pending"
            finally:
                await engine.dispose()

        asyncio.run(scenario())
    finally:
        _drop_database(name)
