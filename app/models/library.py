"""sys_libraries：知识库定义。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Library(Base):
    __tablename__ = "sys_libraries"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    embedding_model: Mapped[str] = mapped_column(String(80), nullable=False)
    embedding_dim: Mapped[int] = mapped_column(Integer, nullable=False)
    vector_distance: Mapped[str] = mapped_column(String(16), nullable=False, default="cosine")
    # 库级 embedding 服务 URL；null = 用全局 settings.embedding_base_url
    embedding_base_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    chunk_overlap: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    qdrant_collection: Mapped[str] = mapped_column(String(128), nullable=False)

    # 源数据补全配置（JSONB）：当 Qdrant payload 只存外键、正文在别的业务库时，
    # 检索后按外键回查源库大表把正文拼回。null = 不补全（走 payload.text）。
    # 结构见 app/services/source_enrichment.py:parse_source_config
    source_config: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    created_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sys_users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
