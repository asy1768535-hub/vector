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
EVOLUTION_DECISION_SUPERSEDED = "superseded"

EVOLUTION_SOURCE_PENDING = "pending"
EVOLUTION_SOURCE_APPLIED = "applied"
EVOLUTION_SOURCE_SUPERSEDED = "superseded"
EVOLUTION_SOURCE_HISTORICAL_ONLY = "historical_only"

EVOLUTION_ASSIGNMENT_RESOLVED = "resolved"
EVOLUTION_ASSIGNMENT_PENDING = "pending"
EVOLUTION_ASSIGNMENT_REJECTED = "rejected"


class CanonicalEntityEvolutionCommand(Base):
    __tablename__ = "canonical_entity_evolution_commands"
    __table_args__ = (
        CheckConstraint(
            "operation_kind IN ('merge','split','reassign')",
            name="ck_canonical_entity_evolution_commands_operation",
        ),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_canonical_entity_evolution_commands_fingerprint",
        ),
        UniqueConstraint("id", "library_id", name="uq_canonical_entity_evolution_commands_id_library"),
        UniqueConstraint(
            "library_id", "idempotency_key", name="uq_canonical_entity_evolution_commands_idempotency"
        ),
        UniqueConstraint(
            "library_id", "supersedes_command_id", name="uq_canonical_entity_evolution_commands_supersedes"
        ),
        ForeignKeyConstraint(
            ["supersedes_command_id", "library_id"],
            ["canonical_entity_evolution_commands.id", "canonical_entity_evolution_commands.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_evolution_commands_supersedes",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_command_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CanonicalEntityEvolutionDecision(Base):
    __tablename__ = "canonical_entity_evolution_decisions"
    __table_args__ = (
        CheckConstraint(
            "operation_kind IN ('merge','split','reassign')",
            name="ck_canonical_entity_evolution_decisions_operation",
        ),
        CheckConstraint(
            "lifecycle_status IN ('pending','applied','rejected','stale','superseded')",
            name="ck_canonical_entity_evolution_decisions_lifecycle",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_canonical_entity_evolution_decisions_confidence",
        ),
        CheckConstraint(
            "precondition_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_canonical_entity_evolution_decisions_precondition",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array'",
            name="ck_canonical_entity_evolution_decisions_evidence",
        ),
        UniqueConstraint("id", "library_id", name="uq_canonical_entity_evolution_decisions_id_library"),
        UniqueConstraint(
            "id", "library_id", "command_id", name="uq_canonical_entity_evolution_decisions_id_library_command"
        ),
        UniqueConstraint(
            "library_id", "supersedes_decision_id", name="uq_canonical_entity_evolution_decisions_supersedes"
        ),
        ForeignKeyConstraint(
            ["command_id", "library_id"],
            ["canonical_entity_evolution_commands.id", "canonical_entity_evolution_commands.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_evolution_decisions_command",
        ),
        ForeignKeyConstraint(
            ["supersedes_decision_id", "library_id", "command_id"],
            [
                "canonical_entity_evolution_decisions.id",
                "canonical_entity_evolution_decisions.library_id",
                "canonical_entity_evolution_decisions.command_id",
            ],
            ondelete="RESTRICT",
            name="fk_canonical_entity_evolution_decisions_supersedes",
        ),
        Index(
            "uq_canonical_entity_evolution_decisions_pending",
            "library_id",
            "command_id",
            unique=True,
            postgresql_where=text("lifecycle_status = 'pending'"),
        ),
        Index(
            "uq_canonical_entity_evolution_decisions_applied",
            "library_id",
            "command_id",
            unique=True,
            postgresql_where=text("lifecycle_status = 'applied'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    command_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_text: Mapped[str] = mapped_column(String(512), nullable=False)
    method: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    evidence_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    precondition_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_decision_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CanonicalEntityEvolutionSource(Base):
    __tablename__ = "canonical_entity_evolution_sources"
    __table_args__ = (
        CheckConstraint(
            "resolution_state IN ('pending','applied','superseded','historical_only')",
            name="ck_canonical_entity_evolution_sources_state",
        ),
        UniqueConstraint(
            "library_id", "evolution_decision_id", "source_canonical_entity_id",
            name="uq_canonical_entity_evolution_sources_decision_source",
        ),
        UniqueConstraint("id", "library_id", name="uq_canonical_entity_evolution_sources_id_library"),
        ForeignKeyConstraint(
            ["evolution_decision_id", "library_id"],
            ["canonical_entity_evolution_decisions.id", "canonical_entity_evolution_decisions.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_evolution_sources_decision",
        ),
        ForeignKeyConstraint(
            ["source_canonical_entity_id", "library_id"],
            ["canonical_entities.id", "canonical_entities.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_evolution_sources_canonical",
        ),
        Index(
            "uq_canonical_entity_evolution_sources_current",
            "library_id",
            "source_canonical_entity_id",
            unique=True,
            postgresql_where=text("resolution_state IN ('pending','applied')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    evolution_decision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    source_canonical_entity_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    resolution_state: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CanonicalEntityEvolutionSuccessor(Base):
    __tablename__ = "canonical_entity_evolution_successors"
    __table_args__ = (
        CheckConstraint(
            "target_spec_snapshot IS NULL OR jsonb_typeof(target_spec_snapshot) = 'object'",
            name="ck_canonical_entity_evolution_successors_target_spec",
        ),
        UniqueConstraint(
            "library_id", "source_transition_id", "target_canonical_entity_id",
            name="uq_canonical_entity_evolution_successors_target",
        ),
        ForeignKeyConstraint(
            ["source_transition_id", "library_id"],
            ["canonical_entity_evolution_sources.id", "canonical_entity_evolution_sources.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_evolution_successors_source",
        ),
        ForeignKeyConstraint(
            ["target_canonical_entity_id", "library_id"],
            ["canonical_entities.id", "canonical_entities.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_evolution_successors_target",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    source_transition_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    target_canonical_entity_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    target_spec_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CanonicalEntityProjectionAssignment(Base):
    __tablename__ = "canonical_entity_projection_assignments"
    __table_args__ = (
        CheckConstraint(
            "assignment_state IN ('resolved','pending','rejected')",
            name="ck_canonical_entity_projection_assignments_state",
        ),
        CheckConstraint(
            "jsonb_typeof(partition_basis_snapshot) = 'object'",
            name="ck_canonical_entity_projection_assignments_basis",
        ),
        UniqueConstraint(
            "library_id", "evolution_decision_id", "entity_id",
            name="uq_canonical_entity_projection_assignments_decision_entity",
        ),
        ForeignKeyConstraint(
            ["evolution_decision_id", "library_id"],
            ["canonical_entity_evolution_decisions.id", "canonical_entity_evolution_decisions.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_projection_assignments_decision",
        ),
        ForeignKeyConstraint(
            ["entity_id", "library_id"],
            ["entities.id", "entities.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_projection_assignments_entity",
        ),
        ForeignKeyConstraint(
            ["from_canonical_entity_id", "library_id"],
            ["canonical_entities.id", "canonical_entities.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_projection_assignments_from",
        ),
        ForeignKeyConstraint(
            ["target_canonical_entity_id", "library_id"],
            ["canonical_entities.id", "canonical_entities.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_projection_assignments_target",
        ),
        ForeignKeyConstraint(
            ["previous_entity_resolution_decision_id", "library_id"],
            ["entity_resolution_decisions.id", "entity_resolution_decisions.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_projection_assignments_previous_decision",
        ),
        ForeignKeyConstraint(
            ["new_entity_resolution_decision_id", "library_id"],
            ["entity_resolution_decisions.id", "entity_resolution_decisions.library_id"],
            ondelete="RESTRICT",
            name="fk_canonical_entity_projection_assignments_new_decision",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    evolution_decision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    from_canonical_entity_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    target_canonical_entity_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    assignment_state: Mapped[str] = mapped_column(String(32), nullable=False)
    partition_basis_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_entity_resolution_decision_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    new_entity_resolution_decision_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
