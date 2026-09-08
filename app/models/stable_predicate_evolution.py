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

PREDICATE_EVOLUTION_OPERATION_MERGE = "merge"
PREDICATE_EVOLUTION_OPERATION_SPLIT = "split"
PREDICATE_EVOLUTION_OPERATION_REASSIGN = "reassign"

PREDICATE_EVOLUTION_DECISION_PENDING = "pending"
PREDICATE_EVOLUTION_DECISION_APPLIED = "applied"
PREDICATE_EVOLUTION_DECISION_REJECTED = "rejected"
PREDICATE_EVOLUTION_DECISION_STALE = "stale"
PREDICATE_EVOLUTION_DECISION_CANCELLED = "cancelled"
PREDICATE_EVOLUTION_DECISION_SUPERSEDED = "superseded"

PREDICATE_EVOLUTION_SOURCE_PENDING = "pending"
PREDICATE_EVOLUTION_SOURCE_APPLIED = "applied"
PREDICATE_EVOLUTION_SOURCE_HISTORICAL_ONLY = "historical_only"
PREDICATE_EVOLUTION_SOURCE_SUPERSEDED = "superseded"

PREDICATE_EVOLUTION_ASSIGNMENT_PENDING = "pending"
PREDICATE_EVOLUTION_ASSIGNMENT_RESOLVED = "resolved"
PREDICATE_EVOLUTION_ASSIGNMENT_SUPERSEDED = "superseded"


