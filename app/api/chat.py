"""Chat 用户端（轻量问答 + 会话历史 + 问答审计）。

普通用户对自己有 read 权限的库直接问答：复用现有检索召回 chunks，再调 OpenAI 兼容 chat
模型生成答案。每轮问答落库（会话/消息/来源），支持多轮追问（仅理解，不作事实依据）。
**不改 Dify API、不改 /libraries/{slug}/query、不接 Agent/工具。**

权限边界：
  - 必须登录（current_active_user）。
  - library_slug 必须有 read 权限（superuser 直通），否则 403。
  - conversation_id 必须属于当前用户、同一知识库、active，否则 403/409/400。
  - 检索 collection 由库记录推导，请求体无 collection 字段 → 杜绝越权。
  - CHAT_API_KEY 只在后端使用，绝不下发前端、不写日志。
"""
from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.backend import current_active_user
from app.casbin import service as casbin_service
from app.casbin.enforcer import has_permission
from app.config import settings
from app.db import async_session_factory, get_db
from app.deps import load_active_library
from app.models.chat_history import ChatConversation
from app.models.library import Library
from app.models.user import User
from app.schemas.chat import (
    ChatConversationRead,
    ChatHistoryMessage,
    ChatLibraryRead,
    ChatMessageRequest,
    ChatMessageResponse,
    ChatSource,
)
from app.schemas.dify import DifyRetrievalRequest, RetrievalSetting
from app.services import chat_answer, chat_history
from app.services.retrieval import run_retrieval

log = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])


@router.get("/libraries", response_model=list[ChatLibraryRead])
async def chat_libraries(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[Library]:
    """当前用户可问答的库：superuser 看全部 active；普通用户只看有 read 权限的库。"""
    base = select(Library).where(Library.deleted_at.is_(None)).order_by(Library.name.asc())
    if user.is_superuser:
        rows = await db.execute(base)
        return list(rows.scalars().all())
    perms = casbin_service.list_user_permissions(str(user.id))
    read_slugs = [slug for slug, actions in perms.items() if "read" in actions]
    if not read_slugs:
        return []
    rows = await db.execute(base.where(Library.slug.in_(read_slugs)))
    return list(rows.scalars().all())


def _to_source(record) -> ChatSource:
    md = record.metadata or {}

    def _s(v):
        return None if v is None else str(v)

    def _i(v):
        try:
            return None if v is None else int(v)
        except (TypeError, ValueError):
            return None

    return ChatSource(
        title=record.title or "",
        document_id=_s(md.get("document_id")),
        chunk_id=_s(md.get("chunk_id")),
        seq=_i(md.get("seq")),
        score=float(record.score or 0.0),
        content=record.content or "",
    )


def _rewritten_query(records, query: str) -> str | None:
    """从召回结果的 matched_queries 推导本轮实际检索用到的 query（Query Rewrite 留痕）。"""
    matched: list[str] = []
    for r in records:
        for q in (r.metadata or {}).get("matched_queries") or []:
            if q not in matched:
                matched.append(q)
    if not matched or matched == [query]:
        return None
    return " | ".join(matched)


def _build_debug(records, top_k: int) -> dict:
    matched: list[str] = []
    for r in records:
        for q in (r.metadata or {}).get("matched_queries") or []:
            if q not in matched:
                matched.append(q)
    return {
        "matched_queries": matched,
        "rerank_scores": [(r.metadata or {}).get("rerank_score") for r in records],
        "vector_scores": [(r.metadata or {}).get("vector_score") for r in records],
        "recalled": len(records),
        "top_k": top_k,
        "chat_model": settings.chat_model,
    }


async def _retrieve_for_chat(body: ChatMessageRequest, user: User, db: AsyncSession):
    """开关 + read 权限 + 库状态校验，复用现有检索。返回 (lib, records)；不通过抛 HTTPException。"""
    if not settings.chat_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Chat 功能未启用")
    lib = await load_active_library(body.library_slug, db)        # 不暴露存在性，统一 403
    if lib is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    if not user.is_superuser and not has_permission(str(user.id), lib.slug, "read"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
    if lib.index_state in ("rebuilding", "failed"):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "library index rebuilding")
    try:
        retr = await run_retrieval(
            collection=lib.qdrant_collection,
            embedding_model=lib.embedding_model,
            embedding_base_url=lib.embedding_base_url,
            request=DifyRetrievalRequest(
                knowledge_id=lib.slug, query=body.query,
                retrieval_setting=RetrievalSetting(top_k=body.top_k),
            ),
            source_config=lib.source_config,
            rerank_enabled=lib.rerank_enabled,
            retrieval_mode=lib.retrieval_mode,
            db=db,
            library=lib,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("chat retrieval failed: slug=%s", lib.slug)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "retrieval failed") from exc
    return lib, retr.records


async def _resolve_conversation(db: AsyncSession, user: User, body: ChatMessageRequest) -> ChatConversation:
    """续聊校验 / 新建会话。conversation_id 不属于自己 → 403；非 active → 409；库不一致 → 400。"""
    if body.conversation_id is not None:
        conv = await chat_history.get_conversation(db, body.conversation_id)
        if conv is None or str(conv.user_id) != str(user.id):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
        if conv.status != "active":
            raise HTTPException(status.HTTP_409_CONFLICT, "会话已归档或删除，请新建会话")
        if conv.library_slug != body.library_slug:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "会话与知识库不一致")
        return conv
    return await chat_history.create_conversation(db, user.id, body.library_slug, body.query)


