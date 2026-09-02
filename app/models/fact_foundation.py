from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


STABLE_PREDICATE_TEMPORAL_CLASSES = (
    "static_fact",
    "state_fact",
    "measurement_slot",
    "event_fact",
)
STABLE_PREDICATE_RESOLUTION_STATUSES = (
    "resolved",
    "pending",
    "ambiguous",
    "rejected",
)
STABLE_PREDICATE_MAPPING_STATUSES = ("active", "superseded", "rejected")
LOGICAL_FACT_STATUSES = ("active", "conflicted", "inactive")
FACT_ASSERTION_POLARITIES = ("affirmed", "negated", "unknown")
FACT_ASSERTION_MODALITIES = (
    "planned",
    "possible",
    "expected",
    "confirmed",
    "completed",
    "unknown",
)
FACT_ASSERTION_STATUSES = ("active", "stale", "superseded", "rejected")
FACT_ASSERTION_SOURCE_KINDS = (
    "raw_claim",
    "graph_relation_candidate",
    "knowledge_relation",
    "manual",
)
FACT_RESOLUTION_STATUSES = ("pending", "resolved", "rejected", "superseded")


class StablePredicateIdentity(Base):
    __tablename__ = "stable_predicate_identities"
    __table_args__ = (
        CheckConstraint(
            "temporal_class IN ('static_fact','state_fact','measurement_slot','event_fact')",
            name="ck_stable_predicate_identities_temporal_class",
        ),
        CheckConstraint(
            "resolution_status IN ('resolved','pending','ambiguous','rejected')",
            name="ck_stable_predicate_identities_resolution_status",
        ),
        CheckConstraint(
            "resolution_policy IS NULL OR jsonb_typeof(resolution_policy) = 'object'",
            name="ck_stable_predicate_identities_resolution_policy_json",
        ),
        CheckConstraint(
            "resolution_status != 'resolved' OR resolution_policy IS NOT NULL",
            name="ck_stable_predicate_identities_resolved_requires_policy",
        ),
        UniqueConstraint(
            "library_id",
            "namespace",
            "key",
            "contract_version",
            name="uq_stable_predicate_identities_scope_key_version",
        ),
        UniqueConstraint(
            "id",
            "library_id",
            name="uq_stable_predicate_identities_id_library",
        ),
        Index(
            "ix_stable_predicate_identities_library_status",
            "library_id",
            "resolution_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="RESTRICT", name="fk_stable_predicate_identities_library"),
        nullable=False,
        index=True,
    )
    namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    temporal_class: Mapped[str] = mapped_column(String(32), nullable=False)
    identity_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    resolution_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    resolution_policy: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class StablePredicateMapping(Base):
    __tablename__ = "stable_predicate_mappings"
    __table_args__ = (
        CheckConstraint(
            "mapping_status IN ('active','superseded','rejected')",
            name="ck_stable_predicate_mappings_status",
        ),
        ForeignKeyConstraint(
            ["stable_predicate_identity_id", "library_id"],
            ["stable_predicate_identities.id", "stable_predicate_identities.library_id"],
            ondelete="RESTRICT",
            name="fk_stable_predicate_mappings_identity",
        ),
        ForeignKeyConstraint(
            ["relation_type_id", "library_id"],
            ["relation_types.id", "relation_types.library_id"],
            ondelete="RESTRICT",
            name="fk_stable_predicate_mappings_relation_type",
        ),
        Index(
            "ix_stable_predicate_mappings_library_identity",
            "library_id",
            "stable_predicate_identity_id",
        ),
        Index(
            "uq_stable_predicate_mappings_library_relation_type_active",
            "library_id",
            "relation_type_id",
            unique=True,
            postgresql_where=text("mapping_status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="RESTRICT", name="fk_stable_predicate_mappings_library"),
        nullable=False,
        index=True,
    )
    stable_predicate_identity_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    relation_type_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    mapping_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    superseded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class LogicalFact(Base):
    __tablename__ = "logical_facts"
    __table_args__ = (
        CheckConstraint(
            "object_kind IS NULL OR object_kind IN ('entity','literal','reference')",
            name="ck_logical_facts_object_kind",
        ),
        CheckConstraint(
            "object_value IS NULL OR jsonb_typeof(object_value) = 'object'",
            name="ck_logical_facts_object_value_json",
        ),
        CheckConstraint(
            "jsonb_typeof(identity_qualifiers) = 'object'",
            name="ck_logical_facts_identity_qualifiers_json",
        ),
        CheckConstraint(
            "identity_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_logical_facts_identity_fingerprint",
        ),
        CheckConstraint(
            "status IN ('active','conflicted','inactive')",
            name="ck_logical_facts_status",
        ),
        ForeignKeyConstraint(
            ["stable_predicate_identity_id", "library_id"],
            ["stable_predicate_identities.id", "stable_predicate_identities.library_id"],
            ondelete="RESTRICT",
            name="fk_logical_facts_stable_predicate",
        ),
        ForeignKeyConstraint(
            ["subject_canonical_entity_id", "library_id"],
            ["canonical_entities.id", "canonical_entities.library_id"],
            ondelete="RESTRICT",
            name="fk_logical_facts_subject_canonical",
        ),
        ForeignKeyConstraint(
            ["object_canonical_entity_id", "library_id"],
            ["canonical_entities.id", "canonical_entities.library_id"],
            ondelete="RESTRICT",
            name="fk_logical_facts_object_canonical",
        ),
        UniqueConstraint("id", "library_id", name="uq_logical_facts_id_library"),
        Index("ix_logical_facts_library_predicate", "library_id", "stable_predicate_identity_id"),
        Index("ix_logical_facts_library_subject", "library_id", "subject_canonical_entity_id"),
        Index("ix_logical_facts_library_fingerprint", "library_id", "identity_fingerprint"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="RESTRICT", name="fk_logical_facts_library"),
        nullable=False,
        index=True,
    )
    stable_predicate_identity_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    subject_canonical_entity_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    object_kind: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    object_canonical_entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True), nullable=True, index=True
    )
    object_value: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    identity_qualifiers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    temporal_identity_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    identity_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    identity_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class FactAssertion(Base):
    __tablename__ = "fact_assertions"
    __table_args__ = (
        CheckConstraint(
            "asserted_object_kind IS NULL OR asserted_object_kind IN ('entity','literal','reference')",
            name="ck_fact_assertions_asserted_object_kind",
        ),
        CheckConstraint(
            "asserted_value IS NULL OR jsonb_typeof(asserted_value) = 'object'",
            name="ck_fact_assertions_asserted_value_json",
        ),
        CheckConstraint(
            "jsonb_typeof(qualifiers) = 'object'",
            name="ck_fact_assertions_qualifiers_json",
        ),
        CheckConstraint(
            "polarity IN ('affirmed','negated','unknown')",
            name="ck_fact_assertions_polarity",
        ),
        CheckConstraint(
            "modality IN ('planned','possible','expected','confirmed','completed','unknown')",
            name="ck_fact_assertions_modality",
        ),
        CheckConstraint(
            "status IN ('active','stale','superseded','rejected')",
            name="ck_fact_assertions_status",
        ),
        CheckConstraint(
            "source_kind IN ('raw_claim','graph_relation_candidate','knowledge_relation','manual')",
            name="ck_fact_assertions_source_kind",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_fact_assertions_confidence",
        ),
        ForeignKeyConstraint(
            ["logical_fact_id", "library_id"],
            ["logical_facts.id", "logical_facts.library_id"],
            ondelete="RESTRICT",
            name="fk_fact_assertions_logical_fact",
        ),
        ForeignKeyConstraint(
            ["knowledge_relation_id", "library_id"],
            ["knowledge_relations.id", "knowledge_relations.library_id"],
            ondelete="SET NULL",
            name="fk_fact_assertions_knowledge_relation",
        ),
        ForeignKeyConstraint(
            ["asserted_object_canonical_entity_id", "library_id"],
            ["canonical_entities.id", "canonical_entities.library_id"],
            ondelete="SET NULL",
            name="fk_fact_assertions_asserted_object_canonical",
        ),
        UniqueConstraint("id", "library_id", name="uq_fact_assertions_id_library"),
        UniqueConstraint("library_id", "assertion_fingerprint", name="uq_fact_assertions_library_fingerprint"),
        Index("ix_fact_assertions_library_fact_status", "library_id", "logical_fact_id", "status"),
        Index("ix_fact_assertions_library_relation", "library_id", "knowledge_relation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="RESTRICT", name="fk_fact_assertions_library"),
        nullable=False,
        index=True,
    )
    logical_fact_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False, index=True
    )
    knowledge_relation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True), nullable=True, index=True
    )
    assertion_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    asserted_object_kind: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    asserted_object_canonical_entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    asserted_value: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    polarity: Mapped[str] = mapped_column(String(16), nullable=False, default="affirmed", server_default="affirmed")
    modality: Mapped[str] = mapped_column(String(16), nullable=False, default="unknown", server_default="unknown")
    qualifiers: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    valid_time: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    effective_time: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="manual", server_default="manual")
    raw_claim_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("graph_raw_claims.id", ondelete="SET NULL", name="fk_fact_assertions_raw_claim"),
        nullable=True,
    )
    graph_relation_candidate_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("graph_relation_candidates.id", ondelete="SET NULL", name="fk_fact_assertions_relation_candidate"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class FactResolutionDecision(Base):
    __tablename__ = "fact_resolution_decisions"
    __table_args__ = (
        CheckConstraint(
            "source_kind IN ('raw_claim','graph_relation_candidate','knowledge_relation','manual')",
            name="ck_fact_resolution_decisions_source_kind",
        ),
        CheckConstraint(
            "status IN ('pending','resolved','rejected','superseded')",
            name="ck_fact_resolution_decisions_status",
        ),
        CheckConstraint(
            "subject_fingerprint ~ '^[0-9a-f]{64}$' AND decision_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_fact_resolution_decisions_fingerprints",
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_fact_resolution_decisions_confidence",
        ),
        CheckConstraint(
            "jsonb_typeof(source_snapshot) = 'object' AND "
            "jsonb_typeof(candidate_snapshot) = 'object' AND "
            "jsonb_typeof(evidence_refs) = 'array'",
            name="ck_fact_resolution_decisions_snapshot_shapes",
        ),
        Index(
            "uq_fact_resolution_decisions_library_fingerprint_active",
            "library_id",
            "decision_fingerprint",
            unique=True,
            postgresql_where=text("status <> 'superseded'"),
        ),
        Index(
            "uq_fact_resolution_decisions_library_subject_active",
            "library_id",
            "source_kind",
            "subject_fingerprint",
            unique=True,
            postgresql_where=text("status <> 'superseded'"),
        ),
        Index(
            "ix_fact_resolution_decisions_library_status",
            "library_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_fact_resolution_decisions_library_source",
            "library_id",
            "source_kind",
            "created_at",
        ),
        Index(
            "ix_fact_resolution_decisions_library_subject",
            "library_id",
            "subject_fingerprint",
        ),
        ForeignKeyConstraint(
            ["stable_predicate_identity_id", "library_id"],
            ["stable_predicate_identities.id", "stable_predicate_identities.library_id"],
            ondelete="SET NULL",
            name="fk_fact_resolution_decisions_stable_predicate",
        ),
        ForeignKeyConstraint(
            ["logical_fact_id", "library_id"],
            ["logical_facts.id", "logical_facts.library_id"],
            ondelete="SET NULL",
            name="fk_fact_resolution_decisions_logical_fact",
        ),
        ForeignKeyConstraint(
            ["fact_assertion_id", "library_id"],
            ["fact_assertions.id", "fact_assertions.library_id"],
            ondelete="SET NULL",
            name="fk_fact_resolution_decisions_assertion",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="RESTRICT", name="fk_fact_resolution_decisions_library"),
        nullable=False,
        index=True,
    )
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    graph_relation_candidate_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("graph_relation_candidates.id", ondelete="SET NULL", name="fk_fact_resolution_decisions_candidate"),
        nullable=True,
    )
    raw_claim_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("graph_raw_claims.id", ondelete="SET NULL", name="fk_fact_resolution_decisions_raw_claim"),
        nullable=True,
    )
    stable_predicate_identity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    logical_fact_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    fact_assertion_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    source_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    candidate_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    evidence_refs: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    method: Mapped[str] = mapped_column(String(64), nullable=False, default="none", server_default="none")
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reason_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    resolver_version: Mapped[str] = mapped_column(
        String(64), nullable=False, default="fact_resolution_v1", server_default="fact_resolution_v1"
    )
    supersedes_decision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("fact_resolution_decisions.id", ondelete="SET NULL", name="fk_fact_resolution_decisions_supersedes"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
