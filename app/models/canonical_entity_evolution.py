from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

EVOLUTION_OPERATION_MERGE = "merge"
EVOLUTION_OPERATION_SPLIT = "split"
EVOLUTION_OPERATION_REASSIGN = "reassign"

EVOLUTION_DECISION_PENDING = "pending"
EVOLUTION_DECISION_APPLIED = "applied"
EVOLUTION_DECISION_REJECTED = "rejected"
EVOLUTION_DECISION_STALE = "stale"
EVOLUTION_DECISION_CANCELLED = "cancelled"
EVOLUTION_DECISION_SUPERSEDED = "superseded"

EVOLUTION_SOURCE_PENDING = "pending"
EVOLUTION_SOURCE_APPLIED = "applied"
EVOLUTION_SOURCE_SUPERSEDED = "superseded"
EVOLUTION_SOURCE_HISTORICAL_ONLY = "historical_only"

EVOLUTION_ASSIGNMENT_RESOLVED = "resolved"
EVOLUTION_ASSIGNMENT_PENDING = "pending"
EVOLUTION_ASSIGNMENT_SUPERSEDED = "superseded"


class CanonicalEntityEvolutionCommand(Base):
    __tablename__ = "canonical_entity_evolution_commands"
    __table_args__ = (
        CheckConstraint("operation_kind IN ('merge','split','reassign')", name="ck_canonical_entity_evolution_commands_operation"),
        CheckConstraint("command_identity_fingerprint ~ '^[0-9a-f]{64}$'", name="ck_canonical_entity_evolution_commands_identity_fingerprint"),
        CheckConstraint(
            "jsonb_typeof(source_identity_snapshot) = 'object' AND jsonb_typeof(command_scope_snapshot) = 'object'",
            name="ck_canonical_entity_evolution_commands_snapshots",
        ),
        UniqueConstraint("id", "library_id", name="uq_canonical_entity_evolution_commands_id_library"),
        UniqueConstraint("library_id", "idempotency_key", name="uq_canonical_entity_evolution_commands_idempotency"),
        UniqueConstraint("library_id", "command_identity_fingerprint", name="uq_canonical_entity_evolution_commands_identity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("sys_libraries.id", ondelete="RESTRICT"), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    command_identity_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_identity_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    command_scope_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CanonicalEntityEvolutionDecision(Base):
    __tablename__ = "canonical_entity_evolution_decisions"
    __table_args__ = (
        CheckConstraint("operation_kind IN ('merge','split','reassign')", name="ck_canonical_entity_evolution_decisions_operation"),
        CheckConstraint("evaluated_outcome IN ('pending','applied','rejected','stale','cancelled')", name="ck_canonical_entity_evolution_decisions_outcome"),
        CheckConstraint("lifecycle_status IN ('pending','applied','rejected','stale','cancelled','superseded')", name="ck_canonical_entity_evolution_decisions_lifecycle"),
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_canonical_entity_evolution_decisions_confidence"),
        CheckConstraint(
            "decision_payload_fingerprint ~ '^[0-9a-f]{64}$' AND precondition_fingerprint ~ '^[0-9a-f]{64}$' AND observed_precondition_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_canonical_entity_evolution_decisions_fingerprints",
        ),
        CheckConstraint(
            "jsonb_typeof(successor_detail_snapshot) = 'object' AND jsonb_typeof(projection_assignment_snapshot) IN ('array','object') AND jsonb_typeof(evidence_refs) = 'array' AND (split_partition_snapshot IS NULL OR jsonb_typeof(split_partition_snapshot) = 'array')",
            name="ck_canonical_entity_evolution_decisions_snapshots",
        ),
        CheckConstraint("actor_type IN ('user','service')", name="ck_canonical_entity_evolution_decisions_actor_type"),
        UniqueConstraint("id", "library_id", name="uq_canonical_entity_evolution_decisions_id_library"),
        UniqueConstraint("id", "library_id", "command_id", name="uq_canonical_entity_evolution_decisions_id_library_command"),
        UniqueConstraint("library_id", "command_id", "decision_payload_fingerprint", name="uq_canonical_entity_evolution_decisions_payload"),
        UniqueConstraint("library_id", "supersedes_decision_id", name="uq_canonical_entity_evolution_decisions_supersedes"),
        ForeignKeyConstraint(
            ["command_id", "library_id"],
            ["canonical_entity_evolution_commands.id", "canonical_entity_evolution_commands.library_id"],
            ondelete="RESTRICT", name="fk_canonical_entity_evolution_decisions_command",
        ),
        ForeignKeyConstraint(
            ["supersedes_decision_id", "library_id", "command_id"],
            ["canonical_entity_evolution_decisions.id", "canonical_entity_evolution_decisions.library_id", "canonical_entity_evolution_decisions.command_id"],
            ondelete="RESTRICT", name="fk_canonical_entity_evolution_decisions_supersedes",
        ),
        Index("uq_canonical_entity_evolution_decisions_pending", "library_id", "command_id", unique=True, postgresql_where=text("lifecycle_status = 'pending'")),
        Index("uq_canonical_entity_evolution_decisions_applied", "library_id", "command_id", unique=True, postgresql_where=text("lifecycle_status = 'applied'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    command_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    decision_payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    evaluated_outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(32), nullable=False)
    successor_detail_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    split_partition_snapshot: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    projection_assignment_snapshot: Mapped[list[dict[str, Any]] | dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_text: Mapped[str] = mapped_column(String(512), nullable=False)
    method: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(7, 6), nullable=True)
    evidence_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    precondition_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_precondition_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_decision_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CanonicalEntityEvolutionSource(Base):
    __tablename__ = "canonical_entity_evolution_sources"
    __table_args__ = (
        CheckConstraint("resolution_state IN ('pending','applied','superseded','historical_only')", name="ck_canonical_entity_evolution_sources_state"),
        CheckConstraint("supersedes_source_transition_id IS NULL OR supersedes_source_transition_id <> id", name="ck_canonical_entity_evolution_sources_not_self_predecessor"),
        UniqueConstraint("id", "library_id", "command_id", "source_canonical_entity_id", name="uq_canonical_entity_evolution_sources_id_library_command_source"),
        UniqueConstraint("id", "library_id", "command_id", "evolution_decision_id", name="uq_canonical_entity_evolution_sources_id_library_command_decision"),
        UniqueConstraint("library_id", "evolution_decision_id", "source_canonical_entity_id", name="uq_canonical_entity_evolution_sources_decision_source"),
        ForeignKeyConstraint(
            ["evolution_decision_id", "library_id", "command_id"],
            ["canonical_entity_evolution_decisions.id", "canonical_entity_evolution_decisions.library_id", "canonical_entity_evolution_decisions.command_id"],
            ondelete="RESTRICT", name="fk_canonical_entity_evolution_sources_decision",
        ),
        ForeignKeyConstraint(
            ["source_canonical_entity_id", "library_id"], ["canonical_entities.id", "canonical_entities.library_id"],
            ondelete="RESTRICT", name="fk_canonical_entity_evolution_sources_canonical",
        ),
        ForeignKeyConstraint(
            ["supersedes_source_transition_id", "library_id", "command_id", "source_canonical_entity_id"],
            ["canonical_entity_evolution_sources.id", "canonical_entity_evolution_sources.library_id", "canonical_entity_evolution_sources.command_id", "canonical_entity_evolution_sources.source_canonical_entity_id"],
            ondelete="RESTRICT", name="fk_canonical_entity_evolution_sources_supersedes",
        ),
        Index("uq_canonical_entity_evolution_sources_command_root", "library_id", "command_id", "source_canonical_entity_id", unique=True, postgresql_where=text("supersedes_source_transition_id IS NULL")),
        Index("uq_canonical_entity_evolution_sources_superseded_by", "library_id", "supersedes_source_transition_id", unique=True, postgresql_where=text("supersedes_source_transition_id IS NOT NULL")),
        Index("uq_canonical_entity_evolution_sources_current", "library_id", "source_canonical_entity_id", unique=True, postgresql_where=text("resolution_state IN ('pending','applied')")),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    command_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    evolution_decision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    source_canonical_entity_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    supersedes_source_transition_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    resolution_state: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CanonicalEntityEvolutionSuccessor(Base):
    __tablename__ = "canonical_entity_evolution_successors"
    __table_args__ = (
        CheckConstraint("target_ref_kind IN ('existing','new')", name="ck_canonical_entity_evolution_successors_target_kind"),
        CheckConstraint(
            "(target_ref_kind = 'existing' AND target_canonical_entity_id IS NOT NULL AND target_spec_snapshot IS NULL) OR (target_ref_kind = 'new' AND target_spec_snapshot IS NOT NULL)",
            name="ck_canonical_entity_evolution_successors_target_shape",
        ),
        CheckConstraint("target_spec_snapshot IS NULL OR jsonb_typeof(target_spec_snapshot) = 'object'", name="ck_canonical_entity_evolution_successors_target_spec"),
        UniqueConstraint("id", "library_id", name="uq_canonical_entity_evolution_successors_id_library"),
        UniqueConstraint("id", "library_id", "command_id", "evolution_decision_id", name="uq_canonical_entity_evolution_successors_id_library_command_decision"),
        UniqueConstraint("library_id", "source_transition_id", "target_ref_kind", "target_ref_key", name="uq_canonical_entity_evolution_successors_target_slot"),
        ForeignKeyConstraint(
            ["evolution_decision_id", "library_id", "command_id"],
            ["canonical_entity_evolution_decisions.id", "canonical_entity_evolution_decisions.library_id", "canonical_entity_evolution_decisions.command_id"],
            ondelete="RESTRICT", name="fk_canonical_entity_evolution_successors_decision",
        ),
        ForeignKeyConstraint(
            ["source_transition_id", "library_id", "command_id", "evolution_decision_id"],
            ["canonical_entity_evolution_sources.id", "canonical_entity_evolution_sources.library_id", "canonical_entity_evolution_sources.command_id", "canonical_entity_evolution_sources.evolution_decision_id"],
            ondelete="RESTRICT", name="fk_canonical_entity_evolution_successors_source",
        ),
        ForeignKeyConstraint(
            ["target_canonical_entity_id", "library_id"], ["canonical_entities.id", "canonical_entities.library_id"],
            ondelete="RESTRICT", name="fk_canonical_entity_evolution_successors_target",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    command_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    evolution_decision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    source_transition_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    target_ref_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_ref_key: Mapped[str] = mapped_column(String(256), nullable=False)
    target_canonical_entity_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    target_spec_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CanonicalEntityProjectionAssignment(Base):
    __tablename__ = "canonical_entity_projection_assignments"
    __table_args__ = (
        CheckConstraint("assignment_state IN ('resolved','pending','superseded')", name="ck_canonical_entity_projection_assignments_state"),
        CheckConstraint(
            "target_canonical_entity_id IS NULL OR target_canonical_entity_id <> from_canonical_entity_id",
            name="ck_canonical_entity_projection_assignments_not_self_target",
        ),
        CheckConstraint(
            "assignment_state = 'superseded' OR "
            "(assignment_state = 'pending' AND target_successor_id IS NULL AND target_canonical_entity_id IS NULL) OR "
            "(assignment_state = 'resolved' AND target_canonical_entity_id IS NOT NULL)",
            name="ck_canonical_entity_projection_assignments_target_state",
        ),
        CheckConstraint("jsonb_typeof(partition_basis_snapshot) = 'object'", name="ck_canonical_entity_projection_assignments_basis"),
        CheckConstraint("supersedes_assignment_id IS NULL OR supersedes_assignment_id <> id", name="ck_canonical_entity_projection_assignments_not_self_predecessor"),
        UniqueConstraint("id", "library_id", "entity_id", name="uq_canonical_entity_projection_assignments_id_library_entity"),
        UniqueConstraint("id", "library_id", "command_id", "entity_id", name="uq_canonical_entity_projection_assignments_id_library_command_entity"),
        UniqueConstraint("library_id", "evolution_decision_id", "entity_id", name="uq_canonical_entity_projection_assignments_decision_entity"),
        ForeignKeyConstraint(
            ["evolution_decision_id", "library_id", "command_id"],
            ["canonical_entity_evolution_decisions.id", "canonical_entity_evolution_decisions.library_id", "canonical_entity_evolution_decisions.command_id"],
            ondelete="RESTRICT", name="fk_canonical_entity_projection_assignments_decision",
        ),
        ForeignKeyConstraint(["entity_id", "library_id"], ["entities.id", "entities.library_id"], ondelete="RESTRICT", name="fk_canonical_entity_projection_assignments_entity"),
        ForeignKeyConstraint(["from_canonical_entity_id", "library_id"], ["canonical_entities.id", "canonical_entities.library_id"], ondelete="RESTRICT", name="fk_canonical_entity_projection_assignments_from"),
        ForeignKeyConstraint(["target_canonical_entity_id", "library_id"], ["canonical_entities.id", "canonical_entities.library_id"], ondelete="RESTRICT", name="fk_canonical_entity_projection_assignments_target"),
        ForeignKeyConstraint(
            ["supersedes_assignment_id", "library_id", "command_id", "entity_id"],
            ["canonical_entity_projection_assignments.id", "canonical_entity_projection_assignments.library_id", "canonical_entity_projection_assignments.command_id", "canonical_entity_projection_assignments.entity_id"],
            ondelete="RESTRICT", name="fk_canonical_entity_projection_assignments_supersedes",
        ),
        ForeignKeyConstraint(
            ["target_successor_id", "library_id", "command_id", "evolution_decision_id"],
            ["canonical_entity_evolution_successors.id", "canonical_entity_evolution_successors.library_id", "canonical_entity_evolution_successors.command_id", "canonical_entity_evolution_successors.evolution_decision_id"],
            ondelete="RESTRICT", name="fk_canonical_entity_projection_assignments_successor",
        ),
        Index("uq_canonical_entity_projection_assignments_command_root", "library_id", "command_id", "entity_id", unique=True, postgresql_where=text("supersedes_assignment_id IS NULL")),
        Index("uq_canonical_entity_projection_assignments_superseded_by", "library_id", "supersedes_assignment_id", unique=True, postgresql_where=text("supersedes_assignment_id IS NOT NULL")),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    command_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    evolution_decision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    supersedes_assignment_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    from_canonical_entity_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    target_successor_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    target_canonical_entity_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    assignment_state: Mapped[str] = mapped_column(String(32), nullable=False)
    partition_basis_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
