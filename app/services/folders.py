from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document
from app.models.folder import Folder
from app.models.library import Library


_FOLDER_LOCK_PREFIX = "vector-kb:folder-tree:"


def _folder_advisory_lock_key(library_id: uuid.UUID) -> int:
    digest = hashlib.sha256(f"{_FOLDER_LOCK_PREFIX}{library_id}".encode("ascii")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _database_dialect_name(db: AsyncSession) -> str | None:
    try:
        bind = db.sync_session.get_bind()
    except (AttributeError, RuntimeError):
        try:
            bind = db.get_bind()
        except (AttributeError, RuntimeError):
            return None
    return getattr(getattr(bind, "dialect", None), "name", None)


async def _lock_library_folder_tree(db: AsyncSession, library_id: uuid.UUID) -> None:
    dialect = _database_dialect_name(db)
    if dialect == "sqlite":
        return
    if dialect not in {None, "postgresql"}:
        raise RuntimeError("folder creation requires PostgreSQL")
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": _folder_advisory_lock_key(library_id)},
    )


def normalize_folder_path(path: str | None) -> list[str]:
    if path is None or path == "":
        return []
    if not path.startswith("/"):
        path = f"/{path}"
    stripped = path.strip("/")
    if stripped == "":
        return []
    parts = stripped.split("/")
    if any(part == "" for part in parts):
        raise ValueError("folder path contains an empty segment")
    if any(part in {".", ".."} for part in parts):
        raise ValueError("folder path contains an invalid segment")
    return parts


def _clean_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("folder name is required")
    if "/" in cleaned or cleaned in {".", ".."}:
        raise ValueError("folder name is invalid")
    return cleaned


def build_folder_path(parent_path: str | None, name: str) -> str:
    cleaned = _clean_name(name)
    parent_parts = normalize_folder_path(parent_path)
    return "/" + "/".join([*parent_parts, cleaned])


def ensure_not_descendant_move(folder: Folder, new_parent: Folder | None) -> None:
    if new_parent is None:
        return
    if folder.id == new_parent.id:
        raise ValueError("folder cannot move under itself")
    base = folder.path.rstrip("/")
    target = new_parent.path.rstrip("/")
    if target == base or target.startswith(f"{base}/"):
        raise ValueError("folder cannot move under a descendant")


def apply_document_folder(document: Document, folder_id: uuid.UUID | None) -> None:
    document.folder_id = folder_id


async def get_active_folder(db: AsyncSession, library: Library, folder_id: uuid.UUID) -> Folder:
    folder = await db.get(Folder, folder_id)
    if folder is None or folder.library_id != library.id or folder.deleted_at is not None:
        raise LookupError("folder not found")
    return folder


async def list_folders(db: AsyncSession, library: Library) -> list[Folder]:
    result = await db.execute(
        select(Folder)
        .where(Folder.library_id == library.id, Folder.deleted_at.is_(None))
        .order_by(Folder.path.asc(), Folder.sort_order.asc())
    )
    return list(result.scalars().all())


async def _create_folder_unlocked(
    db: AsyncSession,
    library: Library,
    *,
    name: str,
    parent_id: uuid.UUID | None,
    sort_order: int,
) -> Folder:
    parent = await get_active_folder(db, library, parent_id) if parent_id is not None else None
    path = build_folder_path(parent.path if parent is not None else None, name)
    folder = Folder(
        library_id=library.id,
        parent_id=parent.id if parent is not None else None,
        name=_clean_name(name),
        path=path,
        sort_order=sort_order,
    )
    db.add(folder)
    await db.flush()
    return folder


async def create_folder(
    db: AsyncSession,
    library: Library,
    *,
    name: str,
    parent_id: uuid.UUID | None,
    sort_order: int,
) -> Folder:
    await _lock_library_folder_tree(db, library.id)
    return await _create_folder_unlocked(
        db,
        library,
        name=name,
        parent_id=parent_id,
        sort_order=sort_order,
    )


async def ensure_folder_path(
    db: AsyncSession,
    library: Library,
    path: str | None,
) -> uuid.UUID | None:
    names = normalize_folder_path(path)
    if not names:
        return None
    await _lock_library_folder_tree(db, library.id)
    parent_id: uuid.UUID | None = None
    for name in names:
        conditions = [
            Folder.library_id == library.id,
            Folder.name == name,
            Folder.deleted_at.is_(None),
        ]
        if parent_id is None:
            conditions.append(Folder.parent_id.is_(None))
        else:
            conditions.append(Folder.parent_id == parent_id)
        existing = (await db.execute(select(Folder).where(*conditions).limit(1))).scalars().first()
        if existing is None:
            existing = await _create_folder_unlocked(
                db,
                library,
                name=name,
                parent_id=parent_id,
                sort_order=0,
            )
        parent_id = existing.id
    return parent_id


async def update_folder(db: AsyncSession, library: Library, folder_id: uuid.UUID, payload) -> Folder:
    folder = await get_active_folder(db, library, folder_id)
    fields = getattr(payload, "model_fields_set", set())

    new_parent = None
    parent_changed = "parent_id" in fields
    if parent_changed and payload.parent_id is not None:
        new_parent = await get_active_folder(db, library, payload.parent_id)
        ensure_not_descendant_move(folder, new_parent)
    elif not parent_changed and folder.parent_id is not None:
        new_parent = await get_active_folder(db, library, folder.parent_id)

    new_name = _clean_name(payload.name) if "name" in fields and payload.name is not None else folder.name
    old_path = folder.path
    new_parent_path = new_parent.path if new_parent is not None else None
    new_path = build_folder_path(new_parent_path, new_name)

    folder.name = new_name
    if parent_changed:
        folder.parent_id = new_parent.id if new_parent is not None else None
    if "sort_order" in fields and payload.sort_order is not None:
        folder.sort_order = payload.sort_order
    folder.path = new_path

    if new_path != old_path:
        result = await db.execute(
            select(Folder).where(
                Folder.library_id == library.id,
                Folder.deleted_at.is_(None),
                Folder.path.like(f"{old_path.rstrip('/')}/%"),
            )
        )
        descendants = list(result.scalars().all())
        old_prefix = old_path.rstrip("/")
        new_prefix = new_path.rstrip("/")
        for descendant in descendants:
            descendant.path = f"{new_prefix}{descendant.path[len(old_prefix):]}"

    await db.flush()
    return folder


async def delete_folder(db: AsyncSession, library: Library, folder_id: uuid.UUID) -> None:
    folder = await get_active_folder(db, library, folder_id)
    child_result = await db.execute(
        select(Folder.id)
        .where(
            Folder.library_id == library.id,
            Folder.parent_id == folder.id,
            Folder.deleted_at.is_(None),
        )
        .limit(1)
    )
    if child_result.scalars().first() is not None:
        raise ValueError("folder is not empty")

    document_result = await db.execute(
        select(Document.id)
        .where(
            Document.library_id == library.id,
            Document.folder_id == folder.id,
            Document.deleted_at.is_(None),
        )
        .limit(1)
    )
    if document_result.scalars().first() is not None:
        raise ValueError("folder is not empty")

    folder.deleted_at = datetime.now(timezone.utc)
    await db.flush()


async def move_document_to_folder(
    db: AsyncSession,
    library: Library,
    document_id: uuid.UUID,
    folder_id: uuid.UUID | None,
) -> Document:
    document = await db.get(Document, document_id)
    if document is None or document.library_id != library.id or document.deleted_at is not None:
        raise LookupError("document not found")
    if folder_id is not None:
        await get_active_folder(db, library, folder_id)
    apply_document_folder(document, folder_id)
    await db.flush()
    return document
