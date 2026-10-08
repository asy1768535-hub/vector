"""Chat 会话历史 + 问答审计：会话/消息/来源的持久化与查询。

集中所有 DB 访问。被 /chat 用户端与 /admin/chat-logs 调用。不参与检索链路。
"""
from __future__ import annotations

import uuid

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql import func

from app.models.chat_history import ChatConversation, ChatMessage, ChatMessageSource
from app.schemas.chat import ChatGraphEvidence, ChatLogRow, ChatSource

_TITLE_MAX = 28
CONVERSATION_MESSAGES_DEFAULT_LIMIT = 200
CONVERSATION_MESSAGES_MAX_LIMIT = 500


def title_from_query(query: str) -> str:
    """会话标题：取首条问题前 ~28 字（去空白）。"""
    s = " ".join((query or "").split()).strip()
    if not s:
        return "新会话"
    return s[:_TITLE_MAX] + ("…" if len(s) > _TITLE_MAX else "")


async def create_conversation(
    db: AsyncSession, user_id: uuid.UUID, library_slug: str, first_query: str,
) -> ChatConversation:
    conv = ChatConversation(
        user_id=user_id, library_slug=library_slug, title=title_from_query(first_query), status="active",
    )
    db.add(conv)
    await db.flush()
    await db.refresh(conv)
    return conv


async def get_conversation(db: AsyncSession, conversation_id: uuid.UUID) -> ChatConversation | None:
    return (
        await db.execute(select(ChatConversation).where(ChatConversation.id == conversation_id))
    ).scalar_one_or_none()


async def list_conversations(
    db: AsyncSession, user_id: uuid.UUID, *, include_archived: bool = False,
) -> list[ChatConversation]:
    stmt = select(ChatConversation).where(ChatConversation.user_id == user_id)
    if not include_archived:
        stmt = stmt.where(ChatConversation.status == "active")
    stmt = stmt.order_by(ChatConversation.updated_at.desc())
    return list((await db.execute(stmt)).scalars().all())


async def touch_conversation(db: AsyncSession, conversation_id: uuid.UUID) -> None:
    """续聊后把会话顶到列表最前（更新 updated_at）。"""
    await db.execute(
        update(ChatConversation).where(ChatConversation.id == conversation_id).values(updated_at=func.now())
    )


async def archive_conversation(db: AsyncSession, conv: ChatConversation) -> None:
    conv.status = "archived"
    await db.flush()


async def delete_conversation(db: AsyncSession, conv: ChatConversation) -> None:
    await db.delete(conv)         # 级联删除 messages + sources
    await db.flush()


async def recent_turns(
    db: AsyncSession, conversation_id: uuid.UUID, max_turns: int,
) -> list[dict]:
    """最近 max_turns 轮历史（user + 成功的 assistant），oldest→newest，用于理解追问。

    失败/空的 assistant 不带入。max_turns<=0 → 返回空（不带历史）。
    """
    if max_turns <= 0:
        return []
    assistant = aliased(ChatMessage)
    stmt = (
        select(ChatMessage, assistant)
        .join(
            assistant,
            and_(
                assistant.parent_message_id == ChatMessage.id,
                assistant.conversation_id == conversation_id,
                assistant.role == "assistant",
                assistant.status == "success",
                func.trim(assistant.content) != "",
            ),
        )
        .where(
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.role == "user",
        )
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(max_turns)
    )
    pairs = list((await db.execute(stmt)).all())
    out: list[dict] = []
    for user, assistant in reversed(pairs):
        out.extend([
            {"role": "user", "content": user.content},
            {"role": "assistant", "content": assistant.content},
        ])
    return out


async def save_user_message(db: AsyncSession, conversation_id: uuid.UUID, content: str) -> ChatMessage:
    msg = ChatMessage(conversation_id=conversation_id, role="user", content=content)
    db.add(msg)
    await db.flush()
    await db.refresh(msg)
    return msg


async def save_assistant_message(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    content: str,
    rewritten_query: str | None,
    latency_ms: int | None,
    status: str,
    error_message: str | None,
    parent_message_id: uuid.UUID | None,
    sources: list[ChatSource],
    graph_augmented: bool = False,
    graph_evidence: list[ChatGraphEvidence] | None = None,
) -> ChatMessage:
    msg = ChatMessage(
        conversation_id=conversation_id, role="assistant", content=content,
        rewritten_query=rewritten_query, latency_ms=latency_ms, status=status,
        error_message=error_message, parent_message_id=parent_message_id,
        graph_augmented=graph_augmented,
        graph_evidence=[row.model_dump(mode="json") for row in graph_evidence or []],
    )
    db.add(msg)
    await db.flush()
    for i, s in enumerate(sources):
        db.add(ChatMessageSource(
            message_id=msg.id, seq=i, title=s.title or None,
            document_id=s.document_id, chunk_id=s.chunk_id, score=s.score,
            score_type=s.score_type, display_score=s.display_score, content=s.content,
        ))
    await db.flush()
    await db.refresh(msg)
    return msg


