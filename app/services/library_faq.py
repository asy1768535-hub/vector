"""知识库「常用问题」服务层：FAQ 的增删改查。

集中所有 DB 访问，API 只做权限/装配。FAQ 只改善提问体验，不参与检索链路。
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.library_faq import LibraryFAQQuestion
from app.schemas.admin import LibraryFAQCreate, LibraryFAQUpdate


async def list_faqs(
    db: AsyncSession, library_id: uuid.UUID, *, include_inactive: bool = False,
) -> list[LibraryFAQQuestion]:
    """按库列出 FAQ。默认只返回 active；按 sort_order asc, created_at asc 排序。"""
    stmt = select(LibraryFAQQuestion).where(LibraryFAQQuestion.library_id == library_id)
    if not include_inactive:
        stmt = stmt.where(LibraryFAQQuestion.is_active.is_(True))
    stmt = stmt.order_by(
        LibraryFAQQuestion.sort_order.asc(), LibraryFAQQuestion.created_at.asc()
    )
    rows = await db.execute(stmt)
    return list(rows.scalars().all())


async def get_faq(
    db: AsyncSession, library_id: uuid.UUID, faq_id: uuid.UUID,
) -> LibraryFAQQuestion | None:
    """取单条 FAQ，并校验它属于该库（跨库返回 None）。"""
    stmt = select(LibraryFAQQuestion).where(
        LibraryFAQQuestion.id == faq_id, LibraryFAQQuestion.library_id == library_id,
    )
    rows = await db.execute(stmt)
    return rows.scalar_one_or_none()


async def create_faq(
    db: AsyncSession, library_id: uuid.UUID, payload: LibraryFAQCreate,
) -> LibraryFAQQuestion:
    faq = LibraryFAQQuestion(
        library_id=library_id,
        question=payload.question,          # schema 已 strip 校验
        sort_order=payload.sort_order,
        is_active=payload.is_active,
    )
    db.add(faq)
    await db.flush()
    await db.refresh(faq)
    return faq


async def update_faq(
    db: AsyncSession, faq: LibraryFAQQuestion, payload: LibraryFAQUpdate,
) -> LibraryFAQQuestion:
    """部分更新：None = 不改（question 若传则已被 schema strip 校验）。"""
    if payload.question is not None:
        faq.question = payload.question
    if payload.sort_order is not None:
        faq.sort_order = payload.sort_order
    if payload.is_active is not None:
        faq.is_active = payload.is_active
    await db.flush()
    await db.refresh(faq)
    return faq


async def delete_faq(db: AsyncSession, faq: LibraryFAQQuestion) -> None:
    """第一版：物理删除（简单）。"""
    await db.delete(faq)
    await db.flush()