@router.post("/messages", response_model=ChatMessageResponse)
async def chat_messages(
    body: ChatMessageRequest,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ChatMessageResponse:
    """非流式问答 + 落库。无 conversation_id 自动建会话，有则校验后续聊。"""
    conv = await _resolve_conversation(db, user, body) if body.conversation_id is not None else None
    _lib, records = await _retrieve_for_chat(body, user, db)
    if conv is None:
        conv = await _resolve_conversation(db, user, body)
    history = await chat_history.recent_turns(db, conv.id, settings.chat_history_max_turns)
    user_msg = await chat_history.save_user_message(db, conv.id, body.query)

    debug = _build_debug(records, body.top_k) if body.show_debug else None
    rewritten = _rewritten_query(records, body.query)
    t0 = time.monotonic()
    status_val, err, used = "success", None, []
    if not records:
        answer_text = "资料中未找到明确依据。"
    else:
        try:
            result = await chat_answer.generate_answer(
                body.query, records,
                base_url=settings.chat_base_url, model=settings.chat_model,
                api_key=settings.chat_api_key, timeout=settings.chat_timeout_seconds,
                temperature=settings.chat_temperature, max_context_chars=settings.chat_max_context_chars,
                history=history,
            )
            answer_text, used = result.answer, result.used_records
        except chat_answer.ChatError as exc:
            status_val, err, answer_text = "failed", str(exc), ""
    latency_ms = int((time.monotonic() - t0) * 1000)
    sources = [_to_source(r) for r in used]

    await chat_history.save_assistant_message(
        db, conv.id, content=answer_text, rewritten_query=rewritten, latency_ms=latency_ms,
        status=status_val, error_message=err, parent_message_id=user_msg.id, sources=sources,
    )
    await chat_history.touch_conversation(db, conv.id)
    await db.commit()

    if status_val == "failed":
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"answer generation failed: {err}")
    return ChatMessageResponse(answer=answer_text, sources=sources, conversation_id=conv.id, debug=debug)


def _sse(obj: dict) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


async def _persist_assistant(conv_id, *, parent_id, content, rewritten, latency_ms, status_val, error, sources):
    """流式结束后用独立 session 落 assistant 消息（请求 session 此时已关闭）。"""
    async with async_session_factory() as db2:
        await chat_history.save_assistant_message(
            db2, conv_id, content=content, rewritten_query=rewritten, latency_ms=latency_ms,
            status=status_val, error_message=error, parent_message_id=parent_id, sources=sources,
        )
        await chat_history.touch_conversation(db2, conv_id)
        await db2.commit()