async def _sources_by_message(db: AsyncSession, message_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[ChatMessageSource]]:
    if not message_ids:
        return {}
    rows = (await db.execute(
        select(ChatMessageSource)
        .where(ChatMessageSource.message_id.in_(message_ids))
        .order_by(ChatMessageSource.seq.asc())
    )).scalars().all()
    out: dict[uuid.UUID, list[ChatMessageSource]] = {}
    for r in rows:
        out.setdefault(r.message_id, []).append(r)
    return out


async def get_conversation_messages(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    limit: int = CONVERSATION_MESSAGES_DEFAULT_LIMIT,
    before: uuid.UUID | None = None,
) -> list[tuple[ChatMessage, list[ChatMessageSource]]]:
    """返回最近的有界消息，恢复顺序仍为时间升序；assistant 带上其 sources。"""
    if limit <= 0:
        return []
    stmt = select(ChatMessage).where(
        ChatMessage.conversation_id == conversation_id,
    )
    if before is not None:
        cursor_created_at = select(ChatMessage.created_at).where(
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.id == before,
        ).scalar_subquery()
        stmt = stmt.where(or_(
            ChatMessage.created_at < cursor_created_at,
            and_(
                ChatMessage.created_at == cursor_created_at,
                ChatMessage.id < before,
            ),
        ))
    stmt = stmt.order_by(
        ChatMessage.created_at.desc(),
        ChatMessage.id.desc(),
    ).limit(min(limit, CONVERSATION_MESSAGES_MAX_LIMIT))
    msgs = list((await db.execute(stmt)).scalars().all())
    msgs.reverse()
    src_map = await _sources_by_message(db, [m.id for m in msgs])
    return [(m, src_map.get(m.id, [])) for m in msgs]


def src_to_schema(s: ChatMessageSource) -> ChatSource:
    return ChatSource(
        title=s.title or "", document_id=s.document_id, chunk_id=s.chunk_id,
        score=float(s.score or 0.0), score_type=s.score_type, display_score=s.display_score,
        content=s.content or "",
    )


def graph_evidence_to_schema(rows: list[dict] | None) -> list[ChatGraphEvidence]:
    return [ChatGraphEvidence.model_validate(row) for row in rows] if isinstance(rows, list) else []


def historical_sources_to_schema(
    sources: list[ChatMessageSource], graph_rows: list[dict] | None,
) -> list[ChatSource]:
    """Restore saved citation order without projecting current revision locations onto history."""
    occupied = {
        row["citation_index"] for row in graph_rows or []
        if isinstance(row, dict) and type(row.get("citation_index")) is int
        and 1 <= row["citation_index"] <= 100
    }
    indexes = iter(index for index in range(1, 101) if index not in occupied)
    return [src_to_schema(source).model_copy(update={"citation_index": next(indexes, None)})
            for source in sources]


async def list_logs(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None = None,
    library_slug: str | None = None,
    status: str | None = None,
    start=None,
    end=None,
    limit: int = 100,
    offset: int = 0,
) -> list[ChatLogRow]:
    """管理后台问答日志：一行 = 一条 assistant 消息 + 其 user 问题 + sources。"""
    stmt = (
        select(ChatMessage, ChatConversation)
        .join(ChatConversation, ChatMessage.conversation_id == ChatConversation.id)
        .where(ChatMessage.role == "assistant")
    )
    if user_id is not None:
        stmt = stmt.where(ChatConversation.user_id == user_id)
    if library_slug:
        stmt = stmt.where(ChatConversation.library_slug == library_slug)
    if status:
        stmt = stmt.where(ChatMessage.status == status)
    if start is not None:
        stmt = stmt.where(ChatMessage.created_at >= start)
    if end is not None:
        stmt = stmt.where(ChatMessage.created_at <= end)
    stmt = stmt.order_by(ChatMessage.created_at.desc()).limit(limit).offset(offset)
    pairs = list((await db.execute(stmt)).all())

    # 问题：批量取 parent user 消息
    parent_ids = [m.parent_message_id for m, _c in pairs if m.parent_message_id]
    questions: dict[uuid.UUID, str] = {}
    if parent_ids:
        for um in (await db.execute(
            select(ChatMessage).where(ChatMessage.id.in_(parent_ids))
        )).scalars().all():
            questions[um.id] = um.content
    src_map = await _sources_by_message(db, [m.id for m, _c in pairs])

    rows: list[ChatLogRow] = []
    for m, conv in pairs:
        rows.append(ChatLogRow(
            message_id=m.id, conversation_id=conv.id, user_id=conv.user_id,
            library_slug=conv.library_slug,
            question=questions.get(m.parent_message_id, "") if m.parent_message_id else "",
            answer=m.content, rewritten_query=m.rewritten_query, status=m.status,
            error_message=m.error_message, latency_ms=m.latency_ms, created_at=m.created_at,
            sources=historical_sources_to_schema(src_map.get(m.id, []), m.graph_evidence),
            graph_augmented=m.graph_augmented is True,
            graph_evidence=graph_evidence_to_schema(m.graph_evidence),
        ))
    return rows
