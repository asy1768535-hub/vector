from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


ORGANIZATION_CAPABILITIES = (
    "public_api_v1",
    "mcp_adapter",
    "external_graph_sync",
)


class OrganizationCapabilityRollout(Base):
    __tablename__ = "organization_capability_rollouts"
    __table_args__ = (
        CheckConstraint(
            "capability IN ('public_api_v1','mcp_adapter','external_graph_sync')",
            name="ck_org_capability_rollouts_capability",
        ),
        CheckConstraint("version >= 1", name="ck_org_capability_rollouts_version"),
        UniqueConstraint(
            "organization_id",
            "capability",
            name="uq_org_capability_rollouts_org_capability",
        ),
        Index("ix_org_capability_rollouts_enabled", "capability", "enabled"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    capability: Mapped[str] = mapped_column(String(48), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    changed_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