@router.post("/stream")
async def chat_stream(
    body: ChatMessageRequest,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """SSE 流式问答 + 落库：先发 sources（含 conversation_id），再逐段 delta，最后 done；出错发 error。

    会话/用户消息在开流前用请求 session 落库并提交；assistant 消息在流结束后用独立 session 落库。
    """
    conv = await _resolve_conversation(db, user, body) if body.conversation_id is not None else None
    _lib, records = await _retrieve_for_chat(body, user, db)
    if conv is None:
        conv = await _resolve_conversation(db, user, body)
    history = await chat_history.recent_turns(db, conv.id, settings.chat_history_max_turns)
    user_msg = await chat_history.save_user_message(db, conv.id, body.query)
    await db.commit()                                    # 会话 + user 消息先持久化

    conv_id, user_msg_id = conv.id, user_msg.id
    debug = _build_debug(records, body.top_k) if body.show_debug else None
    rewritten = _rewritten_query(records, body.query)
    _ctx, used = chat_answer.build_context(records, settings.chat_max_context_chars) if records else ("", [])
    sources = [_to_source(r) for r in used]
    sources_dump = [s.model_dump() for s in sources]
    query = body.query

    async def _gen():
        yield _sse({"type": "sources", "conversation_id": str(conv_id), "sources": sources_dump, "debug": debug})
        t0 = time.monotonic()
        if not records:
            text = "资料中未找到明确依据。"
            yield _sse({"type": "delta", "text": text})
            await _persist_assistant(conv_id, parent_id=user_msg_id, content=text, rewritten=rewritten,
                                     latency_ms=int((time.monotonic() - t0) * 1000), status_val="success",
                                     error=None, sources=[])
            yield _sse({"type": "done"})
            return
        acc = []
        try:
            async for delta in chat_answer.stream_answer(
                query, records,
                base_url=settings.chat_base_url, model=settings.chat_model,
                api_key=settings.chat_api_key, timeout=settings.chat_timeout_seconds,
                temperature=settings.chat_temperature, max_context_chars=settings.chat_max_context_chars,
                history=history,
            ):
                acc.append(delta)
                yield _sse({"type": "delta", "text": delta})
        except chat_answer.ChatError as exc:
            await _persist_assistant(conv_id, parent_id=user_msg_id, content="".join(acc), rewritten=rewritten,
                                     latency_ms=int((time.monotonic() - t0) * 1000), status_val="failed",
                                     error=str(exc), sources=sources)
            yield _sse({"type": "error", "message": f"answer generation failed: {exc}"})
            return
        await _persist_assistant(conv_id, parent_id=user_msg_id, content="".join(acc), rewritten=rewritten,
                                 latency_ms=int((time.monotonic() - t0) * 1000), status_val="success",
                                 error=None, sources=sources)
        yield _sse({"type": "done"})

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── 会话管理 ────────────────────────────────────────────────────────────────
async def _owned_conversation(db: AsyncSession, user: User, conversation_id) -> ChatConversation:
    conv = await chat_history.get_conversation(db, conversation_id)
    if conv is None or str(conv.user_id) != str(user.id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")   # 别人的会话 → 403
    return conv


@router.get("/conversations", response_model=list[ChatConversationRead])
async def list_conversations(
    include_archived: bool = Query(default=False),
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[ChatConversation]:
    return await chat_history.list_conversations(db, user.id, include_archived=include_archived)


@router.get("/conversations/{conversation_id}/messages", response_model=list[ChatHistoryMessage])
async def conversation_messages(
    conversation_id: str,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> list[ChatHistoryMessage]:
    conv = await _owned_conversation(db, user, conversation_id)
    pairs = await chat_history.get_conversation_messages(db, conv.id)
    out: list[ChatHistoryMessage] = []
    for m, srcs in pairs:
        out.append(ChatHistoryMessage(
            id=m.id, role=m.role, content=m.content, status=m.status,
            error_message=m.error_message, created_at=m.created_at,
            sources=[chat_history.src_to_schema(s) for s in srcs],
        ))
    return out


@router.post("/conversations/{conversation_id}/archive", response_model=ChatConversationRead)
async def archive_conversation(
    conversation_id: str,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> ChatConversation:
    conv = await _owned_conversation(db, user, conversation_id)
    await chat_history.archive_conversation(db, conv)
    await db.commit()
    await db.refresh(conv)
    return conv


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: str,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    conv = await _owned_conversation(db, user, conversation_id)
    await chat_history.delete_conversation(db, conv)
    await db.commit()
    return None
