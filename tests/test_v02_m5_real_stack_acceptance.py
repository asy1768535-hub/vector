from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
import app.services.retrieval as retrieval_service
from app.config import settings
from app.db import Base
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.evidence_unit import EvidenceUnit
from app.models.library import Library
from app.schemas.dify import DifyRetrievalRequest
from app.services import qdrant
from app.services.retrieval import run_retrieval


_PG_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
_QDRANT_URL = os.getenv("VECTOR_KB_QDRANT_TEST_URL")

pytestmark = pytest.mark.skipif(
    not (_PG_DSN and _QDRANT_URL),
    reason=(
        "Set VECTOR_KB_PG_TEST_DSN and VECTOR_KB_QDRANT_TEST_URL to disposable "
        "PostgreSQL/Qdrant services to run the v0.2 real stack acceptance smoke test."
    ),
)


async def _run_real_stack_acceptance(monkeypatch) -> None:
    monkeypatch.setattr(settings, "qdrant_url", _QDRANT_URL.rstrip("/"))
    monkeypatch.setattr(settings, "qdrant_api_key", os.getenv("VECTOR_KB_QDRANT_API_KEY") or "")
    monkeypatch.setattr(settings, "retrieval_consistency_filter", True)
    monkeypatch.setattr(settings, "enable_revision_id_visibility", True)
    monkeypatch.setattr(settings, "query_rewrite_enabled", False)
    monkeypatch.setattr(settings, "query_rewrite_llm_enabled", False)
    monkeypatch.setattr(settings, "rerank_enabled", False)

    async def _embed_one(*args, **kwargs):
        return [1.0, 0.0, 0.0]

    monkeypatch.setattr(retrieval_service.embedding, "embed_one", _embed_one)

    engine = create_async_engine(_PG_DSN)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    collection = "v02_m5_" + uuid.uuid4().hex
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        await qdrant.ensure_collection(collection, dim=3, quantization_type=None)

        lib = Library(
            slug="v02-m5-" + uuid.uuid4().hex[:8],
            name="v0.2 M5 acceptance",
            qdrant_collection=collection,
            embedding_model="m5-test",
            embedding_dim=3,
            chunk_size=1000,
            chunk_overlap=120,
            lifecycle_mode="managed",
            index_state="ready",
        )
        body = "m5 evidence acceptance body"
        async with Session() as db:
            db.add(lib)
            await db.flush()
            doc = Document(
                library_id=lib.id,
                title="M5 Evidence Doc",
                external_id="m5-evidence",
                content_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                current_revision=1,
                status="ready",
            )
            db.add(doc)
            await db.flush()
            revision = DocumentRevision(
                id=uuid.uuid4(),
                document_id=doc.id,
                library_id=lib.id,
                revision_no=1,
                title=doc.title,
                document_metadata=None,
                content_hash=doc.content_hash,
                normalized_text=body,
                parser_name="m5-acceptance",
                parser_version="v0.2",
                parser_config=None,
                chunking_strategy="manual",
                chunking_strategy_version="v0.2",
                chunking_config=None,
                visibility_scope=None,
                security_level=None,
                status="ready",
                published_at=datetime.now(timezone.utc),
                created_by=None,
            )
            evidence = EvidenceUnit(
                id=uuid.uuid4(),
                library_id=lib.id,
                document_id=doc.id,
                document_revision_id=revision.id,
                document_block_id=None,
                evidence_kind="chunk",
                source_start=0,
                source_end=len(body),
                text_quote=body,
                text_quote_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                status="active",
            )
            chunk = Chunk(
                id=uuid.uuid4(),
                library_id=lib.id,
                document_id=doc.id,
                document_revision_id=revision.id,
                evidence_id=evidence.id,
                seq=0,
                chunk_kind="text",
                text=body,
                token_count=len(body),
                source_start=0,
                source_end=len(body),
            )
            doc.current_revision_id = revision.id
            doc.latest_revision_id = revision.id
            db.add_all([revision, evidence, chunk])
            await db.commit()
            lib_id = lib.id
            doc_id = doc.id
            revision_id = revision.id
            evidence_id = evidence.id
            chunk_id = chunk.id

        await qdrant.upsert_points(
            collection,
            [
                {
                    "id": str(chunk_id),
                    "vector": [1.0, 0.0, 0.0],
                    "payload": {
                        "library_id": str(lib_id),
                        "document_id": str(doc_id),
                        "chunk_id": str(chunk_id),
                        "seq": 0,
                        "text": body,
                        "title": "M5 Evidence Doc",
                        "document_revision": 1,
                        "document_revision_id": str(revision_id),
                        "evidence_id": str(evidence_id),
                    },
                }
            ],
        )

        request = DifyRetrievalRequest.model_validate(
            {"knowledge_id": lib.slug, "query": "m5 evidence", "retrieval_setting": {"top_k": 1}}
        )
        async with Session() as db:
            visible = await run_retrieval(
                collection=collection,
                embedding_model=lib.embedding_model,
                embedding_base_url=None,
                request=request,
                db=db,
                library=lib,
            )

        assert len(visible.records) == 1
        metadata = visible.records[0].metadata
        assert metadata["evidence_id"] == str(evidence_id)
        assert metadata["document_revision_id"] == str(revision_id)

        raw = await qdrant.search(collection, [1.0, 0.0, 0.0], limit=1)
        assert raw

        async with Session() as db:
            persisted = await db.get(Document, doc_id)
            persisted.deleted_at = datetime.now(timezone.utc)
            await db.commit()

        async with Session() as db:
            hidden = await run_retrieval(
                collection=collection,
                embedding_model=lib.embedding_model,
                embedding_base_url=None,
                request=request,
                db=db,
                library=lib,
            )
        assert hidden.records == []
    finally:
        await qdrant.delete_collection(collection)
        await engine.dispose()


def test_real_postgresql_qdrant_retrieval_trace_and_tombstone_acceptance(monkeypatch):
    asyncio.run(_run_real_stack_acceptance(monkeypatch))
