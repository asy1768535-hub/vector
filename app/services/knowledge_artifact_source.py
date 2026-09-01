from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.models.chunk import Chunk
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision


async def load_artifact_source_text(db, *, revision: DocumentRevision) -> str:
    if isinstance(revision.normalized_text, str) and revision.normalized_text.strip():
        return revision.normalized_text
    result = await db.execute(
        select(Chunk.text)
        .where(
            Chunk.library_id == revision.library_id,
            Chunk.document_id == revision.document_id,
            Chunk.document_revision_id == revision.id,
        )
        .order_by(Chunk.seq.asc(), Chunk.id.asc())
    )
    return "\n".join(value for value in result.scalars().all() if value)


async def load_artifact_title_paths(
    db, *, revision: DocumentRevision
) -> tuple[list[Any], ...]:
    block_result = await db.execute(
        select(DocumentBlock.title_path)
        .where(
            DocumentBlock.library_id == revision.library_id,
            DocumentBlock.document_id == revision.document_id,
            DocumentBlock.document_revision_id == revision.id,
            DocumentBlock.title_path.is_not(None),
        )
        .order_by(DocumentBlock.seq.asc(), DocumentBlock.id.asc())
    )
    paths = [value for value in block_result.scalars().all() if isinstance(value, list)]
    if paths:
        return tuple(paths)
    chunk_result = await db.execute(
        select(Chunk.title_path)
        .where(
            Chunk.library_id == revision.library_id,
            Chunk.document_id == revision.document_id,
            Chunk.document_revision_id == revision.id,
            Chunk.title_path.is_not(None),
        )
        .order_by(Chunk.seq.asc(), Chunk.id.asc())
    )
    return tuple(
        value for value in chunk_result.scalars().all() if isinstance(value, list)
    )
