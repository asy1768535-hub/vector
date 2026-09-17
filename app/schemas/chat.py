"""Chat 用户端（轻量问答 v1）schemas。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class ChatLibraryRead(BaseModel):
    slug: str
    name: str
    description: Optional[str] = None

    model_config = {"from_attributes": True}


class ChatMessageRequest(BaseModel):
    library_slug: str = Field(..., min_length=1)
    folder_id: Optional[uuid.UUID] = None
    query: str = Field(..., max_length=2000)
    top_k: int = Field(default=5, ge=1, le=20)
    use_graph: bool = True
    show_debug: bool = False
    # 可选：续聊已有会话。无 → 自动新建会话。
    conversation_id: Optional[uuid.UUID] = None

    @field_validator("query")
    @classmethod
    def _q(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("query 去掉首尾空格后不能为空")
        return v


class ChatSource(BaseModel):
    title: str = ""
    document_id: Optional[str] = None
    chunk_id: Optional[str] = None
    seq: Optional[int] = None
    score: float = 0.0
    score_type: Literal["rerank", "vector", "rrf", "legacy"] = "rrf"
    display_score: Optional[float] = None
    content: str = ""


class ChatGraphEvidence(BaseModel):
    citation_index: int = Field(default=0, ge=0, le=100)
    publication_id: uuid.UUID
    relation_id: uuid.UUID
    source_entity_id: uuid.UUID
    source_entity_name: str = Field(max_length=512)
    relation_type_key: str = Field(max_length=128)
    relation_label: str = Field(max_length=255)
    depth: int = Field(default=1, ge=1, le=3)
    target_entity_id: uuid.UUID
    target_entity_name: str = Field(max_length=512)
    evidence_id: uuid.UUID
    document_id: uuid.UUID
    document_revision_id: uuid.UUID
    chunk_id: uuid.UUID
    title: str = Field(default="", max_length=512)
    page_start: Optional[int] = Field(default=None, ge=1)
    page_end: Optional[int] = Field(default=None, ge=1)
    content: str = Field(default="", max_length=4000)

    model_config = {"extra": "forbid"}


class ChatMessageResponse(BaseModel):
    answer: str
    sources: list[ChatSource] = Field(default_factory=list)
    graph_augmented: bool = False
    graph_evidence: list[ChatGraphEvidence] = Field(default_factory=list, max_length=20)
    conversation_id: Optional[uuid.UUID] = None
    # show_debug=false 时为 null；true 时含 matched_queries / rerank_scores 等
    debug: Optional[dict[str, Any]] = None


# ── 会话历史 ────────────────────────────────────────────────────────────────
class ChatConversationRead(BaseModel):
    id: uuid.UUID
    library_slug: str
    title: str
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ChatHistoryMessage(BaseModel):
    """会话恢复时的单条消息（含 assistant 的 sources）。"""
    id: uuid.UUID
    role: str
    content: str
    status: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime
    sources: list[ChatSource] = Field(default_factory=list)
    graph_augmented: bool = False
    graph_evidence: list[ChatGraphEvidence] = Field(default_factory=list, max_length=20)

    model_config = {"from_attributes": True}


# ── 管理后台：问答日志 ──────────────────────────────────────────────────────
class ChatLogRow(BaseModel):
    """一问一答为一行（assistant 消息为主，配对其 user 问题）。"""
    message_id: uuid.UUID
    conversation_id: uuid.UUID
    user_id: Optional[uuid.UUID] = None
    library_slug: str
    question: str = ""
    answer: str = ""
    rewritten_query: Optional[str] = None
    status: Optional[str] = None
    error_message: Optional[str] = None
    latency_ms: Optional[int] = None
    created_at: datetime
    sources: list[ChatSource] = Field(default_factory=list)
    graph_augmented: bool = False
    graph_evidence: list[ChatGraphEvidence] = Field(default_factory=list, max_length=20)
