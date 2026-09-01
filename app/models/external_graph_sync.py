from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class GraphSyncSourcePolicy(Base):
    __tablename__ = "graph_sync_source_policies"
    __table_args__ = (
        CheckConstraint("authority_rank BETWEEN 1 AND 999", name="ck_graph_sync_policies_rank"),
        CheckConstraint("status IN ('active','disabled')", name="ck_graph_sync_policies_status"),
        CheckConstraint(
            "stale_after_seconds BETWEEN 60 AND 31536000",
            name="ck_graph_sync_policies_stale",
        ),
        UniqueConstraint("sync_source_id", name="uq_graph_sync_policies_source"),
        Index("ix_graph_sync_policies_library_status", "library_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="CASCADE"),
        nullable=False,
    )
    sync_source_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sync_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    authority_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    stale_after_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=86400)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GraphExternalSyncOperation(Base):
    __tablename__ = "graph_external_sync_operations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('processing','applied','conflicted','failed')",
            name="ck_graph_external_sync_operations_status",
        ),
        CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$' AND item_count BETWEEN 1 AND 100 "
            "AND created_count >= 0 AND updated_count >= 0 AND unchanged_count >= 0 "
            "AND deleted_count >= 0 AND conflict_count >= 0 AND stale_count >= 0",
            name="ck_graph_external_sync_operations_values",
        ),
        CheckConstraint(
            "jsonb_typeof(result_payload) = 'object' "
            "AND octet_length(result_payload::text) <= 131072",
            name="ck_graph_external_sync_operations_result",
        ),
        UniqueConstraint(
            "library_id",
            "sync_source_id",
            "idempotency_key",
            name="uq_graph_external_sync_operations_key",
        ),
        Index(
            "ix_graph_external_sync_operations_scope_created",
            "library_id",
            "sync_source_id",
            "created_at",
        ),
        Index(
            "uq_graph_external_sync_operations_source_event",
            "library_id",
            "sync_source_id",
            "source_event_id",
            unique=True,
            postgresql_where=text("source_event_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sys_libraries.id", ondelete="CASCADE"), nullable=False
    )
    sync_source_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sync_sources.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(String(128))
    snapshot_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="processing")
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unchanged_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deleted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    conflict_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stale_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sys_users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GraphExternalFactMapping(Base):
    __tablename__ = "graph_external_fact_mappings"
    __table_args__ = (
        CheckConstraint("fact_kind IN ('entity','relation')", name="ck_graph_external_mappings_kind"),
        CheckConstraint(
            "lifecycle IN ('active','stale','tombstoned')",
            name="ck_graph_external_mappings_lifecycle",
        ),
        CheckConstraint(
            "payload_hash ~ '^[0-9a-f]{64}$'",
            name="ck_graph_external_mappings_hash",
        ),
        CheckConstraint(
            "((fact_kind = 'entity' AND entity_id IS NOT NULL AND relation_id IS NULL) OR "
            "(fact_kind = 'relation' AND relation_id IS NOT NULL AND entity_id IS NULL))",
            name="ck_graph_external_mappings_target",
        ),
        CheckConstraint(
            "jsonb_typeof(source_locator) = 'object' "
            "AND octet_length(source_locator::text) <= 8192",
            name="ck_graph_external_mappings_locator",
        ),
        UniqueConstraint(
            "library_id",
            "sync_source_id",
            "fact_kind",
            "external_type",
            "external_id",
            name="uq_graph_external_mappings_identity",
        ),
        Index("ix_graph_external_mappings_entity", "entity_id"),
        Index("ix_graph_external_mappings_relation", "relation_id"),
        Index(
            "ix_graph_external_mappings_source_lifecycle",
            "library_id",
            "sync_source_id",
            "lifecycle",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sys_libraries.id", ondelete="CASCADE"), nullable=False
    )
    sync_source_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sync_sources.id", ondelete="CASCADE"), nullable=False
    )
    fact_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    external_type: Mapped[str] = mapped_column(String(128), nullable=False)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("entities.id", ondelete="RESTRICT")
    )
    relation_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("knowledge_relations.id", ondelete="RESTRICT")
    )
    lifecycle: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_version: Mapped[str | None] = mapped_column(String(128))
    source_event_id: Mapped[str | None] = mapped_column(String(128))
    snapshot_id: Mapped[str | None] = mapped_column(String(128))
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("evidence_units.id", ondelete="SET NULL")
    )
    source_locator: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    last_operation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("graph_external_sync_operations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    tombstoned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class GraphExternalSyncConflict(Base):
    __tablename__ = "graph_external_sync_conflicts"
    __table_args__ = (
        CheckConstraint(
            "reason_code IN ('manual_authority','stronger_source_authority',"
            "'equal_authority_divergence','published_fact_change','published_fact_delete',"
            "'relation_dependency','source_snapshot_stale')",
            name="ck_graph_external_sync_conflicts_reason",
        ),
        CheckConstraint(
            "status IN ('open','resolved','dismissed')",
            name="ck_graph_external_sync_conflicts_status",
        ),
        CheckConstraint(
            "incoming_hash ~ '^[0-9a-f]{64}$' "
            "AND (current_hash IS NULL OR current_hash ~ '^[0-9a-f]{64}$')",
            name="ck_graph_external_sync_conflicts_hashes",
        ),
        CheckConstraint(
            "jsonb_typeof(proposal) = 'object' AND octet_length(proposal::text) <= 65536",
            name="ck_graph_external_sync_conflicts_proposal",
        ),
        Index(
            "uq_graph_external_sync_conflicts_open",
            "mapping_id",
            "reason_code",
            "incoming_hash",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index("ix_graph_external_sync_conflicts_scope_status", "library_id", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sys_libraries.id", ondelete="CASCADE"), nullable=False
    )
    sync_source_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("sync_sources.id", ondelete="CASCADE"), nullable=False
    )
    mapping_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("graph_external_fact_mappings.id", ondelete="CASCADE"),
        nullable=False,
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("graph_external_sync_operations.id", ondelete="CASCADE"),
        nullable=False,
    )
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    incoming_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    current_hash: Mapped[str | None] = mapped_column(String(64))
    incoming_authority_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    current_authority_rank: Mapped[int | None] = mapped_column(Integer)
    proposal: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
