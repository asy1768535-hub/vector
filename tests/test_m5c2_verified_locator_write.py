from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.config import settings
from app.db import Base
from app.models.chunk import Chunk
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision
from app.models.extraction_context_snapshot import ExtractionContextSnapshot
from app.models.evidence_unit import EvidenceUnit
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.organization import Organization
from app.schemas.evidence_locator import EvidenceLocatorV1, sha256_text
from app.services.graph_claim_shadow_worker import _load_verified_evidence
from app.services import import_parsing, ingest


LIBRARY_ID = uuid.UUID("00000000-0000-0000-0000-00000000c2aa")
FILE_ID = uuid.UUID("00000000-0000-0000-0000-00000000c2bb")


def _library() -> Library:
    return Library(
        id=LIBRARY_ID,
        slug="m5c2-fixture",
        name="M5C2 fixture",
        qdrant_collection="m5c2_fixture",
        embedding_model="fixture",
        embedding_dim=8,
        chunk_size=1000,
        chunk_overlap=0,
    )


def _db(added: list[object], *, fail_flush: bool = False):
    from unittest.mock import AsyncMock, MagicMock

    result = MagicMock()
    result.scalars.return_value.first.return_value = None
    result.scalar_one.return_value = 0
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.add = MagicMock(side_effect=added.append)
    db.add_all = MagicMock(side_effect=lambda values: added.extend(values))
    db.flush = AsyncMock(side_effect=RuntimeError("fixture flush failure") if fail_flush else None)
    return db


def _locators(added: list[object]) -> tuple[dict, dict, dict]:
    block = next(row for row in added if isinstance(row, DocumentBlock))
    chunk = next(row for row in added if isinstance(row, Chunk))
    evidence = next(row for row in added if isinstance(row, EvidenceUnit))
    return (
        block.content["evidence_locator_v1"],
        chunk.chunk_metadata["evidence_locator_v1"],
        evidence.evidence_metadata["evidence_locator_v1"],
    )


def test_non_segmented_evidence_write_populates_verified_locators(monkeypatch):
    monkeypatch.setattr(settings, "enable_evidence_write_path", True)
    text = "asset legal medical synthetic relation"
    added: list[object] = []

    asyncio.run(
        ingest.ingest_text(
            db=_db(added),
            library=_library(),
            text=text,
            title="synthetic.txt",
            external_id="m5c2-direct",
            metadata=None,
            splitter="text",
            created_by=None,
            chunks=[{"text": text, "source_start": 0, "source_end": len(text)}],
            file_name="synthetic.txt",
            raw_file_sha256=hashlib.sha256(b"synthetic.txt").hexdigest(),
            document_revision_file_id=FILE_ID,
        )
    )

    block_locator, chunk_locator, evidence_locator = _locators(added)
    block = next(row for row in added if isinstance(row, DocumentBlock))
    chunk = next(row for row in added if isinstance(row, Chunk))
    evidence = next(row for row in added if isinstance(row, EvidenceUnit))
    for locator in (block_locator, chunk_locator, evidence_locator):
        parsed = EvidenceLocatorV1.model_validate(locator)
        assert parsed.provenance_status == "verified"
        assert parsed.document_revision_file_id == FILE_ID
        assert parsed.unit_text_sha256 == sha256_text(text)
        assert parsed.quote_sha256 == sha256_text(text)
        assert parsed.source.kind == "text"
    assert block_locator["unit_id"] == str(block.id)
    assert chunk_locator["unit_id"] == str(chunk.id)
    assert evidence_locator["unit_id"] == str(evidence.id)
    assert chunk_locator["parent_unit_id"] == str(block.id)
    assert evidence_locator["parent_unit_id"] == str(block.id)


