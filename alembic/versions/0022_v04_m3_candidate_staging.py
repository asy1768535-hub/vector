"""v0.4 graph extraction pipeline m3 candidate staging

Revision ID: 0022
Revises: 0021
Create Date: 2026-07-13
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CANDIDATE_STATUSES = (
    "'extracted','aggregated','validated','pending_review','rejected',"
    "'materialized','superseded'"
)


def _id_column() -> sa.Column:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True)


def _fk_column(
    column_name: str,
    target: str,
    constraint_name: str,
    ondelete: str,
    *,
    nullable: bool = False,
) -> sa.Column:
    return sa.Column(
        column_name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(
            target,
            name=constraint_name,
            ondelete=ondelete,
        ),
        nullable=nullable,
    )


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


def _updated_at() -> sa.Column:
    return sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    )


def _purged_at() -> sa.Column:
    return sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True)


def upgrade() -> None:
    _create_graph_entity_candidates()
    _create_graph_relation_candidates()
    _create_graph_entity_occurrences()
    _create_graph_relation_occurrences()
    _create_candidate_evidence(
        "graph_entity_candidate_evidence",
        "graph_entity_candidates.id",
    )
    _create_candidate_evidence(
        "graph_relation_candidate_evidence",
        "graph_relation_candidates.id",
    )
    _create_graph_entity_merge_candidates()
    _create_graph_extraction_conflicts()
    _add_formal_graph_provenance()


def _create_graph_entity_candidates() -> None:
    table = "graph_entity_candidates"
    op.create_table(
        table,
        _id_column(),
        _fk_column(
            "job_id",
            "graph_extraction_jobs.id",
            "fk_graph_entity_candidates_job",
            "CASCADE",
        ),
        _fk_column(
            "library_id",
            "sys_libraries.id",
            "fk_graph_entity_candidates_library",
            "CASCADE",
        ),
        _fk_column(
            "ontology_version_id",
            "ontology_versions.id",
            "fk_graph_entity_candidates_ontology",
            "RESTRICT",
        ),
        sa.Column("entity_type_key", sa.String(length=128), nullable=False),
        sa.Column("canonical_name", sa.String(length=512), nullable=True),
        sa.Column("normalized_name", sa.String(length=512), nullable=True),
        sa.Column("proposed_aliases", postgresql.JSONB(), nullable=True),
        sa.Column("proposed_properties", postgresql.JSONB(), nullable=True),
        sa.Column("external_mapping_hints", postgresql.JSONB(), nullable=True),
        sa.Column("candidate_key", sa.String(length=64), nullable=False),
        _fk_column(
            "matched_entity_id",
            "entities.id",
            "fk_graph_entity_candidates_matched_entity",
            "SET NULL",
            nullable=True,
        ),
        _fk_column(
            "materialized_entity_id",
            "entities.id",
            "fk_graph_entity_candidates_materialized_entity",
            "SET NULL",
            nullable=True,
        ),
        sa.Column("normalization_method", sa.String(length=64), nullable=True),
        sa.Column("model_confidence", sa.Float(), nullable=True),
        sa.Column("evidence_quality_score", sa.Float(), nullable=True),
        sa.Column("schema_validation_score", sa.Float(), nullable=True),
        sa.Column("final_confidence", sa.Float(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="extracted",
        ),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("validation_errors", postgresql.JSONB(), nullable=True),
        _created_at(),
        _updated_at(),
        _purged_at(),
        sa.CheckConstraint(
            f"status IN ({_CANDIDATE_STATUSES})",
            name="ck_graph_entity_candidates_status",
        ),
        sa.CheckConstraint(
            "(model_confidence IS NULL OR "
            "(model_confidence >= 0 AND model_confidence <= 1)) AND "
            "(evidence_quality_score IS NULL OR "
            "(evidence_quality_score >= 0 AND evidence_quality_score <= 1)) AND "
            "(schema_validation_score IS NULL OR "
            "(schema_validation_score >= 0 AND schema_validation_score <= 1)) AND "
            "(final_confidence IS NULL OR "
            "(final_confidence >= 0 AND final_confidence <= 1))",
            name="ck_graph_entity_candidates_confidence",
        ),
        sa.CheckConstraint(
            "(purged_at IS NULL AND canonical_name IS NOT NULL "
            "AND normalized_name IS NOT NULL AND proposed_aliases IS NOT NULL "
            "AND proposed_properties IS NOT NULL AND external_mapping_hints IS NOT NULL) OR "
            "(purged_at IS NOT NULL AND canonical_name IS NULL "
            "AND normalized_name IS NULL AND proposed_aliases IS NULL "
            "AND proposed_properties IS NULL AND external_mapping_hints IS NULL "
            "AND review_reason IS NULL AND validation_errors IS NULL)",
            name="ck_graph_entity_candidates_payload_or_purged",
        ),
        sa.UniqueConstraint(
            "job_id",
            "candidate_key",
            name="uq_graph_entity_candidates_job_key",
        ),
    )
    op.create_index(
        "ix_graph_entity_candidates_job_status_key",
        table,
        ["job_id", "status", "candidate_key"],
    )
    op.create_index(
        "ix_graph_entity_candidates_purge",
        table,
        ["purged_at", "created_at"],
    )


def _create_graph_relation_candidates() -> None:
    table = "graph_relation_candidates"
    op.create_table(
        table,
        _id_column(),
        _fk_column(
            "job_id",
            "graph_extraction_jobs.id",
            "fk_graph_relation_candidates_job",
            "CASCADE",
        ),
        _fk_column(
            "library_id",
            "sys_libraries.id",
            "fk_graph_relation_candidates_library",
            "CASCADE",
        ),
        _fk_column(
            "ontology_version_id",
            "ontology_versions.id",
            "fk_graph_relation_candidates_ontology",
            "RESTRICT",
        ),
        _fk_column(
            "source_candidate_id",
            "graph_entity_candidates.id",
            "fk_graph_relation_candidates_source_candidate",
            "CASCADE",
        ),
        sa.Column("relation_type_key", sa.String(length=128), nullable=False),
        _fk_column(
            "target_candidate_id",
            "graph_entity_candidates.id",
            "fk_graph_relation_candidates_target_candidate",
            "CASCADE",
        ),
        sa.Column("proposed_properties", postgresql.JSONB(), nullable=True),
        sa.Column("candidate_key", sa.String(length=64), nullable=False),
        _fk_column(
            "matched_relation_id",
            "knowledge_relations.id",
            "fk_graph_relation_candidates_matched_relation",
            "SET NULL",
            nullable=True,
        ),
        _fk_column(
            "materialized_relation_id",
            "knowledge_relations.id",
            "fk_graph_relation_candidates_materialized_relation",
            "SET NULL",
            nullable=True,
        ),
        sa.Column("evidence_support_mode", sa.String(length=32), nullable=False),
        sa.Column("model_confidence", sa.Float(), nullable=True),
        sa.Column("evidence_quality_score", sa.Float(), nullable=True),
        sa.Column("schema_validation_score", sa.Float(), nullable=True),
        sa.Column("normalization_score", sa.Float(), nullable=True),
        sa.Column("final_confidence", sa.Float(), nullable=True),
        sa.Column(
            "ontology_validation_status", sa.String(length=32), nullable=True
        ),
        sa.Column(
            "has_conflict",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="extracted",
        ),
        sa.Column("review_reason", sa.Text(), nullable=True),
        sa.Column("validation_errors", postgresql.JSONB(), nullable=True),
        _created_at(),
        _updated_at(),
        _purged_at(),
        sa.CheckConstraint(
            f"status IN ({_CANDIDATE_STATUSES})",
            name="ck_graph_relation_candidates_status",
        ),
        sa.CheckConstraint(
            "evidence_support_mode IN ('single_evidence','evidence_group')",
            name="ck_graph_relation_candidates_support_mode",
        ),
        sa.CheckConstraint(
            "ontology_validation_status IS NULL OR ontology_validation_status IN "
            "('valid','warning','invalid','boundary_unclear')",
            name="ck_graph_relation_candidates_ontology_status",
        ),
        sa.CheckConstraint(
            "(model_confidence IS NULL OR "
            "(model_confidence >= 0 AND model_confidence <= 1)) AND "
            "(evidence_quality_score IS NULL OR "
            "(evidence_quality_score >= 0 AND evidence_quality_score <= 1)) AND "
            "(schema_validation_score IS NULL OR "
            "(schema_validation_score >= 0 AND schema_validation_score <= 1)) AND "
            "(normalization_score IS NULL OR "
            "(normalization_score >= 0 AND normalization_score <= 1)) AND "
            "(final_confidence IS NULL OR "
            "(final_confidence >= 0 AND final_confidence <= 1))",
            name="ck_graph_relation_candidates_confidence",
        ),
        sa.CheckConstraint(
            "(purged_at IS NULL AND proposed_properties IS NOT NULL) OR "
            "(purged_at IS NOT NULL AND proposed_properties IS NULL "
            "AND review_reason IS NULL AND validation_errors IS NULL)",
            name="ck_graph_relation_candidates_payload_or_purged",
        ),
        sa.UniqueConstraint(
            "job_id",
            "candidate_key",
            name="uq_graph_relation_candidates_job_key",
        ),
    )
    op.create_index(
        "ix_graph_relation_candidates_job_status_key",
        table,
        ["job_id", "status", "candidate_key"],
    )
    op.create_index(
        "ix_graph_relation_candidates_source", table, ["source_candidate_id"]
    )
    op.create_index(
        "ix_graph_relation_candidates_target", table, ["target_candidate_id"]
    )
    op.create_index(
        "ix_graph_relation_candidates_purge",
        table,
        ["purged_at", "created_at"],
    )


def _create_graph_entity_occurrences() -> None:
    table = "graph_entity_occurrences"
    op.create_table(
        table,
        _id_column(),
        _fk_column(
            "job_id",
            "graph_extraction_jobs.id",
            "fk_graph_entity_occurrences_job",
            "CASCADE",
        ),
        _fk_column(
            "extraction_unit_id",
            "graph_extraction_units.id",
            "fk_graph_entity_occurrences_unit",
            "CASCADE",
        ),
        sa.Column("local_ref", sa.String(length=128), nullable=False),
        _fk_column(
            "entity_candidate_id",
            "graph_entity_candidates.id",
            "fk_graph_entity_occurrences_candidate",
            "CASCADE",
        ),
        sa.Column("model_confidence", sa.Float(), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=True),
        _created_at(),
        _purged_at(),
        sa.CheckConstraint(
            "model_confidence IS NULL OR "
            "(model_confidence >= 0 AND model_confidence <= 1)",
            name="ck_graph_entity_occurrences_confidence",
        ),
        sa.CheckConstraint(
            "(purged_at IS NULL AND raw_payload IS NOT NULL) OR "
            "(purged_at IS NOT NULL AND raw_payload IS NULL)",
            name="ck_graph_entity_occurrences_payload_or_purged",
        ),
        sa.UniqueConstraint(
            "extraction_unit_id",
            "local_ref",
            name="uq_graph_entity_occurrences_unit_ref",
        ),
    )
    op.create_index(
        "ix_graph_entity_occurrences_job_candidate",
        table,
        ["job_id", "entity_candidate_id"],
    )
    op.create_index(
        "ix_graph_entity_occurrences_purge",
        table,
        ["purged_at", "created_at"],
    )


def _create_graph_relation_occurrences() -> None:
    table = "graph_relation_occurrences"
    op.create_table(
        table,
        _id_column(),
        _fk_column(
            "job_id",
            "graph_extraction_jobs.id",
            "fk_graph_relation_occurrences_job",
            "CASCADE",
        ),
        _fk_column(
            "extraction_unit_id",
            "graph_extraction_units.id",
            "fk_graph_relation_occurrences_unit",
            "CASCADE",
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        _fk_column(
            "source_entity_occurrence_id",
            "graph_entity_occurrences.id",
            "fk_graph_relation_occurrences_source_occurrence",
            "CASCADE",
        ),
        _fk_column(
            "target_entity_occurrence_id",
            "graph_entity_occurrences.id",
            "fk_graph_relation_occurrences_target_occurrence",
            "CASCADE",
        ),
        _fk_column(
            "relation_candidate_id",
            "graph_relation_candidates.id",
            "fk_graph_relation_occurrences_candidate",
            "CASCADE",
        ),
        sa.Column("model_confidence", sa.Float(), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=True),
        _created_at(),
        _purged_at(),
        sa.CheckConstraint(
            "ordinal >= 0", name="ck_graph_relation_occurrences_ordinal"
        ),
        sa.CheckConstraint(
            "model_confidence IS NULL OR "
            "(model_confidence >= 0 AND model_confidence <= 1)",
            name="ck_graph_relation_occurrences_confidence",
        ),
        sa.CheckConstraint(
            "(purged_at IS NULL AND raw_payload IS NOT NULL) OR "
            "(purged_at IS NOT NULL AND raw_payload IS NULL)",
            name="ck_graph_relation_occurrences_payload_or_purged",
        ),
        sa.UniqueConstraint(
            "extraction_unit_id",
            "ordinal",
            name="uq_graph_relation_occurrences_unit_ordinal",
        ),
    )
    op.create_index(
        "ix_graph_relation_occurrences_job_candidate",
        table,
        ["job_id", "relation_candidate_id"],
    )
    op.create_index(
        "ix_graph_relation_occurrences_purge",
        table,
        ["purged_at", "created_at"],
    )


def _candidate_evidence_resolution_check() -> str:
    nullable_resolution = (
        "resolved_evidence_id IS NULL AND resolved_document_id IS NULL "
        "AND resolved_document_revision_id IS NULL AND resolved_chunk_id IS NULL "
        "AND resolved_block_id IS NULL AND resolved_source_span IS NULL "
        "AND evidence_type IS NULL AND evidence_quality_score IS NULL"
    )
    return (
        "(purged_at IS NULL AND quote_text IS NOT NULL AND "
        "((validation_status = 'valid' "
        "AND jsonb_array_length(candidate_matches) = 1 "
        "AND resolved_evidence_id IS NOT NULL AND resolved_document_id IS NOT NULL "
        "AND resolved_document_revision_id IS NOT NULL AND resolved_chunk_id IS NOT NULL "
        "AND resolved_source_span IS NOT NULL AND evidence_type IS NOT NULL "
        "AND evidence_quality_score IS NOT NULL "
        "AND candidate_matches -> 0 ->> 'evidence_id' = resolved_evidence_id::text "
        "AND candidate_matches -> 0 ->> 'document_id' = resolved_document_id::text "
        "AND candidate_matches -> 0 ->> 'revision_id' = "
        "resolved_document_revision_id::text "
        "AND candidate_matches -> 0 ->> 'chunk_id' = resolved_chunk_id::text "
        "AND (candidate_matches -> 0 ->> 'block_id') IS NOT DISTINCT FROM "
        "resolved_block_id::text "
        "AND candidate_matches -> 0 -> 'source_span' = resolved_source_span) OR "
        "(validation_status = 'ambiguous' "
        "AND jsonb_array_length(candidate_matches) >= 2 AND "
        f"{nullable_resolution}) OR "
        "(validation_status = 'invalid' "
        "AND candidate_matches = '[]'::jsonb AND "
        f"{nullable_resolution}))) OR "
        "(purged_at IS NOT NULL AND quote_text IS NULL "
        "AND resolved_source_span IS NULL AND validation_error IS NULL "
        "AND candidate_matches = '[]'::jsonb AND "
        "((validation_status = 'valid' "
        "AND resolved_evidence_id IS NOT NULL AND resolved_document_id IS NOT NULL "
        "AND resolved_document_revision_id IS NOT NULL AND resolved_chunk_id IS NOT NULL "
        "AND evidence_type IS NOT NULL AND evidence_quality_score IS NOT NULL) OR "
        "(validation_status IN ('ambiguous','invalid') AND "
        f"{nullable_resolution})))"
    )


def _create_candidate_evidence(table: str, candidate_target: str) -> None:
    prefix = table
    op.create_table(
        table,
        _id_column(),
        _fk_column(
            "job_id",
            "graph_extraction_jobs.id",
            f"fk_{prefix}_job",
            "CASCADE",
        ),
        _fk_column(
            "extraction_unit_id",
            "graph_extraction_units.id",
            f"fk_{prefix}_unit",
            "CASCADE",
        ),
        _fk_column(
            "candidate_id",
            candidate_target,
            f"fk_{prefix}_candidate",
            "CASCADE",
        ),
        sa.Column("claim_key", sa.String(length=64), nullable=False),
        sa.Column("context_ref", sa.String(length=32), nullable=False),
        sa.Column("quote_text", sa.Text(), nullable=True),
        sa.Column("quote_hash", sa.String(length=64), nullable=False),
        _fk_column(
            "resolved_evidence_id",
            "evidence_units.id",
            f"fk_{prefix}_evidence",
            "RESTRICT",
            nullable=True,
        ),
        _fk_column(
            "resolved_document_id",
            "documents.id",
            f"fk_{prefix}_document",
            "RESTRICT",
            nullable=True,
        ),
        _fk_column(
            "resolved_document_revision_id",
            "document_revisions.id",
            f"fk_{prefix}_revision",
            "RESTRICT",
            nullable=True,
        ),
        _fk_column(
            "resolved_chunk_id",
            "chunks.id",
            f"fk_{prefix}_chunk",
            "RESTRICT",
            nullable=True,
        ),
        _fk_column(
            "resolved_block_id",
            "document_blocks.id",
            f"fk_{prefix}_block",
            "RESTRICT",
            nullable=True,
        ),
        sa.Column("resolved_source_span", postgresql.JSONB(), nullable=True),
        sa.Column(
            "candidate_matches",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("evidence_type", sa.String(length=32), nullable=True),
        sa.Column("evidence_quality_score", sa.Float(), nullable=True),
        sa.Column("validation_status", sa.String(length=32), nullable=False),
        sa.Column("validation_error", sa.Text(), nullable=True),
        _created_at(),
        _purged_at(),
        sa.CheckConstraint(
            "validation_status IN ('valid','ambiguous','invalid')",
            name=f"ck_{prefix}_status",
        ),
        sa.CheckConstraint(
            "evidence_type IS NULL OR evidence_type IN "
            "('direct_statement','table_cell')",
            name=f"ck_{prefix}_type",
        ),
        sa.CheckConstraint(
            "evidence_quality_score IS NULL OR evidence_quality_score BETWEEN 0 AND 1",
            name=f"ck_{prefix}_score",
        ),
        sa.CheckConstraint(
            _candidate_evidence_resolution_check(),
            name=f"ck_{prefix}_resolution",
        ),
        sa.UniqueConstraint(
            "candidate_id",
            "extraction_unit_id",
            "claim_key",
            name=f"uq_{prefix}_claim",
        ),
    )
    op.create_index(
        f"ix_{prefix}_job_status", table, ["job_id", "validation_status"]
    )
    op.create_index(f"ix_{prefix}_candidate", table, ["candidate_id"])
    op.create_index(f"ix_{prefix}_unit", table, ["extraction_unit_id"])
    op.create_index(
        f"ix_{prefix}_purge", table, ["purged_at", "created_at"]
    )


def _create_graph_entity_merge_candidates() -> None:
    table = "graph_entity_merge_candidates"
    op.create_table(
        table,
        _id_column(),
        _fk_column(
            "job_id",
            "graph_extraction_jobs.id",
            "fk_graph_entity_merge_candidates_job",
            "CASCADE",
        ),
        _fk_column(
            "library_id",
            "sys_libraries.id",
            "fk_graph_entity_merge_candidates_library",
            "CASCADE",
        ),
        _fk_column(
            "entity_candidate_id",
            "graph_entity_candidates.id",
            "fk_graph_entity_merge_candidates_candidate",
            "CASCADE",
        ),
        _fk_column(
            "suggested_target_entity_id",
            "entities.id",
            "fk_graph_entity_merge_candidates_target_entity",
            "SET NULL",
            nullable=True,
        ),
        sa.Column("merge_key", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="pending_review",
        ),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        _created_at(),
        _updated_at(),
        _purged_at(),
        sa.CheckConstraint(
            "status IN ('pending_review','rejected','superseded')",
            name="ck_graph_entity_merge_candidates_status",
        ),
        sa.CheckConstraint(
            "purged_at IS NULL OR "
            "(details IS NULL AND description IS NULL AND evidence IS NULL)",
            name="ck_graph_entity_merge_candidates_payload_or_purged",
        ),
        sa.UniqueConstraint(
            "job_id",
            "merge_key",
            name="uq_graph_entity_merge_candidates_job_key",
        ),
    )
    op.create_index(
        "ix_graph_entity_merge_candidates_job_status",
        table,
        ["job_id", "status"],
    )
    op.create_index(
        "ix_graph_entity_merge_candidates_purge",
        table,
        ["purged_at", "created_at"],
    )


def _create_graph_extraction_conflicts() -> None:
    table = "graph_extraction_conflicts"
    op.create_table(
        table,
        _id_column(),
        _fk_column(
            "job_id",
            "graph_extraction_jobs.id",
            "fk_graph_extraction_conflicts_job",
            "CASCADE",
        ),
        _fk_column(
            "library_id",
            "sys_libraries.id",
            "fk_graph_extraction_conflicts_library",
            "CASCADE",
        ),
        sa.Column("conflict_key", sa.String(length=64), nullable=False),
        sa.Column("conflict_type", sa.String(length=64), nullable=False),
        sa.Column(
            "entity_candidate_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "relation_candidate_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("conflicting_fields", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status", sa.String(length=32), nullable=False, server_default="open"
        ),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        _created_at(),
        _updated_at(),
        _purged_at(),
        sa.CheckConstraint(
            "status IN ('open','superseded')",
            name="ck_graph_extraction_conflicts_status",
        ),
        sa.CheckConstraint(
            "jsonb_array_length(entity_candidate_ids) > 0 OR "
            "jsonb_array_length(relation_candidate_ids) > 0",
            name="ck_graph_extraction_conflicts_members",
        ),
        sa.CheckConstraint(
            "purged_at IS NULL OR "
            "(details IS NULL AND description IS NULL AND evidence IS NULL)",
            name="ck_graph_extraction_conflicts_payload_or_purged",
        ),
        sa.UniqueConstraint(
            "job_id",
            "conflict_key",
            name="uq_graph_extraction_conflicts_job_key",
        ),
    )
    op.create_index(
        "ix_graph_extraction_conflicts_job_status",
        table,
        ["job_id", "status"],
    )
    op.create_index(
        "ix_graph_extraction_conflicts_purge",
        table,
        ["purged_at", "created_at"],
    )


def _add_formal_graph_provenance() -> None:
    op.add_column(
        "entities",
        sa.Column(
            "created_by_job_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.create_foreign_key(
        "fk_entities_created_by_job",
        "entities",
        "graph_extraction_jobs",
        ["created_by_job_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column(
        "entity_mentions",
        sa.Column(
            "created_by_job_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.add_column(
        "entity_mentions",
        sa.Column("extraction_key", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_entity_mentions_created_by_job",
        "entity_mentions",
        "graph_extraction_jobs",
        ["created_by_job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "uq_entity_mentions_extraction_key",
        "entity_mentions",
        ["extraction_key"],
        unique=True,
        postgresql_where=sa.text("extraction_key IS NOT NULL"),
    )

    op.add_column(
        "knowledge_relations",
        sa.Column(
            "created_by_job_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.add_column(
        "knowledge_relations",
        sa.Column("extraction_key", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_knowledge_relations_created_by_job",
        "knowledge_relations",
        "graph_extraction_jobs",
        ["created_by_job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "uq_knowledge_relations_extraction_key",
        "knowledge_relations",
        ["extraction_key"],
        unique=True,
        postgresql_where=sa.text("extraction_key IS NOT NULL"),
    )

    op.add_column(
        "relation_evidence",
        sa.Column(
            "created_by_job_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.create_foreign_key(
        "fk_relation_evidence_created_by_job",
        "relation_evidence",
        "graph_extraction_jobs",
        ["created_by_job_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_relation_evidence_created_by_job",
        "relation_evidence",
        type_="foreignkey",
    )
    op.drop_column("relation_evidence", "created_by_job_id")

    op.drop_index(
        "uq_knowledge_relations_extraction_key",
        table_name="knowledge_relations",
    )
    op.drop_constraint(
        "fk_knowledge_relations_created_by_job",
        "knowledge_relations",
        type_="foreignkey",
    )
    op.drop_column("knowledge_relations", "extraction_key")
    op.drop_column("knowledge_relations", "created_by_job_id")

    op.drop_index(
        "uq_entity_mentions_extraction_key",
        table_name="entity_mentions",
    )
    op.drop_constraint(
        "fk_entity_mentions_created_by_job",
        "entity_mentions",
        type_="foreignkey",
    )
    op.drop_column("entity_mentions", "extraction_key")
    op.drop_column("entity_mentions", "created_by_job_id")

    op.drop_constraint(
        "fk_entities_created_by_job",
        "entities",
        type_="foreignkey",
    )
    op.drop_column("entities", "created_by_job_id")

    op.drop_table("graph_relation_candidate_evidence")
    op.drop_table("graph_entity_candidate_evidence")
    op.drop_table("graph_relation_occurrences")
    op.drop_table("graph_entity_occurrences")
    op.drop_table("graph_entity_merge_candidates")
    op.drop_table("graph_extraction_conflicts")
    op.drop_table("graph_relation_candidates")
    op.drop_table("graph_entity_candidates")
