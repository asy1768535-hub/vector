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

FACT_RECONCILIATION_OUTCOMES = ("pending", "applied", "rejected", "stale", "cancelled")
FACT_RECONCILIATION_DECISION_STATUSES = (*FACT_RECONCILIATION_OUTCOMES, "superseded")


class FactReconciliationCommand(Base):
    __tablename__ = "fact_reconciliation_commands"
    __table_args__ = (
        CheckConstraint(
            "operation_kind = 'reconcile'",
            name="ck_fact_reconciliation_commands_operation",
        ),
        CheckConstraint(
            "command_identity_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_fact_reconciliation_commands_fingerprint",
        ),
        CheckConstraint(
            "jsonb_typeof(command_payload_snapshot) = 'object' "
            "AND octet_length(command_payload_snapshot::text) <= 262144",
            name="ck_fact_reconciliation_commands_payload",
        ),
        CheckConstraint(
            "contract_version = 'p3_3_fact_reconciliation/v1'",
            name="ck_fact_reconciliation_commands_contract",
        ),
        UniqueConstraint("id", "library_id", name="uq_fact_reconciliation_commands_id_library"),
        UniqueConstraint(
            "library_id",
            "idempotency_key",
            name="uq_fact_reconciliation_commands_idempotency",
        ),
        UniqueConstraint(
            "library_id",
            "command_identity_fingerprint",
            name="uq_fact_reconciliation_commands_identity",
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
    command_identity_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(32), nullable=False, server_default="reconcile")
    command_payload_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FactReconciliationDecision(Base):
    __tablename__ = "fact_reconciliation_decisions"
    __table_args__ = (
        CheckConstraint(
            "operation_kind = 'reconcile'",
            name="ck_fact_reconciliation_decisions_operation",
        ),
        CheckConstraint(
            "requested_effect IN ('stage','apply','cancel')",
            name="ck_fact_reconciliation_decisions_effect",
        ),
        CheckConstraint(
            "evaluated_outcome IN ('pending','applied','rejected','stale','cancelled')",
            name="ck_fact_reconciliation_decisions_outcome",
        ),
        CheckConstraint(
            "lifecycle_status IN ('pending','applied','rejected','stale','cancelled','superseded')",
            name="ck_fact_reconciliation_decisions_lifecycle",
        ),
        CheckConstraint(
            "decision_payload_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_fact_reconciliation_decisions_payload_fingerprint",
        ),
        CheckConstraint(
            "expected_precondition_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_fact_reconciliation_decisions_expected_fingerprint",
        ),
        CheckConstraint(
            "observed_precondition_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_fact_reconciliation_decisions_observed_fingerprint",
        ),
        CheckConstraint(
            "jsonb_typeof(operation_payload_snapshot) = 'object' "
            "AND octet_length(operation_payload_snapshot::text) <= 262144",
            name="ck_fact_reconciliation_decisions_payload",
        ),
        CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array' "
            "AND octet_length(evidence_refs::text) <= 65536",
            name="ck_fact_reconciliation_decisions_evidence_refs",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_fact_reconciliation_decisions_confidence",
        ),
        CheckConstraint(
            "lifecycle_status = evaluated_outcome OR lifecycle_status = 'superseded'",
            name="ck_fact_reconciliation_decisions_status_shape",
        ),
        ForeignKeyConstraint(
            ["command_id", "library_id"],
            ["fact_reconciliation_commands.id", "fact_reconciliation_commands.library_id"],
            ondelete="RESTRICT",
            name="fk_fact_reconciliation_decisions_command",
        ),
        UniqueConstraint("id", "library_id", "command_id", name="uq_fact_reconciliation_decisions_owner"),
        UniqueConstraint(
            "library_id",
            "command_id",
            "decision_payload_fingerprint",
            name="uq_fact_reconciliation_decisions_payload",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    command_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    decision_payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_kind: Mapped[str] = mapped_column(String(32), nullable=False, server_default="reconcile")
    requested_effect: Mapped[str] = mapped_column(String(16), nullable=False)
    evaluated_outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(16), nullable=False)
    operation_payload_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expected_precondition_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_precondition_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason_text: Mapped[str] = mapped_column(String(1024), nullable=False)
    method: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
    evidence_refs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    supersedes_decision_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    actor_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(256), nullable=False)
    request_id: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FactReconciliationSource(Base):
    __tablename__ = "fact_reconciliation_sources"
    __table_args__ = (
        CheckConstraint(
            "resolution_state IN ('pending','applied','superseded','historical_only')",
            name="ck_fact_reconciliation_sources_state",
        ),
        CheckConstraint(
            "supersedes_source_transition_id IS NULL OR supersedes_source_transition_id <> id",
            name="ck_fact_reconciliation_sources_not_self_predecessor",
        ),
        ForeignKeyConstraint(
            ["evolution_decision_id", "library_id", "command_id"],
            [
                "fact_reconciliation_decisions.id",
                "fact_reconciliation_decisions.library_id",
                "fact_reconciliation_decisions.command_id",
            ],
            ondelete="RESTRICT",
            name="fk_fact_reconciliation_sources_decision",
        ),
        ForeignKeyConstraint(
            ["source_logical_fact_id", "library_id"],
            ["logical_facts.id", "logical_facts.library_id"],
            ondelete="RESTRICT",
            name="fk_fact_reconciliation_sources_logical_fact",
        ),
        UniqueConstraint(
            "id",
            "library_id",
            "command_id",
            "source_logical_fact_id",
            name="uq_fact_reconciliation_sources_owner_source",
        ),
        UniqueConstraint(
            "id",
            "library_id",
            "command_id",
            "evolution_decision_id",
            "source_logical_fact_id",
            name="uq_fact_reconciliation_sources_owner_decision_source",
        ),
        UniqueConstraint(
            "library_id",
            "evolution_decision_id",
            "source_logical_fact_id",
            name="uq_fact_reconciliation_sources_decision_source",
        ),
        Index(
            "uq_fact_reconciliation_sources_root",
            "library_id",
            "command_id",
            "source_logical_fact_id",
            unique=True,
            postgresql_where=text("supersedes_source_transition_id IS NULL"),
        ),
        Index(
            "uq_fact_reconciliation_sources_superseded_by",
            "library_id",
            "supersedes_source_transition_id",
            unique=True,
            postgresql_where=text("supersedes_source_transition_id IS NOT NULL"),
        ),
        Index(
            "uq_fact_reconciliation_sources_live",
            "library_id",
            "source_logical_fact_id",
            unique=True,
            postgresql_where=text("resolution_state IN ('pending','applied')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    command_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    evolution_decision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    source_logical_fact_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    supersedes_source_transition_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    resolution_state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FactReconciliationTargetSlot(Base):
    __tablename__ = "fact_reconciliation_target_slots"
    __table_args__ = (
        CheckConstraint(
            "target_ref_kind IN ('existing','new')",
            name="ck_fact_reconciliation_target_slots_kind",
        ),
        CheckConstraint(
            "target_identity_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_fact_reconciliation_target_slots_fingerprint",
        ),
        CheckConstraint(
            "jsonb_typeof(target_spec_snapshot) = 'object' "
            "AND octet_length(target_spec_snapshot::text) <= 65536",
            name="ck_fact_reconciliation_target_slots_spec",
        ),
        CheckConstraint(
            "slot_state IN ('pending','applied','superseded')",
            name="ck_fact_reconciliation_target_slots_state",
        ),
        CheckConstraint(
            "supersedes_target_slot_id IS NULL OR supersedes_target_slot_id <> id",
            name="ck_fact_reconciliation_target_slots_not_self_predecessor",
        ),
        CheckConstraint(
            "(target_ref_kind = 'existing' AND existing_logical_fact_id IS NOT NULL "
            "AND planned_target_logical_fact_id IS NULL "
            "AND target_logical_fact_id = existing_logical_fact_id) "
            "OR (target_ref_kind = 'new' AND existing_logical_fact_id IS NULL "
            "AND planned_target_logical_fact_id IS NOT NULL "
            "AND (target_logical_fact_id IS NULL "
            "OR target_logical_fact_id = planned_target_logical_fact_id))",
            name="ck_fact_reconciliation_target_slots_shape",
        ),
        ForeignKeyConstraint(
            ["evolution_decision_id", "library_id", "command_id"],
            [
                "fact_reconciliation_decisions.id",
                "fact_reconciliation_decisions.library_id",
                "fact_reconciliation_decisions.command_id",
            ],
            ondelete="RESTRICT",
            name="fk_fact_reconciliation_target_slots_decision",
        ),
        ForeignKeyConstraint(
            ["existing_logical_fact_id", "library_id"],
            ["logical_facts.id", "logical_facts.library_id"],
            ondelete="RESTRICT",
            name="fk_fact_reconciliation_target_slots_existing_fact",
        ),
        ForeignKeyConstraint(
            ["target_logical_fact_id", "library_id"],
            ["logical_facts.id", "logical_facts.library_id"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            name="fk_fact_reconciliation_target_slots_target_fact",
        ),
        UniqueConstraint(
            "id",
            "library_id",
            name="uq_fact_reconciliation_target_slots_id_library",
        ),
        UniqueConstraint(
            "id",
            "library_id",
            "command_id",
            "target_key",
            name="uq_fact_reconciliation_target_slots_owner_key",
        ),
        UniqueConstraint(
            "id",
            "library_id",
            "command_id",
            "evolution_decision_id",
            "target_key",
            name="uq_fact_reconciliation_target_slots_owner_decision_key",
        ),
        UniqueConstraint(
            "library_id",
            "evolution_decision_id",
            "target_key",
            name="uq_fact_reconciliation_target_slots_decision_key",
        ),
        Index(
            "uq_fact_reconciliation_target_slots_root",
            "library_id",
            "command_id",
            "target_key",
            unique=True,
            postgresql_where=text("supersedes_target_slot_id IS NULL"),
        ),
        Index(
            "uq_fact_reconciliation_target_slots_superseded_by",
            "library_id",
            "supersedes_target_slot_id",
            unique=True,
            postgresql_where=text("supersedes_target_slot_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    command_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    evolution_decision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    target_key: Mapped[str] = mapped_column(String(256), nullable=False)
    target_ref_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    existing_logical_fact_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    target_spec_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    target_identity_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    planned_target_logical_fact_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    target_logical_fact_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    slot_state: Mapped[str] = mapped_column(String(16), nullable=False)
    supersedes_target_slot_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FactReconciliationSourceTargetEdge(Base):
    __tablename__ = "fact_reconciliation_source_target_edges"
    __table_args__ = (
        CheckConstraint(
            "edge_state IN ('pending','applied','superseded')",
            name="ck_fact_reconciliation_source_target_edges_state",
        ),
        CheckConstraint(
            "supersedes_source_target_edge_id IS NULL OR supersedes_source_target_edge_id <> id",
            name="ck_fact_reconciliation_source_target_edges_not_self_predecessor",
        ),
        ForeignKeyConstraint(
            ["evolution_decision_id", "library_id", "command_id"],
            [
                "fact_reconciliation_decisions.id",
                "fact_reconciliation_decisions.library_id",
                "fact_reconciliation_decisions.command_id",
            ],
            ondelete="RESTRICT",
            name="fk_fact_reconciliation_source_target_edges_decision",
        ),
        ForeignKeyConstraint(
            [
                "source_transition_id",
                "library_id",
                "command_id",
                "evolution_decision_id",
                "source_logical_fact_id",
            ],
            [
                "fact_reconciliation_sources.id",
                "fact_reconciliation_sources.library_id",
                "fact_reconciliation_sources.command_id",
                "fact_reconciliation_sources.evolution_decision_id",
                "fact_reconciliation_sources.source_logical_fact_id",
            ],
            ondelete="RESTRICT",
            name="fk_fact_reconciliation_source_target_edges_source",
        ),
        ForeignKeyConstraint(
            ["target_slot_id", "library_id", "command_id", "evolution_decision_id", "target_key"],
            [
                "fact_reconciliation_target_slots.id",
                "fact_reconciliation_target_slots.library_id",
                "fact_reconciliation_target_slots.command_id",
                "fact_reconciliation_target_slots.evolution_decision_id",
                "fact_reconciliation_target_slots.target_key",
            ],
            ondelete="RESTRICT",
            name="fk_fact_reconciliation_source_target_edges_target_slot",
        ),
        UniqueConstraint(
            "id",
            "library_id",
            "command_id",
            "evolution_decision_id",
            "source_logical_fact_id",
            "target_key",
            name="uq_fact_reconciliation_source_target_edges_owner",
        ),
        UniqueConstraint(
            "id",
            "library_id",
            "command_id",
            "source_logical_fact_id",
            "target_key",
            name="uq_fact_reconciliation_source_target_edges_owner_key",
        ),
        UniqueConstraint(
            "library_id",
            "evolution_decision_id",
            "source_transition_id",
            "target_slot_id",
            name="uq_fact_reconciliation_source_target_edges_decision_pair",
        ),
        Index(
            "uq_fact_reconciliation_source_target_edges_root",
            "library_id",
            "command_id",
            "source_logical_fact_id",
            "target_key",
            unique=True,
            postgresql_where=text("supersedes_source_target_edge_id IS NULL"),
        ),
        Index(
            "uq_fact_reconciliation_source_target_edges_superseded_by",
            "library_id",
            "supersedes_source_target_edge_id",
            unique=True,
            postgresql_where=text("supersedes_source_target_edge_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    command_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    evolution_decision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    source_logical_fact_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    source_transition_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    target_key: Mapped[str] = mapped_column(String(256), nullable=False)
    target_slot_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    edge_state: Mapped[str] = mapped_column(String(16), nullable=False)
    supersedes_source_target_edge_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FactReconciliationAssertionAssignment(Base):
    __tablename__ = "fact_reconciliation_assertion_assignments"
    __table_args__ = (
        CheckConstraint(
            "assignment_state IN ('resolved','pending','superseded')",
            name="ck_fact_reconciliation_assertion_assignments_state",
        ),
        CheckConstraint(
            "source_group_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_fact_reconciliation_assertion_assignments_group_fingerprint",
        ),
        CheckConstraint(
            "jsonb_typeof(partition_basis_snapshot) = 'object' "
            "AND octet_length(partition_basis_snapshot::text) <= 65536",
            name="ck_fact_reconciliation_assertion_assignments_basis",
        ),
        CheckConstraint(
            "supersedes_assignment_id IS NULL OR supersedes_assignment_id <> id",
            name="ck_fact_recon_assignments_not_self_predecessor",
        ),
        CheckConstraint(
            "(assignment_state = 'pending' AND target_key IS NULL "
            "AND source_target_edge_id IS NULL) "
            "OR (assignment_state = 'resolved' AND target_key IS NOT NULL "
            "AND source_target_edge_id IS NOT NULL) "
            "OR assignment_state = 'superseded'",
            name="ck_fact_reconciliation_assertion_assignments_shape",
        ),
        ForeignKeyConstraint(
            ["evolution_decision_id", "library_id", "command_id"],
            [
                "fact_reconciliation_decisions.id",
                "fact_reconciliation_decisions.library_id",
                "fact_reconciliation_decisions.command_id",
            ],
            ondelete="RESTRICT",
            name="fk_fact_reconciliation_assertion_assignments_decision",
        ),
        ForeignKeyConstraint(
            [
                "source_transition_id",
                "library_id",
                "command_id",
                "evolution_decision_id",
                "source_logical_fact_id",
            ],
            [
                "fact_reconciliation_sources.id",
                "fact_reconciliation_sources.library_id",
                "fact_reconciliation_sources.command_id",
                "fact_reconciliation_sources.evolution_decision_id",
                "fact_reconciliation_sources.source_logical_fact_id",
            ],
            ondelete="RESTRICT",
            name="fk_fact_reconciliation_assertion_assignments_source",
        ),
        ForeignKeyConstraint(
            ["fact_assertion_id", "library_id"],
            ["fact_assertions.id", "fact_assertions.library_id"],
            ondelete="RESTRICT",
            name="fk_fact_reconciliation_assertion_assignments_assertion",
        ),
        ForeignKeyConstraint(
            [
                "source_target_edge_id",
                "library_id",
                "command_id",
                "evolution_decision_id",
                "source_logical_fact_id",
                "target_key",
            ],
            [
                "fact_reconciliation_source_target_edges.id",
                "fact_reconciliation_source_target_edges.library_id",
                "fact_reconciliation_source_target_edges.command_id",
                "fact_reconciliation_source_target_edges.evolution_decision_id",
                "fact_reconciliation_source_target_edges.source_logical_fact_id",
                "fact_reconciliation_source_target_edges.target_key",
            ],
            ondelete="RESTRICT",
            name="fk_fact_reconciliation_assertion_assignments_edge",
        ),
        UniqueConstraint(
            "id",
            "library_id",
            "command_id",
            "fact_assertion_id",
            name="uq_fact_reconciliation_assertion_assignments_owner_assertion",
        ),
        UniqueConstraint(
            "library_id",
            "evolution_decision_id",
            "fact_assertion_id",
            name="uq_fact_reconciliation_assertion_assignments_decision_assertion",
        ),
        Index(
            "uq_fact_reconciliation_assertion_assignments_root",
            "library_id",
            "command_id",
            "fact_assertion_id",
            unique=True,
            postgresql_where=text("supersedes_assignment_id IS NULL"),
        ),
        Index(
            "uq_fact_reconciliation_assertion_assignments_superseded_by",
            "library_id",
            "supersedes_assignment_id",
            unique=True,
            postgresql_where=text("supersedes_assignment_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    library_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    command_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    evolution_decision_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    source_logical_fact_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    source_transition_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    fact_assertion_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    source_group_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    partition_basis_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    assignment_state: Mapped[str] = mapped_column(String(16), nullable=False)
    target_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    source_target_edge_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_assignment_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