@pytest.mark.parametrize(
    ("suffix", "text"),
    [
        (".txt", "asset synthetic document"),
        (".md", "legal synthetic document"),
        (".txt", "medical synthetic document"),
    ],
)
def test_supported_text_imports_keep_verified_locator_through_ingest(
    monkeypatch, tmp_path, suffix, text
):
    monkeypatch.setattr(settings, "enable_evidence_write_path", True)
    path = tmp_path / f"fixture{suffix}"
    path.write_text(text, encoding="utf-8")
    parsed = import_parsing.parse_import_file(path, _library())
    added: list[object] = []

    asyncio.run(
        ingest.ingest_text(
            db=_db(added),
            library=_library(),
            text=parsed.normalized_text,
            title=path.name,
            external_id=None,
            metadata=None,
            splitter=parsed.splitter_name,
            created_by=None,
            chunks=parsed.chunks,
            segments=parsed.segments,
            file_name=path.name,
            raw_file_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            document_revision_file_id=FILE_ID,
        )
    )

    assert parsed.segments
    block_locator, chunk_locator, evidence_locator = _locators(added)
    for locator in (block_locator, chunk_locator, evidence_locator):
        assert EvidenceLocatorV1.model_validate(locator).provenance_status == "verified"


def test_legacy_flag_off_does_not_fabricate_locator(monkeypatch):
    monkeypatch.setattr(settings, "enable_evidence_write_path", False)
    added: list[object] = []

    asyncio.run(
        ingest.ingest_text(
            db=_db(added),
            library=_library(),
            text="legacy text",
            title="legacy.txt",
            external_id="legacy",
            metadata=None,
            splitter="text",
            created_by=None,
            chunks=["legacy text"],
            file_name="legacy.txt",
            raw_file_sha256="a" * 64,
            document_revision_file_id=FILE_ID,
        )
    )

    assert not [row for row in added if isinstance(row, (DocumentBlock, EvidenceUnit))]
    chunks = [row for row in added if isinstance(row, Chunk)]
    assert len(chunks) == 1
    assert "evidence_locator_v1" not in (chunks[0].chunk_metadata or {})


def test_invalid_non_segmented_source_span_has_no_write_before_flush(monkeypatch):
    monkeypatch.setattr(settings, "enable_evidence_write_path", True)
    added: list[object] = []

    with pytest.raises(ValueError, match="source range"):
        asyncio.run(
            ingest.ingest_text(
                db=_db(added),
                library=_library(),
                text="actual",
                title="bad.txt",
                external_id=None,
                metadata=None,
                splitter="text",
                created_by=None,
                chunks=[{"text": "wrong", "source_start": 0, "source_end": 6}],
            )
        )

    assert added == []


@pytest.mark.skipif(
    not os.getenv("VECTOR_KB_PG_TEST_DSN"),
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL service",
)
def test_pg_ingest_to_verified_shadow_evidence_adapter(monkeypatch):
    monkeypatch.setattr(settings, "enable_evidence_write_path", True)
    monkeypatch.setattr(settings, "enable_evidence_locator_read", True)
    asyncio.run(_run_pg_ingest_to_shadow_adapter())


