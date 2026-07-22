from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


DEFAULT_ORGANIZATION_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
DEFAULT_ORGANIZATION_SLUG = "default"

ORGANIZATION_PROFILES = ("private", "hosted")
ORGANIZATION_STATUSES = ("active", "suspended")


class Organization(Base):
    __tablename__ = "sys_organizations"
    __table_args__ = (
        CheckConstraint(
            "slug ~ '^[a-z][a-z0-9_-]{1,62}[a-z0-9]$'",
            name="ck_sys_organizations_slug",
        ),
        CheckConstraint(
            "btrim(name) <> ''",
            name="ck_sys_organizations_name",
        ),
        CheckConstraint(
            "deployment_profile IN ('private','hosted')",
            name="ck_sys_organizations_deployment_profile",
        ),
        CheckConstraint(
            "status IN ('active','suspended')",
            name="ck_sys_organizations_status",
        ),
        UniqueConstraint("slug", name="uq_sys_organizations_slug"),
        Index("ix_sys_organizations_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    deployment_profile: Mapped[str] = mapped_column(
        String(16), nullable=False, default="private", server_default="private"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_users.id",
            ondelete="SET NULL",
            name="fk_sys_organizations_created_by",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
