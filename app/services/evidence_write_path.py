from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chunk import Chunk
from app.models.chunk_links import ChunkBlock, ChunkEvidence
from app.models.document import Document
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision
from app.models.embedding_job import EmbeddingJob
from app.models.evidence_unit import EvidenceUnit
from app.models.library import Library


PARSER_NAME = "legacy"
PARSER_VERSION = "v0.2-m2"
CHUNKING_STRATEGY_VERSION = "v0.2-m2"


@dataclass(frozen=True)
class PreparedChunk:
    text: str
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class EvidenceGenerationResult:
    revision: DocumentRevision
    job: EmbeddingJob
    chunks: list[Chunk]
    blocks: list[DocumentBlock]
    evidence_units: list[EvidenceUnit]


def _ensure_id(obj: Any) -> uuid.UUID:
    current = getattr(obj, "id", None)
    if current is None:
        current = uuid.uuid4()
        setattr(obj, "id", current)
    return current


def _quote_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _position(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    if not metadata:
        return None
    location = metadata.get("location")
    if isinstance(location, dict):
        return dict(location)
    return None


async def create_evidence_generation(
    db: AsyncSession,
    *,
    library: Library,
    document: Document,
    normalized_text: str,
    title: str | None,
    document_metadata: dict[str, Any] | None,
    splitter: str,
    created_by: uuid.UUID | None,
    prepared_chunks: list[PreparedChunk],
    job: EmbeddingJob | None = None,
    rebuild_operation_id: uuid.UUID | None = None,
) -> EvidenceGenerationResult:
    document_id = _ensure_id(document)
    revision_id = uuid.uuid4()
    revision_no = int(document.current_revision or 1)

    revision = DocumentRevision(
        id=revision_id,
        document_id=document_id,
        library_id=library.id,
        revision_no=revision_no,
        title=title,
        document_metadata=document_metadata,
        content_hash=document.content_hash,
        normalized_text=normalized_text,
        parser_name=PARSER_NAME,
        parser_version=PARSER_VERSION,
        parser_config=None,
        chunking_strategy=splitter,
        chunking_strategy_version=CHUNKING_STRATEGY_VERSION,
        chunking_config={
            "chunk_size": library.chunk_size,
            "chunk_overlap": library.chunk_overlap,
        },
        visibility_scope=document.visibility_scope,
        security_level=document.security_level,
        status="pending",
        created_by=created_by,
    )
    document.latest_revision_id = revision.id

    job_created = job is None
    if job is None:
        job = EmbeddingJob(
            library_id=library.id,
            document_id=document_id,
            status="pending",
            document_revision=revision_no,
            document_revision_id=revision.id,
            document_revision_no=revision_no,
            rebuild_operation_id=rebuild_operation_id,
        )
    else:
        job.document_revision_id = revision.id
        job.document_revision_no = revision_no

    blocks: list[DocumentBlock] = []
    evidence_units: list[EvidenceUnit] = []
    chunks: list[Chunk] = []
    chunk_blocks: list[ChunkBlock] = []
    chunk_evidence: list[ChunkEvidence] = []

    for seq, prepared in enumerate(prepared_chunks):
        metadata = prepared.metadata or {}
        source_start = _int_or_none(metadata.get("source_start"))
        source_end = _int_or_none(metadata.get("source_end"))
        page_start = _int_or_none(metadata.get("page_start"))
        page_end = _int_or_none(metadata.get("page_end"))
        title_path = metadata.get("title_path")
        if not isinstance(title_path, list):
            title_path = None
        position = _position(metadata)

        block = DocumentBlock(
            id=uuid.uuid4(),
            library_id=library.id,
            document_id=document_id,
            document_revision_id=revision.id,
            seq=seq,
            block_kind="paragraph",
            title_path=title_path,
            page_start=page_start,
            page_end=page_end,
            source_start=source_start,
            source_end=source_end,
            text=prepared.text,
            position=position,
            parser_name=PARSER_NAME,
            parser_version=PARSER_VERSION,
        )
        evidence = EvidenceUnit(
            id=uuid.uuid4(),
            library_id=library.id,
            document_id=document_id,
            document_revision_id=revision.id,
            document_block_id=block.id,
            evidence_kind="chunk",
            source_start=source_start,
            source_end=source_end,
            page_start=page_start,
            page_end=page_end,
            title_path=title_path,
            position=position,
            text_quote=prepared.text,
            text_quote_hash=_quote_hash(prepared.text),
            evidence_metadata=None,
            visibility_scope=document.visibility_scope,
            security_level=document.security_level,
            status="active",
        )
        chunk = Chunk(
            id=uuid.uuid4(),
            document_id=document_id,
            library_id=library.id,
            document_revision_id=revision.id,
            block_id=block.id,
            evidence_id=evidence.id,
            seq=seq,
            chunk_kind="text",
            text=prepared.text,
            token_count=len(prepared.text),
            page_start=page_start,
            page_end=page_end,
            title_path=title_path,
            source_start=source_start,
            source_end=source_end,
            position=position,
            chunk_metadata=prepared.metadata,
        )
        blocks.append(block)
        evidence_units.append(evidence)
        chunks.append(chunk)
        chunk_blocks.append(
            ChunkBlock(
                chunk_id=chunk.id,
                document_block_id=block.id,
                document_revision_id=revision.id,
                seq=seq,
                source_start=source_start,
                source_end=source_end,
            )
        )
        chunk_evidence.append(
            ChunkEvidence(
                chunk_id=chunk.id,
                evidence_id=evidence.id,
                document_revision_id=revision.id,
                seq=seq,
            )
        )

    try:
        db.add(revision)
        if job_created:
            db.add(job)
        db.add_all([*blocks, *evidence_units, *chunks, *chunk_blocks, *chunk_evidence])
        await db.flush()
    except Exception as exc:
        revision.status = "failed"
        revision.last_error = str(exc)
        if document.current_revision_id is None:
            document.status = "failed"
        raise ValueError(str(exc)) from exc
    return EvidenceGenerationResult(
        revision=revision,
        job=job,
        chunks=chunks,
        blocks=blocks,
        evidence_units=evidence_units,
    )