async def _run_pg_ingest_to_shadow_adapter() -> None:
    dsn = os.environ["VECTOR_KB_PG_TEST_DSN"]
    engine = create_async_engine(dsn, pool_size=2, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    ids = {name: uuid.uuid4() for name in (
        "organization", "library", "document", "revision", "ontology", "job", "unit", "snapshot"
    )}
    text = "source supports target"
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            db.add(Organization(
                id=ids["organization"],
                slug="m5c2-pg-" + uuid.uuid4().hex[:10],
                name="M5C2 PG organization",
                deployment_profile="private",
                status="active",
            ))
            await db.flush()
            db.add(Library(
                id=ids["library"],
                organization_id=ids["organization"],
                slug="m5c2-pg-" + uuid.uuid4().hex[:10],
                name="M5C2 PG library",
                embedding_model="fixture",
                embedding_dim=3,
                qdrant_collection="m5c2-pg-unused",
                lifecycle_mode="managed",
                index_state="ready",
            ))
            await db.flush()
            document, _embedding_job, _count, _existing = await ingest.ingest_text(
                db=db,
                library=await db.get(Library, ids["library"]),
                text=text,
                title="m5c2.txt",
                external_id="m5c2-pg-document",
                metadata=None,
                splitter="text",
                created_by=None,
                chunks=[{"text": text, "source_start": 0, "source_end": len(text)}],
            )
            await db.commit()

        async with sessions() as db:
            revision = (
                await db.execute(
                    select(DocumentRevision).where(
                        DocumentRevision.document_id == document.id
                    )
                )
            ).scalars().one()
            chunk = (
                await db.execute(select(Chunk).where(Chunk.document_revision_id == revision.id))
            ).scalars().one()
            evidence = (
                await db.execute(
                    select(EvidenceUnit).where(EvidenceUnit.document_revision_id == revision.id)
                )
            ).scalars().one()
            db.add(OntologyVersion(
                id=ids["ontology"],
                library_id=ids["library"],
                version_key="m5c2-pg-ontology-" + uuid.uuid4().hex[:8],
                version_no=1,
                status="active",
                origin="user",
                confirmed=True,
            ))
            await db.flush()
            job = GraphExtractionJob(
                id=ids["job"],
                library_id=ids["library"],
                document_id=document.id,
                document_revision_id=revision.id,
                ontology_version_id=ids["ontology"],
                trigger_type="eval",
                execution_mode="eval",
                status="succeeded",
                input_fingerprint="a" * 64,
                idempotency_key="m5c2-pg-job-" + uuid.uuid4().hex,
                model_provider="fixture",
                model_name="fixture",
                prompt_version="canonical-v1",
                extractor_version="canonical-v1",
                output_parser_version="canonical-v1",
                context_policy_version="v1",
                extraction_policy_version="v1",
                normalization_rule_version="v1",
                confidence_policy_version="v1",
                document_parser_version="v1",
                chunking_strategy_version="v1",
                model_config_snapshot={},
                policy_config_snapshot={},
                model_config_hash="b" * 64,
                policy_config_hash="c" * 64,
                ontology_snapshot={},
                ontology_snapshot_hash="d" * 64,
                prompt_content_hash="e" * 64,
            )
            db.add(job)
            await db.flush()
            unit = GraphExtractionUnit(
                id=ids["unit"],
                job_id=job.id,
                library_id=ids["library"],
                document_revision_id=revision.id,
                ordinal=0,
                center_chunk_id=chunk.id,
                center_evidence_id=evidence.id,
                unit_fingerprint="f" * 64,
                status="succeeded",
            )
            db.add(unit)
            await db.flush()
            snapshot_json = {"chunks": [{"context_ref": "c0", "text": text}]}
            snapshot = ExtractionContextSnapshot(
                id=ids["snapshot"],
                job_id=job.id,
                extraction_unit_id=unit.id,
                center_chunk_id=chunk.id,
                center_evidence_id=evidence.id,
                previous_chunk_ids=[],
                next_chunk_ids=[],
                block_ids=[],
                title_path_source="none",
                ontology_snapshot_hash="d" * 64,
                context_mapping={
                    "c0": {
                        "chunk_id": str(chunk.id),
                        "primary_evidence_id": str(evidence.id),
                        "evidence_ids": [str(evidence.id)],
                        "role": "current",
                    }
                },
                context_policy_version="v1",
                context_hash=sha256_text(text),
                context_char_count=len(json.dumps(snapshot_json)),
                context_json=snapshot_json,
                context_text=json.dumps(snapshot_json),
            )
            db.add(snapshot)
            await db.flush()
            contexts, references = await _load_verified_evidence(
                db,
                job=job,
                unit=unit,
                snapshot=snapshot,
                document=document,
                revision=revision,
            )
            assert [context.ref_key for context in contexts] == ["c0"]
            assert references["c0"].locator is not None
            assert EvidenceLocatorV1.model_validate(
                chunk.chunk_metadata["evidence_locator_v1"]
            ).provenance_status == "verified"
            assert references["c0"].quote_sha256 == sha256_text(text)
            await db.rollback()
    finally:
        await engine.dispose()
