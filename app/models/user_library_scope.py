from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class UserLibraryScope(Base):
    __tablename__ = "user_library_scopes"
    __table_args__ = (
        CheckConstraint(
            "scope_kind IN ('last_used','named')",
            name="ck_user_library_scopes_kind",
        ),
        CheckConstraint(
            "((scope_kind = 'last_used' AND name IS NULL AND normalized_name IS NULL) OR "
            "(scope_kind = 'named' AND name IS NOT NULL AND normalized_name IS NOT NULL))",
            name="ck_user_library_scopes_name_shape",
        ),
        CheckConstraint(
            "name IS NULL OR char_length(name) BETWEEN 1 AND 80",
            name="ck_user_library_scopes_name_length",
        ),
        CheckConstraint(
            "normalized_name IS NULL OR char_length(normalized_name) BETWEEN 1 AND 80",
            name="ck_user_library_scopes_normalized_name_length",
        ),
        Index(
            "uq_user_library_scopes_last_used_owner",
            "organization_id",
            "user_id",
            unique=True,
            postgresql_where=text("scope_kind = 'last_used'"),
        ),
        Index(
            "uq_user_library_scopes_named_owner_name",
            "organization_id",
            "user_id",
            "normalized_name",
            unique=True,
            postgresql_where=text("scope_kind = 'named'"),
        ),
        Index(
            "ix_user_library_scopes_owner_kind_updated",
            "organization_id",
            "user_id",
            "scope_kind",
            "updated_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_organizations.id",
            ondelete="RESTRICT",
            name="fk_user_library_scopes_organization",
        ),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_users.id",
            ondelete="CASCADE",
            name="fk_user_library_scopes_user",
        ),
        nullable=False,
    )
    scope_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    normalized_name: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class UserLibraryScopeItem(Base):
    __tablename__ = "user_library_scope_items"
    __table_args__ = (
        CheckConstraint(
            "ordinal >= 0 AND ordinal < 20",
            name="ck_user_library_scope_items_ordinal",
        ),
        UniqueConstraint(
            "scope_id",
            "library_id",
            name="uq_user_library_scope_items_scope_library",
        ),
        UniqueConstraint(
            "scope_id",
            "ordinal",
            name="uq_user_library_scope_items_scope_ordinal",
        ),
        Index("ix_user_library_scope_items_library", "library_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    scope_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "user_library_scopes.id",
            ondelete="CASCADE",
            name="fk_user_library_scope_items_scope",
        ),
        nullable=False,
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_libraries.id",
            ondelete="RESTRICT",
            name="fk_user_library_scope_items_library",
        ),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
