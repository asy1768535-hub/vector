from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import Index, Integer
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ChunkBlock(Base):
    __tablename__ = "chunk_blocks"
    __table_args__ = (
        Index("ix_chunk_blocks_revision", "document_revision_id"),
        Index("ix_chunk_blocks_block", "document_block_id"),
    )

    chunk_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    document_block_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    document_revision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    source_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class ChunkEvidence(Base):
    __tablename__ = "chunk_evidence"
    __table_args__ = (
        Index("ix_chunk_evidence_revision", "document_revision_id"),
        Index("ix_chunk_evidence_evidence", "evidence_id"),
    )

    chunk_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    evidence_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    document_revision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
