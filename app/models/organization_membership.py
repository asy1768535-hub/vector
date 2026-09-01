from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


ORGANIZATION_MEMBERSHIP_ROLES = ("organization_admin", "member")
ORGANIZATION_MEMBERSHIP_STATUSES = ("active", "disabled")


class OrganizationMembership(Base):
    __tablename__ = "sys_organization_memberships"
    __table_args__ = (
        CheckConstraint(
            "role IN ('organization_admin','member')",
            name="ck_sys_organization_memberships_role",
        ),
        CheckConstraint(
            "status IN ('active','disabled')",
            name="ck_sys_organization_memberships_status",
        ),
        CheckConstraint(
            "((status = 'active' AND disabled_at IS NULL) OR "
            "(status = 'disabled' AND disabled_at IS NOT NULL))",
            name="ck_sys_organization_memberships_disabled_shape",
        ),
        UniqueConstraint(
            "organization_id",
            "user_id",
            name="uq_sys_organization_memberships_org_user",
        ),
        Index(
            "ix_sys_organization_memberships_user_status",
            "user_id",
            "status",
        ),
        Index(
            "ix_sys_organization_memberships_org_status_role",
            "organization_id",
            "status",
            "role",
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
            name="fk_sys_organization_memberships_organization",
        ),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_users.id",
            ondelete="RESTRICT",
            name="fk_sys_organization_memberships_user",
        ),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_users.id",
            ondelete="SET NULL",
            name="fk_sys_organization_memberships_created_by",
        ),
        nullable=True,
    )
    disabled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
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
