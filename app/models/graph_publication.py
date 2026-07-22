from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


GRAPH_PUBLICATION_STATUS_PLANNED = "planned"
GRAPH_PUBLICATION_STATUS_ACTIVATING = "activating"
GRAPH_PUBLICATION_STATUS_ACTIVE = "active"
GRAPH_PUBLICATION_STATUS_DEGRADED = "degraded"
GRAPH_PUBLICATION_STATUS_SUPERSEDED = "superseded"
GRAPH_PUBLICATION_STATUS_CANCELLED = "cancelled"
GRAPH_PUBLICATION_STATUS_FAILED = "failed"

GRAPH_PUBLICATION_SOURCE_INITIAL_SEED = "initial_seed"
GRAPH_PUBLICATION_SOURCE_MANUAL_PLAN = "manual_plan"
GRAPH_PUBLICATION_SOURCE_ROLLBACK = "rollback"
GRAPH_PUBLICATION_SOURCE_COORDINATED_PURGE = "coordinated_purge"


class GraphPublication(Base):
    __tablename__ = "graph_publications"
    __table_args__ = (
        CheckConstraint(
            "status IN ('planned','activating','active','degraded','superseded','cancelled','failed')",
            name="ck_graph_publications_status",
        ),
        CheckConstraint(
            "source_mode IN ('initial_seed','manual_plan','rollback','coordinated_purge')",
            name="ck_graph_publications_source_mode",
        ),
        CheckConstraint(
            "entity_count >= 0 AND relation_count >= 0",
            name="ck_graph_publications_counts",
        ),
        Index(
            "uq_graph_publications_current_scope",
            "library_id",
            "ontology_version_id",
            unique=True,
            postgresql_where=text("status IN ('active','degraded')"),
        ),
        Index(
            "uq_graph_publications_reusable_manifest",
            "library_id",
            "ontology_version_id",
            "manifest_hash",
            unique=True,
            postgresql_where=text("status IN ('planned','activating','active','degraded')"),
        ),
        Index(
            "uq_graph_publications_nonterminal_command",
            "library_id",
            "ontology_version_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("status IN ('planned','activating')"),
        ),
        Index(
            "ix_graph_publications_library_ontology_status",
            "library_id",
            "ontology_version_id",
            "status",
        ),
        Index(
            "ix_graph_publications_library_status_updated",
            "library_id",
            "status",
            "updated_at",
        ),
        Index("ix_graph_publications_parent", "parent_publication_id"),
        Index("ix_graph_publications_rollback_target", "rollback_target_publication_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="CASCADE", name="fk_graph_publications_library"),
        nullable=False,
    )
    ontology_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("ontology_versions.id", ondelete="RESTRICT", name="fk_graph_publications_ontology"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planned", server_default="planned")
    source_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    manifest_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1", server_default="v1")
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1", server_default="v1")
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    include_drafts: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    plan_options: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    parent_publication_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_publications.id",
            ondelete="SET NULL",
            name="fk_graph_publications_parent",
        ),
        nullable=True,
    )
    rollback_target_publication_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_publications.id",
            ondelete="SET NULL",
            name="fk_graph_publications_rollback_target",
        ),
        nullable=True,
    )
    planned_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_users.id", ondelete="SET NULL", name="fk_graph_publications_planned_by"),
        nullable=True,
    )
    activated_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_users.id", ondelete="SET NULL", name="fk_graph_publications_activated_by"),
        nullable=True,
    )
    cancelled_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_users.id", ondelete="SET NULL", name="fk_graph_publications_cancelled_by"),
        nullable=True,
    )
    superseded_by_publication_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_publications.id",
            ondelete="SET NULL",
            name="fk_graph_publications_superseded_by",
        ),
        nullable=True,
    )
    entity_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    relation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    blocked_counts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    blocked_diagnostics: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    item_hashes_summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    planned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reconciled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
