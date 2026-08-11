from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class GraphMappingAuthoritySnapshot(Base):
    """Append-only persistence authority for a mapping registry snapshot.

    The JSON snapshot is revalidated and content-hashed by the repository on
    every write/read.  The database row supplies the immutable persistence
    identity; a caller-provided hash alone is never treated as authority.
    """

    __tablename__ = "graph_mapping_authority_snapshots"
    __table_args__ = (
        CheckConstraint(
            "authority_schema_version = 'mapping_authority_snapshot_v1'",
            name="ck_graph_mapping_authority_schema_version",
        ),
        CheckConstraint(
            "authority_fingerprint ~ '^[0-9a-f]{64}$' AND "
            "registry_snapshot_hash ~ '^[0-9a-f]{64}$' AND "
            "ontology_snapshot_hash ~ '^[0-9a-f]{64}$' AND "
            "source_hash ~ '^[0-9a-f]{64}$' AND source_hash <> repeat('0', 64)",
            name="ck_graph_mapping_authority_hashes",
        ),
        CheckConstraint(
            "jsonb_typeof(registry_snapshot) = 'object' AND "
            "pg_column_size(registry_snapshot) <= 131072",
            name="ck_graph_mapping_authority_snapshot_json",
        ),
        CheckConstraint("revision_no >= 1", name="ck_graph_mapping_authority_revision_no"),
        CheckConstraint(
            "source_table = 'graph_extraction_jobs' AND source_id = job_id AND "
            "source_kind = 'graph_extraction_job' AND source_version = 'ontology_snapshot_v1' "
            "AND source_key = job_id::text",
            name="ck_graph_mapping_authority_repository_source",
        ),
        UniqueConstraint(
            "authority_fingerprint",
            name="uq_graph_mapping_authority_fingerprint",
        ),
        Index(
            "ix_graph_mapping_authority_scope",
            "library_id",
            "document_revision_id",
            "job_id",
            "extraction_unit_id",
        ),
    )

    authority_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    authority_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    authority_schema_version: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="mapping_authority_snapshot_v1"
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="RESTRICT", name="fk_graph_mapping_authority_library"),
        nullable=False,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT", name="fk_graph_mapping_authority_document"),
        nullable=False,
    )
    document_revision_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("document_revisions.id", ondelete="RESTRICT", name="fk_graph_mapping_authority_revision"),
        nullable=False,
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("graph_extraction_jobs.id", ondelete="RESTRICT", name="fk_graph_mapping_authority_job"),
        nullable=False,
    )
    extraction_unit_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("graph_extraction_units.id", ondelete="RESTRICT", name="fk_graph_mapping_authority_unit"),
        nullable=False,
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("graph_raw_claims.id", ondelete="RESTRICT", name="fk_graph_mapping_authority_claim"),
        nullable=False,
    )
    extraction_occurrence_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_raw_claim_occurrences.extraction_occurrence_id",
            ondelete="RESTRICT",
            name="fk_graph_mapping_authority_occurrence",
        ),
        nullable=False,
    )
    ontology_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("ontology_versions.id", ondelete="RESTRICT", name="fk_graph_mapping_authority_ontology"),
        nullable=False,
    )
    ontology_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ontology_contract_version: Mapped[str] = mapped_column(String(64), nullable=False)
    registry_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    registry_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_table: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_key: Mapped[str] = mapped_column(String(128), nullable=False)
    source_version: Mapped[str] = mapped_column(String(128), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class GraphClaimMapping(Base):
    """Append-only persistence for one validated canonical mapping result.

    ``result_projection`` is the complete, versioned CanonicalMappingV1 JSON
    projection.  Scalar columns are duplicated only for bounded scoped reads,
    FK protection, and database-level invariants; callers must still revalidate
    the projection at the typed boundary.
    """

    __tablename__ = "graph_claim_mappings"
    __table_args__ = (
        CheckConstraint(
            "mapping_schema_version = 'canonical_mapping_v1'",
            name="ck_graph_claim_mappings_schema_version",
        ),
        CheckConstraint(
            "outcome IN ('mapped','ambiguous','blocked','rejected')",
            name="ck_graph_claim_mappings_outcome",
        ),
        CheckConstraint(
            "reason_code IS NULL OR reason_code IN "
            "('unknown_predicate','unknown_source_type','unknown_target_type',"
            "'unknown_direction','ambiguous_mapping','ambiguous_endpoint',"
            "'ontology_relation_not_allowed','ontology_snapshot_mismatch',"
            "'evidence_missing','evidence_invalid','scope_mismatch','no_explicit_mapping',"
            "'unsupported_negation','unsupported_modality','unsupported_qualifier',"
            "'unsupported_valid_time','unsupported_effective_time','mapper_error')",
            name="ck_graph_claim_mappings_reason_code",
        ),
        CheckConstraint(
            "semantic_status IN ('preserved','ambiguous','blocked')",
            name="ck_graph_claim_mappings_semantic_status",
        ),
        CheckConstraint(
            "(outcome = 'mapped' AND reason_code IS NULL AND semantic_status = 'preserved' AND "
            "mapping_confidence IS NOT NULL) OR "
            "(outcome = 'ambiguous' AND reason_code IN "
            "('unknown_direction','ambiguous_mapping','ambiguous_endpoint','unsupported_negation',"
            "'unsupported_modality','unsupported_qualifier','unsupported_valid_time','unsupported_effective_time') "
            "AND semantic_status = 'ambiguous' AND "
            "(mapping_confidence IS NULL OR (mapping_confidence >= 0 AND mapping_confidence <= 1))) OR "
            "(outcome = 'blocked' AND reason_code IN "
            "('unknown_predicate','unknown_source_type','unknown_target_type','unknown_direction',"
            "'ambiguous_mapping','ambiguous_endpoint','ontology_snapshot_mismatch','evidence_missing',"
            "'evidence_invalid','scope_mismatch','no_explicit_mapping','unsupported_negation',"
            "'unsupported_modality','unsupported_qualifier','unsupported_valid_time','unsupported_effective_time','mapper_error') "
            "AND semantic_status = 'blocked' AND mapping_confidence IS NULL) OR "
            "(outcome = 'rejected' AND reason_code = 'ontology_relation_not_allowed' AND "
            "semantic_status = 'preserved' AND mapping_confidence IS NULL)",
            name="ck_graph_claim_mappings_outcome_matrix",
        ),
        CheckConstraint(
            "mapping_version >= 1 AND revision_no >= 1 AND remap_generation >= 0",
            name="ck_graph_claim_mappings_versions",
        ),
        CheckConstraint(
            "(remap_generation = 0 AND supersedes_mapping_result_id IS NULL AND "
            "supersedes_mapping_result_fingerprint IS NULL AND lineage_root_mapping_result_id IS NULL AND "
            "lineage_root_mapping_result_fingerprint IS NULL) OR "
            "(remap_generation > 0 AND supersedes_mapping_result_id IS NOT NULL AND "
            "supersedes_mapping_result_fingerprint IS NOT NULL AND lineage_root_mapping_result_id IS NOT NULL AND "
            "lineage_root_mapping_result_fingerprint IS NOT NULL)",
            name="ck_graph_claim_mappings_remap_fields",
        ),
        CheckConstraint(
            "mapping_attempt_fingerprint ~ '^[0-9a-f]{64}$' AND "
            "mapping_result_fingerprint ~ '^[0-9a-f]{64}$' AND "
            "mapping_schema_hash ~ '^[0-9a-f]{64}$' AND "
            "canonical_schema_hash ~ '^[0-9a-f]{64}$' AND "
            "raw_claim_authority_sha256 ~ '^[0-9a-f]{64}$' AND "
            "authority_fingerprint ~ '^[0-9a-f]{64}$' AND "
            "claim_content_scoped_fingerprint ~ '^[0-9a-f]{64}$' AND "
            "extraction_occurrence_fingerprint ~ '^[0-9a-f]{64}$' AND "
            "ontology_snapshot_hash ~ '^[0-9a-f]{64}$' AND "
            "(supersedes_mapping_result_fingerprint IS NULL OR "
            "supersedes_mapping_result_fingerprint ~ '^[0-9a-f]{64}$') AND "
            "(lineage_root_mapping_result_fingerprint IS NULL OR "
            "lineage_root_mapping_result_fingerprint ~ '^[0-9a-f]{64}$')",
            name="ck_graph_claim_mappings_hashes",
        ),
        CheckConstraint(
            "mapping_confidence IS NULL OR "
            "(mapping_confidence::text NOT IN ('NaN', 'Infinity', '-Infinity') AND "
            "mapping_confidence >= 0 AND mapping_confidence <= 1)",
            name="ck_graph_claim_mappings_confidence",
        ),
        CheckConstraint(
            "(outcome = 'mapped' AND canonical_relation_key IS NOT NULL AND "
            "canonical_direction IS NOT NULL AND endpoint_transform IS NOT NULL AND "
            "predicate_transform IS NOT NULL AND mapping_confidence IS NOT NULL) OR "
            "(outcome IN ('ambiguous','blocked','rejected') AND canonical_relation_key IS NULL AND "
            "canonical_direction IS NULL AND endpoint_transform IS NULL AND "
            "predicate_transform IS NULL AND "
            "(outcome = 'ambiguous' OR mapping_confidence IS NULL))",
            name="ck_graph_claim_mappings_canonical_key_presence",
        ),
        CheckConstraint(
            "surface_direction IN ('source_to_target','target_to_source','undirected','unknown')",
            name="ck_graph_claim_mappings_surface_direction",
        ),
        CheckConstraint(
            "canonical_direction IS NULL OR canonical_direction IN ('source_to_target','target_to_source','undirected')",
            name="ck_graph_claim_mappings_canonical_direction",
        ),
        CheckConstraint(
            "endpoint_transform IS NULL OR endpoint_transform IN ('identity','swap')",
            name="ck_graph_claim_mappings_endpoint_transform",
        ),
        CheckConstraint(
            "predicate_transform IS NULL OR predicate_transform IN ('identity','inverse','symmetric')",
            name="ck_graph_claim_mappings_predicate_transform",
        ),
        CheckConstraint(
            "jsonb_typeof(result_projection) = 'object' AND "
            "jsonb_typeof(evidence_bindings) = 'array' AND "
            "jsonb_typeof(source_evidence_ref_ids) = 'array' AND "
            "jsonb_typeof(target_evidence_ref_ids) = 'array' AND "
            "jsonb_typeof(mapping_evidence_ref_ids) = 'array' AND "
            "pg_column_size(result_projection) <= 131072",
            name="ck_graph_claim_mappings_json_projection",
        ),
        UniqueConstraint(
            "mapping_result_fingerprint",
            name="uq_graph_claim_mappings_result_fingerprint",
        ),
        Index(
            "ix_graph_claim_mappings_scope_created",
            "library_id",
            "document_revision_id",
            "created_at",
            "mapping_result_id",
        ),
        Index(
            "ix_graph_claim_mappings_scope_claim",
            "library_id",
            "document_revision_id",
            "claim_id",
            "created_at",
        ),
        Index(
            "ix_graph_claim_mappings_scope_occurrence",
            "library_id",
            "document_revision_id",
            "extraction_occurrence_id",
            "created_at",
        ),
    )

    mapping_result_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True
    )
    mapping_attempt_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), nullable=False
    )
    mapping_attempt_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping_result_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping_schema_version: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="canonical_mapping_v1"
    )
    mapping_schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_claim_authority_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping_version: Mapped[int] = mapped_column(Integer, nullable=False)
    authority_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_mapping_authority_snapshots.authority_id",
            ondelete="RESTRICT",
            name="fk_graph_claim_mappings_authority",
        ),
        nullable=False,
    )
    authority_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="RESTRICT", name="fk_graph_claim_mappings_library"),
        nullable=False,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT", name="fk_graph_claim_mappings_document"),
        nullable=False,
    )
    document_revision_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "document_revisions.id",
            ondelete="RESTRICT",
            name="fk_graph_claim_mappings_revision",
        ),
        nullable=False,
    )
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False)
    job_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("graph_extraction_jobs.id", ondelete="RESTRICT", name="fk_graph_claim_mappings_job"),
        nullable=False,
    )
    extraction_unit_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_extraction_units.id",
            ondelete="RESTRICT",
            name="fk_graph_claim_mappings_unit",
        ),
        nullable=False,
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("graph_raw_claims.id", ondelete="RESTRICT", name="fk_graph_claim_mappings_claim"),
        nullable=False,
    )
    claim_content_scoped_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    extraction_occurrence_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_raw_claim_occurrences.extraction_occurrence_id",
            ondelete="RESTRICT",
            name="fk_graph_claim_mappings_occurrence",
        ),
        nullable=False,
    )
    extraction_occurrence_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    surface_raw_predicate: Mapped[str] = mapped_column(String(256), nullable=False)
    surface_direction: Mapped[str] = mapped_column(String(32), nullable=False)
    ontology_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "ontology_versions.id",
            ondelete="RESTRICT",
            name="fk_graph_claim_mappings_ontology",
        ),
        nullable=False,
    )
    ontology_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ontology_contract_version: Mapped[str] = mapped_column(String(64), nullable=False)

    decision_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_claim_decisions.decision_id",
            ondelete="RESTRICT",
            name="fk_graph_claim_mappings_decision",
        ),
        nullable=True,
    )
    decision_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    decision_kind: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)

    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    semantic_status: Mapped[str] = mapped_column(String(16), nullable=False)
    canonical_relation_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    canonical_direction: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    endpoint_transform: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    predicate_transform: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    mapping_confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    remap_generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    supersedes_mapping_result_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_claim_mappings.mapping_result_id",
            ondelete="RESTRICT",
            name="fk_graph_claim_mappings_supersedes",
        ),
        nullable=True,
    )
    supersedes_mapping_result_fingerprint: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    lineage_root_mapping_result_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_claim_mappings.mapping_result_id",
            ondelete="RESTRICT",
            name="fk_graph_claim_mappings_lineage_root",
        ),
        nullable=True,
    )
    lineage_root_mapping_result_fingerprint: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )

    evidence_bindings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    source_evidence_ref_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    target_evidence_ref_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    mapping_evidence_ref_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    result_projection: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
