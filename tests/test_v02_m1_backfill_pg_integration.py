from __future__ import annotations

import asyncio
import os
import uuid
from urllib.parse import urlparse

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.config import settings
from app.db import Base
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision
from app.models.evidence_unit import EvidenceUnit
from app.models.library import Library
from app.services.evidence_backfill import backfill_v02_m1_batch


_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL database to run M1 migration tests.",
)


def _parts():
    parsed = urlparse(_DSN.replace("+asyncpg", ""))
    return parsed.hostname, parsed.port or 5432, parsed.username, parsed.password


async def _admin(host: str, port: int, user: str, password: str, sql: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(host=host, port=port, user=user, password=password, database="postgres")
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def _version_and_tables(dsn: str):
    eng = create_async_engine(dsn)
    try:
        async with eng.connect() as conn:
            version = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
            tables = {}
            for table_name in (
                "folders",
                "sync_sources",
                "document_revisions",
                "document_revision_files",
                "document_blocks",
                "evidence_units",
                "chunk_blocks",
                "chunk_evidence",
                "migration_backfill_state",
            ):
                tables[table_name] = (
                    await conn.execute(text(f"SELECT to_regclass('public.{table_name}')"))
                ).scalar()
        return version, tables
    finally:
        await eng.dispose()


def test_alembic_upgrade_0018_and_downgrade_0017(monkeypatch):
    host, port, user, password = _parts()
    name = "vkt_v02_m1_" + uuid.uuid4().hex[:8]
    asyncio.run(_admin(host, port, user, password, f'DROP DATABASE IF EXISTS "{name}"'))
    asyncio.run(_admin(host, port, user, password, f'CREATE DATABASE "{name}"'))

    monkeypatch.setattr(settings, "db_host", host)
    monkeypatch.setattr(settings, "db_port", int(port))
    monkeypatch.setattr(settings, "db_user", user)
    monkeypatch.setattr(settings, "db_password", password)
    monkeypatch.setattr(settings, "db_name", name)
    dsn = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}"

    try:
        command.upgrade(Config("alembic.ini"), "0018")
        version, tables = asyncio.run(_version_and_tables(dsn))
        assert version == "0018"
        assert all(value is not None for value in tables.values())

        command.downgrade(Config("alembic.ini"), "0017")
        version, _ = asyncio.run(_version_and_tables(dsn))
        assert version == "0017"
    finally:
        asyncio.run(_admin(host, port, user, password, f'DROP DATABASE IF EXISTS "{name}"'))


def test_backfill_batch_creates_revision_evidence_and_chunk_links_idempotently():
    async def run():
        eng = create_async_engine(_DSN)
        Session = async_sessionmaker(eng, expire_on_commit=False)
        try:
            async with eng.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)

            async with Session() as db:
                lib = Library(
                    slug="v02_m1_" + uuid.uuid4().hex[:8],
                    name="M1",
                    qdrant_collection="v02_m1",
                    embedding_model="m",
                    embedding_dim=8,
                    chunk_size=1000,
                    chunk_overlap=120,
                    lifecycle_mode="managed",
                    index_state="ready",
                )
                db.add(lib)
                await db.flush()
                doc = Document(
                    library_id=lib.id,
                    title="Legacy Doc",
                    external_id="legacy-1",
                    content_hash="b" * 64,
                    current_revision=2,
                    status="ready",
                )
                db.add(doc)
                await db.flush()
                db.add_all(
                    [
                        Chunk(
                            library_id=lib.id,
                            document_id=doc.id,
                            seq=0,
                            text="first chunk",
                            token_count=2,
                        ),
                        Chunk(
                            library_id=lib.id,
                            document_id=doc.id,
                            seq=1,
                            text="second chunk",
                            token_count=2,
                        ),
                    ]
                )
                await db.commit()
                doc_id = doc.id

            async with Session() as db:
                result = await backfill_v02_m1_batch(db, batch_size=10)
                assert result.processed == 1
                assert result.failed == 0

            async with Session() as db:
                second = await backfill_v02_m1_batch(db, batch_size=10)
                assert second.processed == 0
                assert second.exhausted is True

            async with Session() as db:
                doc = await db.get(Document, doc_id)
                assert doc.current_revision_id is not None
                assert doc.latest_revision_id == doc.current_revision_id

                revision_count = (
                    await db.execute(
                        select(func.count()).select_from(DocumentRevision).where(
                            DocumentRevision.document_id == doc_id
                        )
                    )
                ).scalar_one()
                block_count = (
                    await db.execute(
                        select(func.count()).select_from(DocumentBlock).where(
                            DocumentBlock.document_id == doc_id
                        )
                    )
                ).scalar_one()
                evidence_count = (
                    await db.execute(
                        select(func.count()).select_from(EvidenceUnit).where(
                            EvidenceUnit.document_id == doc_id
                        )
                    )
                ).scalar_one()
                chunks = (
                    await db.execute(select(Chunk).where(Chunk.document_id == doc_id).order_by(Chunk.seq))
                ).scalars().all()

                assert revision_count == 1
                assert block_count == 2
                assert evidence_count == 2
                assert all(chunk.document_revision_id == doc.current_revision_id for chunk in chunks)
                assert all(chunk.block_id is not None for chunk in chunks)
                assert all(chunk.evidence_id is not None for chunk in chunks)
                assert all(chunk.chunk_kind == "text" for chunk in chunks)
        finally:
            await eng.dispose()

    asyncio.run(run())
