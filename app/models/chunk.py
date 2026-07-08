"""chunks：文档分片元信息。文本和向量同时存在 Qdrant payload；PG 仅留检索回放需要的元数据。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunks_lib_doc_seq", "library_id", "document_id", "seq"),
        Index("ix_chunks_revision_seq", "document_revision_id", "seq"),
        Index("ix_chunks_evidence_id", "evidence_id"),
        Index("ix_chunks_block_id", "block_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sys_libraries.id", ondelete="CASCADE"), nullable=False
    )
    document_revision_id: Mapped[Optional[uuid.UUID]] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    block_id: Mapped[Optional[uuid.UUID]] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    evidence_id: Mapped[Optional[uuid.UUID]] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_kind: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    page_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    page_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    title_path: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)
    source_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    position: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    chunk_metadata: Mapped[Optional[dict[str, Any]]] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