class StablePredicateEvolutionCommand(Base):
    __tablename__ = "stable_predicate_evolution_commands"
    __table_args__ = (
        CheckConstraint(
            "operation_kind IN ('merge','split','reassign')",
            name="ck_stable_predicate_evolution_commands_operation",
        ),
        CheckConstraint(
            "command_identity_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_stable_predicate_evolution_commands_fingerprint",
        ),
        CheckConstraint(
            "jsonb_typeof(command_payload_snapshot) = 'object'",
            name="ck_stable_predicate_evolution_commands_payload",
        ),
        CheckConstraint(
            "contract_version = 'p3_2_stable_predicate_evolution/v1'",
            name="ck_stable_predicate_evolution_commands_contract",
        ),
        UniqueConstraint(
            "id",
            "library_id",
            name="uq_stable_predicate_evolution_commands_id_library",
        ),
        UniqueConstraint(
            "library_id",
            "idempotency_key",
            name="uq_stable_predicate_evolution_commands_idempotency",
        ),
        UniqueConstraint(
            "library_id",
            "command_identity_fingerprint",
            name="uq_stable_predicate_evolution_commands_identity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    command_identity_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    command_payload_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StablePredicateEvolutionDecision(Base):
    __tablename__ = "stable_predicate_evolution_decisions"
    __table_args__ = (
        CheckConstraint(
            "operation_kind IN ('merge','split','reassign')",
            name="ck_stable_predicate_evolution_decisions_operation",
        ),
        CheckConstraint(
            "requested_effect IN ('stage','apply','cancel')",
            name="ck_stable_predicate_evolution_decisions_requested_effect",
        ),
        CheckConstraint(
            "evaluated_outcome IN ('pending','applied','rejected','stale','cancelled')",
            name="ck_stable_predicate_evolution_decisions_outcome",
        ),
        CheckConstraint(
            "lifecycle_status IN ('pending','applied','rejected','stale','cancelled','superseded')",
            name="ck_stable_predicate_evolution_decisions_lifecycle",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_stable_predicate_evolution_decisions_confidence",
        ),
        CheckConstraint(
            "decision_payload_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND expected_precondition_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND observed_precondition_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_stable_predicate_evolution_decisions_fingerprints",
        ),
        CheckConstraint(
            "jsonb_typeof(operation_payload_snapshot) = 'object' "
            "AND jsonb_typeof(evidence_refs) = 'array'",
            name="ck_stable_predicate_evolution_decisions_json",
        ),
        CheckConstraint(
            "actor_type IN ('user','service')",
            name="ck_stable_predicate_evolution_decisions_actor",
        ),
        CheckConstraint(
            "supersedes_decision_id IS NULL OR supersedes_decision_id <> id",
            name="ck_stable_predicate_evolution_decisions_not_self_predecessor",
        ),
        UniqueConstraint(
            "id",
            "library_id",
            "command_id",
            name="uq_stable_predicate_evolution_decisions_owner",
        ),
        UniqueConstraint(
            "library_id",
            "command_id",
            "decision_payload_fingerprint",
            name="uq_stable_predicate_evolution_decisions_payload",
        ),
        ForeignKeyConstraint(
            ["command_id", "library_id"],
            ["stable_predicate_evolution_commands.id", "stable_predicate_evolution_commands.library_id"],
            ondelete="RESTRICT",
            name="fk_stable_predicate_evolution_decisions_command",
        ),
        ForeignKeyConstraint(
            ["supersedes_decision_id", "library_id", "command_id"],
            [
                "stable_predicate_evolution_decisions.id",
                "stable_predicate_evolution_decisions.library_id",
                "stable_predicate_evolution_decisions.command_id",
            ],
            ondelete="RESTRICT",
            name="fk_stable_predicate_evolution_decisions_supersedes",
        ),
        Index(
            "uq_stable_predicate_evolution_decisions_current",
            "library_id",
            "command_id",
            unique=True,
            postgresql_where=text("lifecycle_status <> 'superseded'"),
        ),
        Index(
            "uq_stable_predicate_evolution_decisions_superseded_by",
            "library_id",
            "supersedes_decision_id",
            unique=True,
            postgresql_where=text("supersedes_decision_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    command_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    decision_payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_effect: Mapped[str] = mapped_column(String(32), nullable=False)
    evaluated_outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(32), nullable=False)
    operation_payload_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_text: Mapped[str] = mapped_column(String(512), nullable=False)
    method: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(7, 6), nullable=True)
    evidence_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    expected_precondition_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_precondition_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StablePredicateEvolutionSource(Base):
    __tablename__ = "stable_predicate_evolution_sources"
    __table_args__ = (
        CheckConstraint(
            "evolution_status IN ('pending','applied','historical_only','superseded')",
            name="ck_stable_predicate_evolution_sources_status",
        ),
        CheckConstraint(
            "supersedes_source_transition_id IS NULL OR supersedes_source_transition_id <> id",
            name="ck_stable_predicate_evolution_sources_not_self_predecessor",
        ),
        UniqueConstraint(
            "id",
            "library_id",
            "command_id",
            "source_predicate_id",
            name="uq_stable_predicate_evolution_sources_owner_source",
        ),
        UniqueConstraint(
            "id",
            "library_id",
            "command_id",
            "evolution_decision_id",
            "source_predicate_id",
            name="uq_stable_predicate_evolution_sources_owner_decision_source",
        ),
        UniqueConstraint(
            "library_id",
            "evolution_decision_id",
            "source_predicate_id",
            name="uq_stable_predicate_evolution_sources_decision_source",
        ),
        ForeignKeyConstraint(
            ["evolution_decision_id", "library_id", "command_id"],
            [
                "stable_predicate_evolution_decisions.id",
                "stable_predicate_evolution_decisions.library_id",
                "stable_predicate_evolution_decisions.command_id",
            ],
            ondelete="RESTRICT",
            name="fk_stable_predicate_evolution_sources_decision",
        ),
        ForeignKeyConstraint(
            ["source_predicate_id", "library_id"],
            ["stable_predicate_identities.id", "stable_predicate_identities.library_id"],
            ondelete="RESTRICT",
            name="fk_stable_predicate_evolution_sources_predicate",
        ),
        ForeignKeyConstraint(
            [
                "supersedes_source_transition_id",
                "library_id",
                "command_id",
                "source_predicate_id",
            ],
            [
                "stable_predicate_evolution_sources.id",
                "stable_predicate_evolution_sources.library_id",
                "stable_predicate_evolution_sources.command_id",
                "stable_predicate_evolution_sources.source_predicate_id",
            ],
            ondelete="RESTRICT",
            name="fk_stable_predicate_evolution_sources_supersedes",
        ),
        Index(
            "uq_stable_predicate_evolution_sources_root",
            "library_id",
            "command_id",
            "source_predicate_id",
            unique=True,
            postgresql_where=text("supersedes_source_transition_id IS NULL"),
        ),
        Index(
            "uq_stable_predicate_evolution_sources_superseded_by",
            "library_id",
            "supersedes_source_transition_id",
            unique=True,
            postgresql_where=text("supersedes_source_transition_id IS NOT NULL"),
        ),
        Index(
            "uq_stable_predicate_evolution_sources_live",
            "library_id",
            "source_predicate_id",
            unique=True,
            postgresql_where=text(
                "evolution_status IN ('pending','applied','historical_only')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    command_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    evolution_decision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    source_predicate_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    supersedes_source_transition_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    evolution_status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StablePredicateEvolutionSuccessor(Base):
    __tablename__ = "stable_predicate_evolution_successors"
    __table_args__ = (
        CheckConstraint(
            "target_ref_kind IN ('existing','new')",
            name="ck_stable_predicate_evolution_successors_kind",
        ),
        CheckConstraint(
            "(target_ref_kind = 'existing' AND target_predicate_id = planned_target_predicate_id "
            "AND target_spec_fingerprint IS NULL AND target_spec_snapshot IS NULL) OR "
            "(target_ref_kind = 'new' AND target_spec_fingerprint IS NOT NULL "
            "AND target_spec_snapshot IS NOT NULL)",
            name="ck_stable_predicate_evolution_successors_shape",
        ),
        CheckConstraint(
            "target_spec_snapshot IS NULL OR jsonb_typeof(target_spec_snapshot) = 'object'",
            name="ck_stable_predicate_evolution_successors_spec",
        ),
        UniqueConstraint(
            "id",
            "library_id",
            "command_id",
            "evolution_decision_id",
            name="uq_stable_predicate_evolution_successors_owner_decision",
        ),
        UniqueConstraint(
            "id",
            "library_id",
            "command_id",
            "evolution_decision_id",
            "source_transition_id",
            "source_predicate_id",
            name="uq_stable_predicate_evolution_successors_owner_source",
        ),
        UniqueConstraint(
            "library_id",
            "source_transition_id",
            "planned_target_predicate_id",
            name="uq_stable_predicate_evolution_successors_target",
        ),
        ForeignKeyConstraint(
            ["evolution_decision_id", "library_id", "command_id"],
            [
                "stable_predicate_evolution_decisions.id",
                "stable_predicate_evolution_decisions.library_id",
                "stable_predicate_evolution_decisions.command_id",
            ],
            ondelete="RESTRICT",
            name="fk_stable_predicate_evolution_successors_decision",
        ),
        ForeignKeyConstraint(
            [
                "source_transition_id",
                "library_id",
                "command_id",
                "evolution_decision_id",
                "source_predicate_id",
            ],
            [
                "stable_predicate_evolution_sources.id",
                "stable_predicate_evolution_sources.library_id",
                "stable_predicate_evolution_sources.command_id",
                "stable_predicate_evolution_sources.evolution_decision_id",
                "stable_predicate_evolution_sources.source_predicate_id",
            ],
            ondelete="RESTRICT",
            name="fk_stable_predicate_evolution_successors_source",
        ),
        ForeignKeyConstraint(
            ["target_predicate_id", "library_id"],
            ["stable_predicate_identities.id", "stable_predicate_identities.library_id"],
            ondelete="RESTRICT",
            name="fk_stable_predicate_evolution_successors_target",
        ),
        Index(
            "ix_stable_predicate_evolution_successors_decision",
            "library_id",
            "command_id",
            "evolution_decision_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    command_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    evolution_decision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    source_transition_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    source_predicate_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    target_ref_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    planned_target_predicate_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False
    )
    target_predicate_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    target_spec_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_spec_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StablePredicateMappingEvolutionAssignment(Base):
    __tablename__ = "stable_predicate_mapping_evolution_assignments"
    __table_args__ = (
        CheckConstraint(
            "assignment_state IN ('pending','resolved','superseded')",
            name="ck_stable_predicate_mapping_evolution_assignments_state",
        ),
        CheckConstraint(
            "jsonb_typeof(partition_basis_snapshot) = 'object'",
            name="ck_stable_predicate_mapping_evolution_assignments_basis",
        ),
        CheckConstraint(
            "supersedes_assignment_id IS NULL OR supersedes_assignment_id <> id",
            name="ck_sp_mapping_evolution_assignments_not_self_predecessor",
        ),
        CheckConstraint(
            "assignment_state <> 'pending' OR "
            "(target_successor_id IS NULL AND target_predicate_id IS NULL "
            "AND new_mapping_id IS NULL)",
            name="ck_stable_predicate_mapping_evolution_assignments_pending_shape",
        ),
        CheckConstraint(
            "new_mapping_id IS NULL OR new_mapping_id <> old_mapping_id",
            name="ck_sp_mapping_evolution_assignments_not_self_mapping",
        ),
        UniqueConstraint(
            "id",
            "library_id",
            name="uq_stable_predicate_mapping_evolution_assignments_id_library",
        ),
        UniqueConstraint(
            "id",
            "library_id",
            "command_id",
            "old_mapping_id",
            name="uq_stable_predicate_mapping_evolution_assignments_owner_mapping",
        ),
        UniqueConstraint(
            "library_id",
            "evolution_decision_id",
            "old_mapping_id",
            name="uq_sp_mapping_evolution_assignments_decision_mapping",
        ),
        ForeignKeyConstraint(
            ["evolution_decision_id", "library_id", "command_id"],
            [
                "stable_predicate_evolution_decisions.id",
                "stable_predicate_evolution_decisions.library_id",
                "stable_predicate_evolution_decisions.command_id",
            ],
            ondelete="RESTRICT",
            name="fk_stable_predicate_mapping_evolution_assignments_decision",
        ),
        ForeignKeyConstraint(
            [
                "source_transition_id",
                "library_id",
                "command_id",
                "evolution_decision_id",
                "source_predicate_id",
            ],
            [
                "stable_predicate_evolution_sources.id",
                "stable_predicate_evolution_sources.library_id",
                "stable_predicate_evolution_sources.command_id",
                "stable_predicate_evolution_sources.evolution_decision_id",
                "stable_predicate_evolution_sources.source_predicate_id",
            ],
            ondelete="RESTRICT",
            name="fk_stable_predicate_mapping_evolution_assignments_source",
        ),
        ForeignKeyConstraint(
            ["old_mapping_id", "library_id"],
            ["stable_predicate_mappings.id", "stable_predicate_mappings.library_id"],
            ondelete="RESTRICT",
            name="fk_stable_predicate_mapping_evolution_assignments_old_mapping",
        ),
        ForeignKeyConstraint(
            ["relation_type_id", "library_id"],
            ["relation_types.id", "relation_types.library_id"],
            ondelete="RESTRICT",
            name="fk_stable_predicate_mapping_evolution_assignments_relation_type",
        ),
        ForeignKeyConstraint(
            ["source_predicate_id", "library_id"],
            ["stable_predicate_identities.id", "stable_predicate_identities.library_id"],
            ondelete="RESTRICT",
            name="fk_sp_mapping_evolution_assignments_source_predicate",
        ),
        ForeignKeyConstraint(
            [
                "target_successor_id",
                "library_id",
                "command_id",
                "evolution_decision_id",
                "source_transition_id",
                "source_predicate_id",
            ],
            [
                "stable_predicate_evolution_successors.id",
                "stable_predicate_evolution_successors.library_id",
                "stable_predicate_evolution_successors.command_id",
                "stable_predicate_evolution_successors.evolution_decision_id",
                "stable_predicate_evolution_successors.source_transition_id",
                "stable_predicate_evolution_successors.source_predicate_id",
            ],
            ondelete="RESTRICT",
            name="fk_sp_mapping_evolution_assignments_target_successor",
        ),
        ForeignKeyConstraint(
            ["target_predicate_id", "library_id"],
            ["stable_predicate_identities.id", "stable_predicate_identities.library_id"],
            ondelete="RESTRICT",
            name="fk_sp_mapping_evolution_assignments_target_predicate",
        ),
        ForeignKeyConstraint(
            ["supersedes_assignment_id", "library_id", "command_id", "old_mapping_id"],
            [
                "stable_predicate_mapping_evolution_assignments.id",
                "stable_predicate_mapping_evolution_assignments.library_id",
                "stable_predicate_mapping_evolution_assignments.command_id",
                "stable_predicate_mapping_evolution_assignments.old_mapping_id",
            ],
            ondelete="RESTRICT",
            name="fk_stable_predicate_mapping_evolution_assignments_supersedes",
        ),
        ForeignKeyConstraint(
            ["new_mapping_id", "library_id"],
            ["stable_predicate_mappings.id", "stable_predicate_mappings.library_id"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            name="fk_stable_predicate_mapping_evolution_assignments_new_mapping",
        ),
        Index(
            "uq_stable_predicate_mapping_evolution_assignments_root",
            "library_id",
            "command_id",
            "old_mapping_id",
            unique=True,
            postgresql_where=text("supersedes_assignment_id IS NULL"),
        ),
        Index(
            "uq_stable_predicate_mapping_evolution_assignments_superseded_by",
            "library_id",
            "supersedes_assignment_id",
            unique=True,
            postgresql_where=text("supersedes_assignment_id IS NOT NULL"),
        ),
        Index(
            "uq_stable_predicate_mapping_evolution_assignments_live",
            "library_id",
            "old_mapping_id",
            unique=True,
            postgresql_where=text("assignment_state IN ('pending','resolved')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    command_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    evolution_decision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    source_transition_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    old_mapping_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    relation_type_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    source_predicate_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    target_successor_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    target_predicate_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    new_mapping_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    assignment_state: Mapped[str] = mapped_column(String(32), nullable=False)
    partition_basis_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_assignment_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = [
    "PREDICATE_EVOLUTION_ASSIGNMENT_PENDING",
    "PREDICATE_EVOLUTION_ASSIGNMENT_RESOLVED",
    "PREDICATE_EVOLUTION_ASSIGNMENT_SUPERSEDED",
    "PREDICATE_EVOLUTION_DECISION_APPLIED",
    "PREDICATE_EVOLUTION_DECISION_CANCELLED",
    "PREDICATE_EVOLUTION_DECISION_PENDING",
    "PREDICATE_EVOLUTION_DECISION_REJECTED",
    "PREDICATE_EVOLUTION_DECISION_STALE",
    "PREDICATE_EVOLUTION_DECISION_SUPERSEDED",
    "PREDICATE_EVOLUTION_OPERATION_MERGE",
    "PREDICATE_EVOLUTION_OPERATION_REASSIGN",
    "PREDICATE_EVOLUTION_OPERATION_SPLIT",
    "PREDICATE_EVOLUTION_SOURCE_APPLIED",
    "PREDICATE_EVOLUTION_SOURCE_HISTORICAL_ONLY",
    "PREDICATE_EVOLUTION_SOURCE_PENDING",
    "PREDICATE_EVOLUTION_SOURCE_SUPERSEDED",
    "StablePredicateEvolutionCommand",
    "StablePredicateEvolutionDecision",
    "StablePredicateEvolutionSource",
    "StablePredicateEvolutionSuccessor",
    "StablePredicateMappingEvolutionAssignment",
]
