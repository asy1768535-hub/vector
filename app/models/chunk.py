"""chunks：文档分片元信息。文本和向量同时存在 Qdrant payload；PG 仅留检索回放需要的元数据。"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import ForeignKey, Index, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunks_lib_doc_seq", "library_id", "document_id", "seq"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sys_libraries.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_metadata: Mapped[Optional[dict[str, Any]]] = mapped_column("metadata", JSONB, nullable=True)
