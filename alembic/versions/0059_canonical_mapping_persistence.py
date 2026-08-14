"""Add append-only canonical mapping result persistence."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0059"
down_revision: Union[str, None] = "0058"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # M3 direct-insert guards recompute the same SHA-256 identities used by
    # the pure contract.  pgcrypto is a database capability, not an
    # application/provider dependency; leave the extension installed on
    # downgrade because other migrations may use it.
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
    op.create_table(
        "graph_mapping_authority_snapshots",
        sa.Column("authority_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("authority_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "authority_schema_version",
            sa.String(length=64),
            server_default="mapping_authority_snapshot_v1",
            nullable=False,
        ),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("extraction_unit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("extraction_occurrence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ontology_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ontology_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("ontology_contract_version", sa.String(length=64), nullable=False),
        sa.Column("registry_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("registry_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_table", sa.String(length=64), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_key", sa.String(length=128), nullable=False),
        sa.Column("source_version", sa.String(length=128), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "authority_schema_version = 'mapping_authority_snapshot_v1'",
            name="ck_graph_mapping_authority_schema_version",
        ),
        sa.CheckConstraint(
            "authority_fingerprint ~ '^[0-9a-f]{64}$' AND "
            "registry_snapshot_hash ~ '^[0-9a-f]{64}$' AND "
            "ontology_snapshot_hash ~ '^[0-9a-f]{64}$' AND "
            "source_hash ~ '^[0-9a-f]{64}$' AND source_hash <> repeat('0', 64)",
            name="ck_graph_mapping_authority_hashes",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(registry_snapshot) = 'object' AND "
            "pg_column_size(registry_snapshot) <= 131072",
            name="ck_graph_mapping_authority_snapshot_json",
        ),
        sa.CheckConstraint("revision_no >= 1", name="ck_graph_mapping_authority_revision_no"),
        sa.CheckConstraint(
            "source_table = 'graph_extraction_jobs' AND source_id = job_id AND "
            "source_kind = 'graph_extraction_job' AND source_version = 'ontology_snapshot_v1' "
            "AND source_key = job_id::text",
            name="ck_graph_mapping_authority_repository_source",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"], ["sys_libraries.id"], ondelete="RESTRICT",
            name="fk_graph_mapping_authority_library",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], ondelete="RESTRICT",
            name="fk_graph_mapping_authority_document",
        ),
        sa.ForeignKeyConstraint(
            ["document_revision_id"], ["document_revisions.id"], ondelete="RESTRICT",
            name="fk_graph_mapping_authority_revision",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["graph_extraction_jobs.id"], ondelete="RESTRICT",
            name="fk_graph_mapping_authority_job",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_unit_id"], ["graph_extraction_units.id"], ondelete="RESTRICT",
            name="fk_graph_mapping_authority_unit",
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"], ["graph_raw_claims.id"], ondelete="RESTRICT",
            name="fk_graph_mapping_authority_claim",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_occurrence_id"],
            ["graph_raw_claim_occurrences.extraction_occurrence_id"],
            ondelete="RESTRICT",
            name="fk_graph_mapping_authority_occurrence",
        ),
        sa.ForeignKeyConstraint(
            ["ontology_version_id"], ["ontology_versions.id"], ondelete="RESTRICT",
            name="fk_graph_mapping_authority_ontology",
        ),
        sa.PrimaryKeyConstraint("authority_id"),
        sa.UniqueConstraint("authority_fingerprint", name="uq_graph_mapping_authority_fingerprint"),
    )
    op.create_index(
        "ix_graph_mapping_authority_scope",
        "graph_mapping_authority_snapshots",
        ["library_id", "document_revision_id", "job_id", "extraction_unit_id"],
    )
    op.create_table(
        "graph_claim_mappings",
        sa.Column("mapping_result_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mapping_attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mapping_attempt_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("mapping_result_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "mapping_schema_version",
            sa.String(length=64),
            server_default="canonical_mapping_v1",
            nullable=False,
        ),
        sa.Column("mapping_schema_hash", sa.String(length=64), nullable=False),
        sa.Column("canonical_schema_hash", sa.String(length=64), nullable=False),
        sa.Column("raw_claim_authority_sha256", sa.String(length=64), nullable=False),
        sa.Column("mapping_version", sa.Integer(), nullable=False),
        sa.Column("authority_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("authority_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("extraction_unit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("claim_content_scoped_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("extraction_occurrence_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("extraction_occurrence_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("surface_raw_predicate", sa.String(length=256), nullable=False),
        sa.Column("surface_direction", sa.String(length=32), nullable=False),
        sa.Column("ontology_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ontology_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("ontology_contract_version", sa.String(length=64), nullable=False),
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decision_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("decision_kind", sa.String(length=40), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("semantic_status", sa.String(length=16), nullable=False),
        sa.Column("canonical_relation_key", sa.String(length=128), nullable=True),
        sa.Column("canonical_direction", sa.String(length=32), nullable=True),
        sa.Column("endpoint_transform", sa.String(length=16), nullable=True),
        sa.Column("predicate_transform", sa.String(length=16), nullable=True),
        sa.Column("mapping_confidence", sa.Float(), nullable=True),
        sa.Column("remap_generation", sa.Integer(), server_default="0", nullable=False),
        sa.Column("supersedes_mapping_result_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("supersedes_mapping_result_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("lineage_root_mapping_result_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lineage_root_mapping_result_fingerprint", sa.String(length=64), nullable=True),
        sa.Column("evidence_bindings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_evidence_ref_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("target_evidence_ref_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("mapping_evidence_ref_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result_projection", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "mapping_schema_version = 'canonical_mapping_v1'",
            name="ck_graph_claim_mappings_schema_version",
        ),
        sa.CheckConstraint(
            "outcome IN ('mapped','ambiguous','blocked','rejected')",
            name="ck_graph_claim_mappings_outcome",
        ),
        sa.CheckConstraint(
            "reason_code IS NULL OR reason_code IN "
            "('unknown_predicate','unknown_source_type','unknown_target_type',"
            "'unknown_direction','ambiguous_mapping','ambiguous_endpoint',"
            "'ontology_relation_not_allowed','ontology_snapshot_mismatch',"
            "'evidence_missing','evidence_invalid','scope_mismatch','no_explicit_mapping',"
            "'unsupported_negation','unsupported_modality','unsupported_qualifier',"
            "'unsupported_valid_time','unsupported_effective_time','mapper_error')",
            name="ck_graph_claim_mappings_reason_code",
        ),
        sa.CheckConstraint(
            "semantic_status IN ('preserved','ambiguous','blocked')",
            name="ck_graph_claim_mappings_semantic_status",
        ),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
            "mapping_version >= 1 AND revision_no >= 1 AND remap_generation >= 0",
            name="ck_graph_claim_mappings_versions",
        ),
        sa.CheckConstraint(
            "(remap_generation = 0 AND supersedes_mapping_result_id IS NULL AND "
            "supersedes_mapping_result_fingerprint IS NULL AND lineage_root_mapping_result_id IS NULL AND "
            "lineage_root_mapping_result_fingerprint IS NULL) OR "
            "(remap_generation > 0 AND supersedes_mapping_result_id IS NOT NULL AND "
            "supersedes_mapping_result_fingerprint IS NOT NULL AND lineage_root_mapping_result_id IS NOT NULL AND "
            "lineage_root_mapping_result_fingerprint IS NOT NULL)",
            name="ck_graph_claim_mappings_remap_fields",
        ),
        sa.CheckConstraint(
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
        sa.CheckConstraint(
            "mapping_confidence IS NULL OR "
            "(mapping_confidence::text NOT IN ('NaN', 'Infinity', '-Infinity') AND "
            "mapping_confidence >= 0 AND mapping_confidence <= 1)",
            name="ck_graph_claim_mappings_confidence",
        ),
        sa.CheckConstraint(
            "(outcome = 'mapped' AND canonical_relation_key IS NOT NULL AND "
            "canonical_direction IS NOT NULL AND endpoint_transform IS NOT NULL AND "
            "predicate_transform IS NOT NULL AND mapping_confidence IS NOT NULL) OR "
            "(outcome IN ('ambiguous','blocked','rejected') AND canonical_relation_key IS NULL AND "
            "canonical_direction IS NULL AND endpoint_transform IS NULL AND "
            "predicate_transform IS NULL AND "
            "(outcome = 'ambiguous' OR mapping_confidence IS NULL))",
            name="ck_graph_claim_mappings_canonical_key_presence",
        ),
        sa.CheckConstraint(
            "surface_direction IN ('source_to_target','target_to_source','undirected','unknown')",
            name="ck_graph_claim_mappings_surface_direction",
        ),
        sa.CheckConstraint(
            "canonical_direction IS NULL OR canonical_direction IN ('source_to_target','target_to_source','undirected')",
            name="ck_graph_claim_mappings_canonical_direction",
        ),
        sa.CheckConstraint(
            "endpoint_transform IS NULL OR endpoint_transform IN ('identity','swap')",
            name="ck_graph_claim_mappings_endpoint_transform",
        ),
        sa.CheckConstraint(
            "predicate_transform IS NULL OR predicate_transform IN ('identity','inverse','symmetric')",
            name="ck_graph_claim_mappings_predicate_transform",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(result_projection) = 'object' AND "
            "jsonb_typeof(evidence_bindings) = 'array' AND "
            "jsonb_typeof(source_evidence_ref_ids) = 'array' AND "
            "jsonb_typeof(target_evidence_ref_ids) = 'array' AND "
            "jsonb_typeof(mapping_evidence_ref_ids) = 'array' AND "
            "pg_column_size(result_projection) <= 131072",
            name="ck_graph_claim_mappings_json_projection",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"], ["sys_libraries.id"], ondelete="RESTRICT",
            name="fk_graph_claim_mappings_library",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], ondelete="RESTRICT",
            name="fk_graph_claim_mappings_document",
        ),
        sa.ForeignKeyConstraint(
            ["document_revision_id"], ["document_revisions.id"], ondelete="RESTRICT",
            name="fk_graph_claim_mappings_revision",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["graph_extraction_jobs.id"], ondelete="RESTRICT",
            name="fk_graph_claim_mappings_job",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_unit_id"], ["graph_extraction_units.id"], ondelete="RESTRICT",
            name="fk_graph_claim_mappings_unit",
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"], ["graph_raw_claims.id"], ondelete="RESTRICT",
            name="fk_graph_claim_mappings_claim",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_occurrence_id"],
            ["graph_raw_claim_occurrences.extraction_occurrence_id"],
            ondelete="RESTRICT",
            name="fk_graph_claim_mappings_occurrence",
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"], ["graph_claim_decisions.decision_id"], ondelete="RESTRICT",
            name="fk_graph_claim_mappings_decision",
        ),
        sa.ForeignKeyConstraint(
            ["ontology_version_id"], ["ontology_versions.id"], ondelete="RESTRICT",
            name="fk_graph_claim_mappings_ontology",
        ),
        sa.ForeignKeyConstraint(
            ["authority_id"], ["graph_mapping_authority_snapshots.authority_id"],
            ondelete="RESTRICT", name="fk_graph_claim_mappings_authority",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_mapping_result_id"], ["graph_claim_mappings.mapping_result_id"],
            ondelete="RESTRICT", name="fk_graph_claim_mappings_supersedes",
        ),
        sa.ForeignKeyConstraint(
            ["lineage_root_mapping_result_id"], ["graph_claim_mappings.mapping_result_id"],
            ondelete="RESTRICT", name="fk_graph_claim_mappings_lineage_root",
        ),
        sa.PrimaryKeyConstraint("mapping_result_id"),
        sa.UniqueConstraint(
            "mapping_result_fingerprint",
            name="uq_graph_claim_mappings_result_fingerprint",
        ),
    )
    op.create_index(
        "ix_graph_claim_mappings_scope_created",
        "graph_claim_mappings",
        ["library_id", "document_revision_id", "created_at", "mapping_result_id"],
    )
    op.create_index(
        "ix_graph_claim_mappings_scope_claim",
        "graph_claim_mappings",
        ["library_id", "document_revision_id", "claim_id", "created_at"],
    )
    op.create_index(
        "ix_graph_claim_mappings_scope_occurrence",
        "graph_claim_mappings",
        ["library_id", "document_revision_id", "extraction_occurrence_id", "created_at"],
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_canonical_json(value jsonb)
            RETURNS text
            LANGUAGE plpgsql
            IMMUTABLE STRICT
            AS $$
            DECLARE
                kind text;
                item_key text;
                item_value jsonb;
                item_ordinal bigint;
                output text := '';
                first_item boolean := true;
            BEGIN
                kind := jsonb_typeof($1);
                IF kind = 'object' THEN
                    output := '{';
                    FOR item_key, item_value IN
                        SELECT object_entry.key, object_entry.value
                          FROM jsonb_each($1) AS object_entry(key, value)
                         ORDER BY object_entry.key COLLATE "C"
                    LOOP
                        IF NOT first_item THEN
                            output := output || ',';
                        END IF;
                        first_item := false;
                        output := output || to_jsonb(item_key)::text || ':'
                            || graph_mapping_canonical_json(item_value);
                    END LOOP;
                    RETURN output || '}';
                ELSIF kind = 'array' THEN
                    output := '[';
                    FOR item_value, item_ordinal IN
                        SELECT array_entry.value, array_entry.ordinality
                          FROM jsonb_array_elements($1) WITH ORDINALITY
                               AS array_entry(value, ordinality)
                         ORDER BY array_entry.ordinality
                    LOOP
                        IF NOT first_item THEN
                            output := output || ',';
                        END IF;
                        first_item := false;
                        output := output || graph_mapping_canonical_json(item_value);
                    END LOOP;
                    RETURN output || ']';
                ELSIF kind = 'string' THEN
                    RETURN to_jsonb($1 #>> '{}')::text;
                END IF;
                RETURN $1::text;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_json_sha256(value jsonb)
            RETURNS text
            LANGUAGE sql
            IMMUTABLE STRICT
            AS $$
                SELECT encode(
                    digest(convert_to(graph_mapping_canonical_json(value), 'UTF8'), 'sha256'),
                    'hex'
                )
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_text_sha256(value text)
            RETURNS text
            LANGUAGE sql
            IMMUTABLE STRICT
            AS $$
                SELECT encode(digest(convert_to(value, 'UTF8'), 'sha256'), 'hex')
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_uuid5(namespace uuid, name text)
            RETURNS uuid
            LANGUAGE plpgsql
            IMMUTABLE STRICT
            AS $$
            DECLARE
                hashed bytea;
                encoded text;
            BEGIN
                hashed := digest(uuid_send(namespace) || convert_to(name, 'UTF8'), 'sha1');
                hashed := set_byte(hashed, 6, (get_byte(hashed, 6) & 15) | 80);
                hashed := set_byte(hashed, 8, (get_byte(hashed, 8) & 63) | 128);
                encoded := encode(substring(hashed FROM 1 FOR 16), 'hex');
                RETURN (
                    substr(encoded, 1, 8) || '-' ||
                    substr(encoded, 9, 4) || '-' ||
                    substr(encoded, 13, 4) || '-' ||
                    substr(encoded, 17, 4) || '-' ||
                    substr(encoded, 21, 12)
                )::uuid;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_authority_fingerprint(
                authority_schema_version text,
                scope jsonb,
                ontology_snapshot_hash text,
                registry_snapshot_hash text,
                registry_snapshot jsonb,
                source_table text,
                source_id uuid,
                source_kind text,
                source_key text,
                source_version text,
                source_hash text
            )
            RETURNS text
            LANGUAGE sql
            IMMUTABLE STRICT
            AS $$
                SELECT graph_mapping_json_sha256(
                    jsonb_build_object(
                        'authority_schema_version', authority_schema_version,
                        'scope', scope,
                        'ontology_snapshot_hash', ontology_snapshot_hash,
                        'registry_snapshot_hash', registry_snapshot_hash,
                        'registry_snapshot', registry_snapshot,
                        'source_table', source_table,
                        'source_id', source_id::text,
                        'source_kind', source_kind,
                        'source_key', source_key,
                        'source_version', source_version,
                        'source_hash', source_hash
                    )
                )
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_json_exact_keys(value jsonb, expected_keys text[])
            RETURNS boolean
            LANGUAGE plpgsql
            IMMUTABLE
            AS $$
            BEGIN
                IF value IS NULL OR jsonb_typeof(value) <> 'object' THEN
                    RETURN false;
                END IF;
                RETURN (
                    ARRAY(
                        SELECT key
                          FROM jsonb_object_keys(value) AS object_key(key)
                         ORDER BY key COLLATE "C"
                    ) IS NOT DISTINCT FROM ARRAY(
                        SELECT key
                          FROM unnest(expected_keys) AS expected_key(key)
                         ORDER BY key COLLATE "C"
                    )
                );
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_validate_timestamp_text(value jsonb)
            RETURNS boolean
            LANGUAGE plpgsql
            IMMUTABLE
            AS $$
            DECLARE
                timestamp_value text;
                parsed timestamptz;
            BEGIN
                IF value IS NULL OR jsonb_typeof(value) <> 'string' THEN
                    RETURN false;
                END IF;
                timestamp_value := value #>> '{}';
                IF timestamp_value !~
                   '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])$' THEN
                    RETURN false;
                END IF;
                BEGIN
                    parsed := timestamp_value::timestamptz;
                EXCEPTION WHEN OTHERS THEN
                    RETURN false;
                END;
                RETURN isfinite(parsed);
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_timestamp_text(value timestamptz)
            RETURNS text
            LANGUAGE sql
            IMMUTABLE STRICT
            AS $$
                SELECT regexp_replace(
                    to_char($1 AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
                    '\.000000$',
                    ''
                ) || 'Z'
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_decision_fingerprint(
                decision_schema_version text,
                decision_version integer,
                library_id uuid,
                document_id uuid,
                document_revision_id uuid,
                revision_no integer,
                claim_id uuid,
                extraction_occurrence_id uuid,
                decision_kind text,
                status text,
                reason_code text,
                created_by_kind text,
                producer_key text,
                producer_version text,
                proposal jsonb
            )
            RETURNS text
            LANGUAGE sql
            IMMUTABLE
            AS $$
                SELECT graph_mapping_json_sha256(
                    jsonb_build_object(
                        'decision_schema_version', decision_schema_version,
                        'decision_version', decision_version,
                        'library_id', library_id::text,
                        'document_id', document_id::text,
                        'document_revision_id', document_revision_id::text,
                        'revision_no', revision_no,
                        'claim_id', claim_id::text,
                        'extraction_occurrence_id', extraction_occurrence_id::text,
                        'decision_kind', decision_kind,
                        'status', status,
                        'reason_code', reason_code,
                        'created_by_kind', created_by_kind,
                        'producer_key', producer_key,
                        'producer_version', producer_version,
                        'proposal', proposal
                    )
                )
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_evidence_bindings_identity(value jsonb)
            RETURNS jsonb
            LANGUAGE sql
            IMMUTABLE STRICT
            AS $$
                SELECT COALESCE(
                    jsonb_agg(
                        (item - 'binding_fingerprint'::text - 'attestation'::text) ||
                        jsonb_build_object(
                            'attestation', (item->'attestation') - 'validated_at'::text
                        )
                        ORDER BY item->>'evidence_ref_id', item->>'stable_evidence_identity_hash'
                    ),
                    '[]'::jsonb
                )
                FROM jsonb_array_elements(value) AS entries(item)
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION graph_mapping_evidence_bindings_attempt(value jsonb)
            RETURNS jsonb
            LANGUAGE sql
            IMMUTABLE STRICT
            AS $$
                SELECT COALESCE(
                    jsonb_agg(
                        (item - 'attestation'::text) || jsonb_build_object(
                            'attestation', (item->'attestation') - 'validated_at'::text
                        )
                        ORDER BY item->>'evidence_ref_id', item->>'stable_evidence_identity_hash'
                    ),
                    '[]'::jsonb
                )
                FROM jsonb_array_elements(value) AS entries(item)
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION graph_mapping_restore_evidence_ref(value jsonb, references_payload jsonb)
            RETURNS jsonb
            LANGUAGE sql
            IMMUTABLE STRICT
            AS $$
                SELECT CASE
                    WHEN ($1->>'evidence_ref') IS NULL THEN $1
                    WHEN EXISTS (
                        SELECT 1
                          FROM jsonb_array_elements($2) AS reference_item(value)
                         WHERE (reference_item.value->>'ref_id') = ($1->>'evidence_ref')
                    ) THEN $1
                    WHEN (
                        SELECT COUNT(*)
                          FROM jsonb_array_elements($2) AS reference_item(value)
                         WHERE graph_mapping_json_sha256(
                             reference_item.value - ARRAY['ref_id','job_id','extraction_unit_id']::text[]
                         ) = ($1->>'evidence_ref')
                    ) = 1 THEN (
                        SELECT jsonb_set(
                            $1,
                            '{evidence_ref}',
                            to_jsonb(reference_item.value->>'ref_id'),
                            false
                        )
                          FROM jsonb_array_elements($2) AS reference_item(value)
                         WHERE graph_mapping_json_sha256(
                             reference_item.value - ARRAY['ref_id','job_id','extraction_unit_id']::text[]
                         ) = ($1->>'evidence_ref')
                         LIMIT 1
                    )
                    ELSE NULL
                END
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION graph_mapping_restore_qualifiers(value jsonb, references_payload jsonb)
            RETURNS jsonb
            LANGUAGE sql
            IMMUTABLE STRICT
            AS $$
                SELECT COALESCE(
                    jsonb_agg(
                        graph_mapping_restore_evidence_ref(item.value, $2)
                        ORDER BY item.ordinality
                    ),
                    '[]'::jsonb
                )
                  FROM jsonb_array_elements($1) WITH ORDINALITY
                       AS item(value, ordinality)
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_json_integer(value jsonb)
            RETURNS boolean
            LANGUAGE sql
            IMMUTABLE
            AS $$
                SELECT $1 IS NOT NULL
                   AND jsonb_typeof($1) = 'number'
                   AND ($1 #>> '{}') ~ '^-?(0|[1-9][0-9]*)$'
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_json_number(value jsonb)
            RETURNS boolean
            LANGUAGE sql
            IMMUTABLE
            AS $$
                SELECT $1 IS NOT NULL AND jsonb_typeof($1) = 'number'
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_validate_text_span(value jsonb)
            RETURNS boolean
            LANGUAGE plpgsql
            IMMUTABLE
            AS $$
            DECLARE
                range_item jsonb;
            BEGIN
                IF value IS NULL OR jsonb_typeof(value) = 'null' THEN
                    RETURN true;
                END IF;
                IF jsonb_typeof(value) <> 'object' THEN
                    RETURN false;
                END IF;
                IF (value ? 'start' AND NOT graph_mapping_json_integer(value->'start'))
                   OR (value ? 'end' AND NOT graph_mapping_json_integer(value->'end')) THEN
                    RETURN false;
                END IF;
                IF value ? 'ranges' THEN
                    IF jsonb_typeof(value->'ranges') <> 'array' THEN
                        RETURN false;
                    END IF;
                    FOR range_item IN SELECT item.value FROM jsonb_array_elements(value->'ranges') AS item(value)
                    LOOP
                        IF jsonb_typeof(range_item) <> 'object'
                           OR (range_item ? 'start' AND NOT graph_mapping_json_integer(range_item->'start'))
                           OR (range_item ? 'end' AND NOT graph_mapping_json_integer(range_item->'end')) THEN
                            RETURN false;
                        END IF;
                    END LOOP;
                END IF;
                RETURN true;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_validate_evidence_reference(value jsonb)
            RETURNS boolean
            LANGUAGE plpgsql
            IMMUTABLE
            AS $$
            DECLARE
                locator jsonb;
                source jsonb;
                field_name text;
            BEGIN
                IF value IS NULL OR jsonb_typeof(value) <> 'object'
                   OR NOT graph_mapping_json_integer(value->'revision_no') THEN
                    RETURN false;
                END IF;
                IF NOT (value ? 'locator') AND NOT (value ? 'source_span') THEN
                    RETURN false;
                END IF;
                IF value ? 'source_span' AND jsonb_typeof(value->'source_span') <> 'null'
                   AND NOT graph_mapping_validate_text_span(value->'source_span') THEN
                    RETURN false;
                END IF;
                locator := value->'locator';
                IF locator IS NULL OR jsonb_typeof(locator) = 'null' THEN
                    RETURN true;
                END IF;
                IF jsonb_typeof(locator) <> 'object'
                   OR NOT graph_mapping_json_integer(locator->'revision_no')
                   OR NOT graph_mapping_json_integer(locator->'ordinal') THEN
                    RETURN false;
                END IF;
                source := locator->'source';
                IF source IS NULL OR jsonb_typeof(source) = 'null' THEN
                    RETURN true;
                END IF;
                IF jsonb_typeof(source) <> 'object' THEN
                    RETURN false;
                END IF;
                FOREACH field_name IN ARRAY ARRAY['page'::text, 'row'::text]
                LOOP
                    IF source ? field_name
                       AND jsonb_typeof(source->field_name) <> 'null'
                       AND NOT graph_mapping_validate_text_span(source->field_name) THEN
                        RETURN false;
                    END IF;
                END LOOP;
                IF source ? 'text' AND jsonb_typeof(source->'text') <> 'null'
                   AND NOT graph_mapping_validate_text_span(source->'text') THEN
                    RETURN false;
                END IF;
                IF source ? 'table' AND jsonb_typeof(source->'table') <> 'null'
                   AND (jsonb_typeof(source->'table') <> 'object'
                        OR (source->'table' ? 'index'
                            AND NOT graph_mapping_json_integer(source->'table'->'index'))) THEN
                    RETURN false;
                END IF;
                IF source ? 'column' AND jsonb_typeof(source->'column') <> 'null'
                   AND (jsonb_typeof(source->'column') <> 'object'
                        OR (source->'column' ? 'start'
                            AND jsonb_typeof(source->'column'->'start') NOT IN ('number', 'string'))
                        OR (source->'column' ? 'end'
                            AND jsonb_typeof(source->'column'->'end') NOT IN ('number', 'string'))) THEN
                    RETURN false;
                END IF;
                IF source ? 'bbox' AND jsonb_typeof(source->'bbox') <> 'null' THEN
                    IF jsonb_typeof(source->'bbox') <> 'object' THEN
                        RETURN false;
                    END IF;
                    FOREACH field_name IN ARRAY ARRAY[
                        'x_min'::text, 'y_min'::text, 'x_max'::text, 'y_max'::text,
                        'width'::text, 'height'::text
                    ]
                    LOOP
                        IF source->'bbox' ? field_name
                           AND jsonb_typeof(source->'bbox'->field_name) <> 'null'
                           AND NOT graph_mapping_json_number(source->'bbox'->field_name) THEN
                            RETURN false;
                        END IF;
                    END LOOP;
                END IF;
                RETURN true;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_evidence_identity(references_payload jsonb, ref_id text)
            RETURNS text
            LANGUAGE sql
            IMMUTABLE
            AS $$
                SELECT graph_mapping_json_sha256(
                    reference.value - ARRAY['ref_id','job_id','extraction_unit_id']::text[]
                )
                  FROM jsonb_array_elements(references_payload) AS reference(value)
                 WHERE (reference.value->>'ref_id') = ref_id
                 LIMIT 1
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_semantic_value(value jsonb, references_payload jsonb)
            RETURNS jsonb
            LANGUAGE sql
            IMMUTABLE
            AS $$
                SELECT CASE
                    WHEN value IS NULL OR jsonb_typeof(value) = 'null'
                         OR jsonb_typeof(value) <> 'object'
                         OR (value->>'evidence_ref') IS NULL THEN value
                    ELSE COALESCE(
                        (
                            SELECT jsonb_set(
                                value,
                                '{evidence_ref}',
                                to_jsonb(graph_mapping_evidence_identity(
                                    references_payload, value->>'evidence_ref'
                                )),
                                false
                            )
                            WHERE graph_mapping_evidence_identity(
                                references_payload, value->>'evidence_ref'
                            ) IS NOT NULL
                        ),
                        value
                    )
                END
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_semantic_qualifiers(value jsonb, references_payload jsonb)
            RETURNS jsonb
            LANGUAGE sql
            IMMUTABLE
            AS $$
                SELECT COALESCE(
                    jsonb_agg(
                        graph_mapping_semantic_value(item.value, references_payload)
                        ORDER BY graph_mapping_canonical_json(
                            graph_mapping_semantic_value(item.value, references_payload)
                        )
                    ),
                    '[]'::jsonb
                )
                  FROM jsonb_array_elements(COALESCE(value, '[]'::jsonb)) AS item(value)
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_semantic_ref_ids(value jsonb)
            RETURNS jsonb
            LANGUAGE sql
            IMMUTABLE STRICT
            AS $$
                WITH selected(ref_id) AS (
                    SELECT value->'negation'->>'evidence_ref'
                    UNION SELECT value->'modality'->>'evidence_ref'
                    UNION SELECT value->'valid_time'->>'evidence_ref'
                    UNION SELECT value->'effective_time'->>'evidence_ref'
                    UNION
                    SELECT item.value->>'evidence_ref'
                  FROM jsonb_array_elements(COALESCE((value->'qualifiers'), '[]'::jsonb)) AS item(value)
                )
                SELECT COALESCE(
                    jsonb_agg(to_jsonb(ref_id) ORDER BY ref_id) FILTER (WHERE ref_id IS NOT NULL),
                    '[]'::jsonb
                )
                  FROM selected
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_semantic_evidence_bound(value jsonb, references_payload jsonb)
            RETURNS boolean
            LANGUAGE sql
            IMMUTABLE STRICT
            AS $$
                SELECT NOT EXISTS (
                    SELECT 1
                      FROM jsonb_array_elements_text(graph_mapping_semantic_ref_ids(value)) AS selected(ref_id)
                     WHERE graph_mapping_evidence_identity(references_payload, selected.ref_id) IS NULL
                )
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_semantic_identity_hashes(value jsonb, references_payload jsonb)
            RETURNS jsonb
            LANGUAGE sql
            IMMUTABLE STRICT
            AS $$
                WITH identities AS (
                    SELECT DISTINCT graph_mapping_evidence_identity(
                        references_payload, item.value #>> '{}'
                    ) AS identity_hash
                      FROM jsonb_array_elements(graph_mapping_semantic_ref_ids(value)) AS item(value)
                )
                SELECT COALESCE(
                    jsonb_agg(to_jsonb(identity_hash) ORDER BY identity_hash)
                    FILTER (WHERE identity_hash IS NOT NULL),
                    '[]'::jsonb
                )
                  FROM identities
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_evidence_snapshots(value jsonb)
            RETURNS jsonb
            LANGUAGE sql
            IMMUTABLE STRICT
            AS $$
                SELECT COALESCE(
                    jsonb_agg(
                        snapshot.base || jsonb_build_object(
                            'snapshot_fingerprint', graph_mapping_json_sha256(snapshot.base)
                        )
                        ORDER BY snapshot.ref_id
                    ),
                    '[]'::jsonb
                )
                  FROM (
                      SELECT
                          reference.value->>'ref_id' AS ref_id,
                          jsonb_build_object(
                              'evidence_ref_id', reference.value->>'ref_id',
                              'stable_evidence_identity_hash', graph_mapping_json_sha256(
                                  reference.value - ARRAY['ref_id','job_id','extraction_unit_id']::text[]
                              ),
                              'evidence_reference_sha256', graph_mapping_json_sha256(reference.value),
                              'quote_sha256', reference.value->>'quote_sha256',
                              'unit_text_sha256', reference.value->>'unit_text_sha256',
                              'locator_sha256', CASE
                                  WHEN (reference.value->'locator') IS NULL
                                       OR jsonb_typeof(reference.value->'locator') = 'null'
                                  THEN NULL
                                  ELSE graph_mapping_json_sha256(reference.value->'locator')
                              END,
                              'source_span_hash', CASE
                                  WHEN (reference.value->'source_span') IS NULL
                                       OR jsonb_typeof(reference.value->'source_span') = 'null'
                                  THEN NULL
                                  ELSE graph_mapping_json_sha256(reference.value->'source_span')
                              END
                          ) AS base
                        FROM jsonb_array_elements(value) AS reference(value)
                  ) AS snapshot
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_qualifier_identity_hashes(value jsonb, references_payload jsonb)
            RETURNS jsonb
            LANGUAGE sql
            IMMUTABLE STRICT
            AS $$
                SELECT COALESCE(
                    jsonb_agg(to_jsonb(identity_hash) ORDER BY identity_hash)
                    FILTER (WHERE identity_hash IS NOT NULL),
                    '[]'::jsonb
                )
                  FROM (
                      SELECT DISTINCT graph_mapping_evidence_identity(
                          references_payload, item.value->>'evidence_ref'
                      ) AS identity_hash
                        FROM jsonb_array_elements(COALESCE(value, '[]'::jsonb)) AS item(value)
                  ) AS identities
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_semantic_projection(value jsonb, references_payload jsonb)
            RETURNS jsonb
            LANGUAGE sql
            IMMUTABLE STRICT
            AS $$
                WITH core AS (
                    SELECT jsonb_build_object(
                        'negation', graph_mapping_restore_evidence_ref(value->'negation', references_payload),
                        'modality', graph_mapping_restore_evidence_ref(value->'modality', references_payload),
                        'qualifiers', graph_mapping_restore_qualifiers(value->'qualifiers', references_payload),
                        'valid_time', graph_mapping_restore_evidence_ref(value->'valid_time', references_payload),
                        'effective_time', graph_mapping_restore_evidence_ref(value->'effective_time', references_payload)
                    ) AS payload
                ), base AS (
                    SELECT jsonb_build_object(
                        'semantic_projection_version', 'canonical_mapping_semantics_v1',
                        'negation_value', core.payload->'negation'->'value',
                        'negation_evidence_identity_hash', graph_mapping_evidence_identity(
                            references_payload, core.payload->'negation'->>'evidence_ref'
                        ),
                        'modality_present', (
                            core.payload->'modality'->'value' IS NOT NULL
                            AND jsonb_typeof(core.payload->'modality'->'value') <> 'null'
                        ),
                        'modality_value_hash', CASE
                            WHEN (core.payload->'modality'->'value') IS NULL
                                 OR jsonb_typeof(core.payload->'modality'->'value') = 'null'
                            THEN NULL
                            ELSE graph_mapping_json_sha256(core.payload->'modality'->'value')
                        END,
                        'modality_evidence_identity_hash', graph_mapping_evidence_identity(
                            references_payload, core.payload->'modality'->>'evidence_ref'
                        ),
                        'qualifier_present', jsonb_array_length(core.payload->'qualifiers') > 0,
                        'qualifier_set_hash', graph_mapping_json_sha256(
                            graph_mapping_semantic_qualifiers(core.payload->'qualifiers', references_payload)
                        ),
                        'qualifier_evidence_identity_hashes', graph_mapping_qualifier_identity_hashes(
                            core.payload->'qualifiers', references_payload
                        ),
                        'valid_time_present', (
                            core.payload->'valid_time' IS NOT NULL
                            AND jsonb_typeof(core.payload->'valid_time') <> 'null'
                        ),
                        'valid_time_hash', CASE
                            WHEN (core.payload->'valid_time') IS NULL
                                 OR jsonb_typeof(core.payload->'valid_time') = 'null'
                            THEN NULL
                            ELSE graph_mapping_json_sha256(core.payload->'valid_time')
                        END,
                        'valid_time_evidence_identity_hash', graph_mapping_evidence_identity(
                            references_payload, core.payload->'valid_time'->>'evidence_ref'
                        ),
                        'effective_time_present', (
                            core.payload->'effective_time' IS NOT NULL
                            AND jsonb_typeof(core.payload->'effective_time') <> 'null'
                        ),
                        'effective_time_hash', CASE
                            WHEN (core.payload->'effective_time') IS NULL
                                 OR jsonb_typeof(core.payload->'effective_time') = 'null'
                            THEN NULL
                            ELSE graph_mapping_json_sha256(core.payload->'effective_time')
                        END,
                        'effective_time_evidence_identity_hash', graph_mapping_evidence_identity(
                            references_payload, core.payload->'effective_time'->>'evidence_ref'
                        ),
                        'evidence_ref_ids', graph_mapping_semantic_ref_ids(core.payload),
                        'evidence_identity_hashes', graph_mapping_semantic_identity_hashes(
                            core.payload, references_payload
                        )
                    ) AS payload
                      FROM core
                )
                SELECT base.payload || jsonb_build_object(
                    'semantic_projection_fingerprint', graph_mapping_json_sha256(base.payload)
                )
                  FROM base
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_mapping_claim_snapshot(raw_claim jsonb, semantic_projection jsonb)
            RETURNS jsonb
            LANGUAGE sql
            IMMUTABLE STRICT
            AS $$
                WITH base AS (
                    SELECT jsonb_build_object(
                        'snapshot_version', 'raw_claim_mapping_snapshot_v1',
                        'claim_id', raw_claim->>'claim_id',
                        'library_id', raw_claim->>'library_id',
                        'document_id', raw_claim->>'document_id',
                        'document_revision_id', raw_claim->>'document_revision_id',
                        'revision_no', raw_claim->'revision_no',
                        'job_id', raw_claim->>'job_id',
                        'extraction_unit_id', raw_claim->>'extraction_unit_id',
                        'extraction_occurrence_id', raw_claim->>'extraction_occurrence_id',
                        'claim_content_scoped_fingerprint', raw_claim->>'content_scoped_claim_fingerprint',
                        'extraction_occurrence_fingerprint', raw_claim->>'extraction_occurrence_fingerprint',
                        'raw_predicate_sha256', graph_mapping_text_sha256(raw_claim->>'raw_predicate'),
                        'surface_direction', raw_claim->>'surface_direction',
                        'source_mention_local_id', raw_claim->'source_mention'->>'local_id',
                        'target_mention_local_id', raw_claim->'target_mention'->>'local_id',
                        'source_mention_surface_sha256', graph_mapping_text_sha256(raw_claim->'source_mention'->>'surface'),
                        'target_mention_surface_sha256', graph_mapping_text_sha256(raw_claim->'target_mention'->>'surface'),
                        'source_mention_evidence_ref', raw_claim->'source_mention'->>'evidence_ref',
                        'target_mention_evidence_ref', raw_claim->'target_mention'->>'evidence_ref',
                        'evidence_ref_ids', (
                            SELECT COALESCE(jsonb_agg(to_jsonb(reference.value->>'ref_id') ORDER BY reference.value->>'ref_id'), '[]'::jsonb)
                              FROM jsonb_array_elements(raw_claim->'evidence_refs') AS reference(value)
                        ),
                        'evidence_identity_hashes', (
                            SELECT COALESCE(jsonb_agg(to_jsonb(identity_hash) ORDER BY identity_hash), '[]'::jsonb)
                              FROM (
                                  SELECT DISTINCT graph_mapping_json_sha256(
                                      reference.value - ARRAY['ref_id','job_id','extraction_unit_id']::text[]
                                  ) AS identity_hash
                                    FROM jsonb_array_elements(raw_claim->'evidence_refs') AS reference(value)
                              ) AS identities
                        ),
                        'evidence_snapshots', graph_mapping_evidence_snapshots(raw_claim->'evidence_refs'),
                        'semantic_projection_fingerprint', semantic_projection->>'semantic_projection_fingerprint',
                        'claim_json_sha256', graph_mapping_json_sha256(raw_claim)
                    ) AS payload
                )
                SELECT base.payload || jsonb_build_object(
                    'snapshot_fingerprint', graph_mapping_json_sha256(base.payload)
                )
                  FROM base
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_extraction_jobs_scope_guard()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                ontology_library_id uuid;
                snapshot_frozen_ontology jsonb;
            BEGIN
                IF TG_OP = 'UPDATE'
                   AND NEW.library_id IS DISTINCT FROM OLD.library_id THEN
                    RAISE EXCEPTION 'graph extraction Job library scope is immutable';
                END IF;
                SELECT library_id
                  INTO ontology_library_id
                  FROM ontology_versions
                 WHERE id = NEW.ontology_version_id;
                IF ontology_library_id IS NULL
                   OR ontology_library_id IS DISTINCT FROM NEW.library_id THEN
                    RAISE EXCEPTION 'graph extraction Job ontology crosses the Library scope';
                END IF;
                IF NEW.ontology_snapshot IS NOT NULL
                   AND jsonb_typeof(NEW.ontology_snapshot) = 'object' THEN
                    IF (NEW.ontology_snapshot ? 'library_id')
                       AND (NEW.ontology_snapshot->>'library_id') IS DISTINCT FROM NEW.library_id::text THEN
                        RAISE EXCEPTION 'graph extraction Job snapshot crosses the Library scope';
                    END IF;
                    IF (NEW.ontology_snapshot ? 'ontology_version_id')
                       AND (NEW.ontology_snapshot->>'ontology_version_id') IS DISTINCT FROM
                           NEW.ontology_version_id::text THEN
                        RAISE EXCEPTION 'graph extraction Job snapshot ontology version is not bound';
                    END IF;
                    snapshot_frozen_ontology := NEW.ontology_snapshot->'frozen_ontology';
                    IF snapshot_frozen_ontology IS NOT NULL
                       AND jsonb_typeof(snapshot_frozen_ontology) = 'object' THEN
                        IF (snapshot_frozen_ontology ? 'library_id')
                           AND (snapshot_frozen_ontology->>'library_id') IS DISTINCT FROM NEW.library_id::text THEN
                            RAISE EXCEPTION 'frozen ontology snapshot crosses the Library scope';
                        END IF;
                        IF (snapshot_frozen_ontology ? 'ontology_version_id')
                           AND (snapshot_frozen_ontology->>'ontology_version_id') IS DISTINCT FROM
                               NEW.ontology_version_id::text THEN
                            RAISE EXCEPTION 'frozen ontology version is not bound to the Job';
                        END IF;
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            CREATE FUNCTION graph_extraction_jobs_ontology_snapshot_immutable_guard()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                old_placeholder boolean;
                old_draft boolean;
                new_draft boolean;
                new_frozen boolean;
            BEGIN
                IF NEW.ontology_version_id IS NOT DISTINCT FROM OLD.ontology_version_id
                   AND NEW.ontology_snapshot IS NOT DISTINCT FROM OLD.ontology_snapshot
                   AND NEW.ontology_snapshot_hash IS NOT DISTINCT FROM OLD.ontology_snapshot_hash THEN
                    RETURN NEW;
                END IF;
                IF NEW.ontology_snapshot IS NULL
                   OR jsonb_typeof(NEW.ontology_snapshot) <> 'object'
                   OR NEW.ontology_snapshot_hash IS NULL
                   OR NEW.ontology_snapshot_hash !~ '^[0-9a-f]{64}$'
                   OR NEW.ontology_snapshot_hash IS DISTINCT FROM
                      graph_mapping_json_sha256(NEW.ontology_snapshot)
                   OR NEW.ontology_version_id IS NULL THEN
                    RAISE EXCEPTION 'frozen ontology snapshot transition is incomplete or not content-bound';
                END IF;
                old_placeholder :=
                    jsonb_typeof(OLD.ontology_snapshot) = 'object'
                    AND OLD.ontology_snapshot->>'schema_state' = 'ai_discovery_pending'
                    AND (OLD.ontology_snapshot->>'ontology_version_id') = OLD.ontology_version_id::text
                    AND jsonb_typeof(OLD.ontology_snapshot->'confirmed') = 'boolean'
                    AND OLD.ontology_snapshot->>'confirmed' = 'false'
                    AND (OLD.ontology_snapshot->'entity_types') = '[]'::jsonb
                    AND (OLD.ontology_snapshot->'relation_types') = '[]'::jsonb
                    AND (OLD.ontology_snapshot->'relation_constraints') = '[]'::jsonb
                    AND OLD.ontology_snapshot_hash IS NOT DISTINCT FROM
                        graph_mapping_json_sha256(OLD.ontology_snapshot);
                old_draft :=
                    jsonb_typeof(OLD.ontology_snapshot) = 'object'
                    AND OLD.ontology_snapshot->>'schema_state' = 'ai_draft'
                    AND jsonb_typeof(OLD.ontology_snapshot->'confirmed') = 'boolean'
                    AND OLD.ontology_snapshot->>'confirmed' = 'false'
                    AND (OLD.ontology_snapshot->>'ontology_version_id') = OLD.ontology_version_id::text
                    AND jsonb_typeof(OLD.ontology_snapshot->'entity_types') = 'array'
                    AND jsonb_typeof(OLD.ontology_snapshot->'relation_types') = 'array'
                    AND jsonb_typeof(OLD.ontology_snapshot->'relation_constraints') = 'array'
                    AND OLD.ontology_snapshot_hash IS NOT DISTINCT FROM
                        graph_mapping_json_sha256(OLD.ontology_snapshot);
                new_draft :=
                    NEW.ontology_snapshot->>'schema_state' = 'ai_draft'
                    AND jsonb_typeof(NEW.ontology_snapshot->'confirmed') = 'boolean'
                    AND NEW.ontology_snapshot->>'confirmed' = 'false'
                    AND (NEW.ontology_snapshot->>'ontology_version_id') = NEW.ontology_version_id::text
                    AND jsonb_typeof(NEW.ontology_snapshot->'entity_types') = 'array'
                    AND jsonb_typeof(NEW.ontology_snapshot->'relation_types') = 'array'
                    AND jsonb_typeof(NEW.ontology_snapshot->'relation_constraints') = 'array';
                new_frozen :=
                    NEW.ontology_snapshot->>'schema_state' = 'confirmed'
                    AND jsonb_typeof(NEW.ontology_snapshot->'confirmed') = 'boolean'
                    AND NEW.ontology_snapshot->>'confirmed' = 'true'
                    AND (NEW.ontology_snapshot->>'ontology_version_id') = NEW.ontology_version_id::text
                    AND jsonb_typeof(NEW.ontology_snapshot->'entity_types') = 'array'
                    AND jsonb_typeof(NEW.ontology_snapshot->'relation_types') = 'array'
                    AND jsonb_typeof(NEW.ontology_snapshot->'relation_constraints') = 'array'
                    AND NEW.ontology_snapshot_hash IS NOT DISTINCT FROM
                        graph_mapping_json_sha256(NEW.ontology_snapshot);
                IF NOT (
                    (
                        old_placeholder
                        AND NEW.ontology_version_id IS NOT DISTINCT FROM OLD.ontology_version_id
                        AND (new_draft OR new_frozen)
                    )
                    OR (
                        old_draft
                        AND NEW.ontology_version_id IS NOT DISTINCT FROM OLD.ontology_version_id
                        AND new_frozen
                    )
                ) THEN
                    RAISE EXCEPTION 'frozen ontology snapshot is immutable after its initial freeze';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER graph_extraction_jobs_scope_guard
            BEFORE INSERT OR UPDATE OF library_id, ontology_version_id, ontology_snapshot, ontology_snapshot_hash
            ON graph_extraction_jobs
            FOR EACH ROW EXECUTE FUNCTION graph_extraction_jobs_scope_guard()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER graph_extraction_jobs_ontology_snapshot_immutable_guard
            BEFORE UPDATE OF ontology_version_id, ontology_snapshot, ontology_snapshot_hash
            ON graph_extraction_jobs
            FOR EACH ROW EXECUTE FUNCTION graph_extraction_jobs_ontology_snapshot_immutable_guard()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION graph_mapping_authority_insert_guard()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                persisted_snapshot jsonb;
                persisted_hash text;
                persisted_registry jsonb;
                persisted_frozen jsonb;
                expected_authority_fingerprint text;
                expected_registry_hash text;
                expected_frozen_hash text;
                registry_entry jsonb;
            BEGIN
                IF NEW.source_table <> 'graph_extraction_jobs'
                   OR NEW.source_id IS DISTINCT FROM NEW.job_id
                   OR NEW.source_kind <> 'graph_extraction_job'
                   OR NEW.source_version <> 'ontology_snapshot_v1'
                   OR NEW.source_key <> NEW.job_id::text THEN
                    RAISE EXCEPTION 'mapping authority source is not repository-owned';
                END IF;
                SELECT ontology_snapshot, ontology_snapshot_hash
                  INTO persisted_snapshot, persisted_hash
                  FROM graph_extraction_jobs
                 WHERE id = NEW.job_id;
                persisted_registry := persisted_snapshot->'authorization_registry_snapshot';
                persisted_frozen := persisted_snapshot->'frozen_ontology';
                expected_registry_hash := graph_mapping_json_sha256(
                    persisted_registry - 'registry_snapshot_hash'::text
                );
                expected_frozen_hash := graph_mapping_json_sha256(
                    persisted_frozen - 'ontology_snapshot_hash'::text
                );
                IF persisted_registry IS NOT NULL
                   AND jsonb_typeof(persisted_registry->'entries') = 'array' THEN
                    FOR registry_entry IN
                        SELECT value FROM jsonb_array_elements(persisted_registry->'entries')
                    LOOP
                        IF (registry_entry->>'entry_fingerprint') IS DISTINCT FROM
                           graph_mapping_json_sha256(registry_entry - 'entry_fingerprint'::text) THEN
                            RAISE EXCEPTION 'mapping authority registry entry fingerprint is not content-bound';
                        END IF;
                    END LOOP;
                    IF EXISTS (
                        SELECT 1
                          FROM jsonb_array_elements(persisted_registry->'entries') WITH ORDINALITY AS left_entry(value, ordinal)
                          JOIN jsonb_array_elements(persisted_registry->'entries') WITH ORDINALITY AS right_entry(value, ordinal)
                            ON left_entry.ordinal < right_entry.ordinal
                           AND (left_entry.value->>'authorization_key') = (right_entry.value->>'authorization_key')
                           AND (left_entry.value->>'authorization_version') = (right_entry.value->>'authorization_version')
                           AND (left_entry.value->>'surface_predicate_sha256') = (right_entry.value->>'surface_predicate_sha256')
                           AND (left_entry.value->>'canonical_relation_key_sha256') = (right_entry.value->>'canonical_relation_key_sha256')
                    ) THEN
                        RAISE EXCEPTION 'mapping authority registry contains duplicate policy identity';
                    END IF;
                END IF;
                expected_authority_fingerprint := graph_mapping_authority_fingerprint(
                    NEW.authority_schema_version,
                    persisted_registry->'scope',
                    persisted_frozen->>'ontology_snapshot_hash',
                    persisted_registry->>'registry_snapshot_hash',
                    persisted_registry,
                    NEW.source_table,
                    NEW.source_id,
                    NEW.source_kind,
                    NEW.source_key,
                    NEW.source_version,
                    NEW.source_hash
                );
                IF persisted_snapshot IS NULL
                   OR persisted_registry IS NULL
                   OR persisted_frozen IS NULL
                   OR jsonb_typeof(persisted_registry->'scope') <> 'object'
                   OR jsonb_typeof(persisted_registry->'entries') <> 'array'
                   OR jsonb_typeof(persisted_frozen) <> 'object'
                   OR (persisted_registry->>'registry_snapshot_hash') IS DISTINCT FROM expected_registry_hash
                   OR (persisted_frozen->>'ontology_snapshot_hash') IS DISTINCT FROM expected_frozen_hash
                   OR NEW.source_hash IS DISTINCT FROM persisted_hash
                   OR NEW.registry_snapshot IS DISTINCT FROM persisted_registry
                   OR NEW.registry_snapshot_hash IS DISTINCT FROM
                      expected_registry_hash
                   OR NEW.ontology_snapshot_hash IS DISTINCT FROM
                      expected_frozen_hash
                   OR NEW.ontology_contract_version IS DISTINCT FROM
                      (persisted_frozen->>'ontology_contract_version')
                   OR NEW.ontology_version_id::text IS DISTINCT FROM
                      (persisted_frozen->>'ontology_version_id')
                   OR (persisted_registry->'scope') IS DISTINCT FROM jsonb_build_object(
                        'library_id', NEW.library_id::text,
                        'document_id', NEW.document_id::text,
                        'document_revision_id', NEW.document_revision_id::text,
                        'revision_no', NEW.revision_no,
                        'job_id', NEW.job_id::text,
                        'extraction_unit_id', NEW.extraction_unit_id::text,
                        'claim_id', NEW.claim_id::text,
                        'extraction_occurrence_id', NEW.extraction_occurrence_id::text
                      )
                   OR NEW.authority_fingerprint IS DISTINCT FROM expected_authority_fingerprint
                   OR NEW.authority_id IS DISTINCT FROM graph_mapping_uuid5(
                        '2cb2c2a1-5d36-5b6b-9c3e-21a96e8b7f40'::uuid,
                        'mapping_authority_v1:' || NEW.source_id::text || ':'
                            || NEW.source_hash || ':' || NEW.registry_snapshot_hash
                      ) THEN
                    RAISE EXCEPTION 'mapping authority is not bound to the persisted ontology snapshot';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER graph_mapping_authority_insert_guard
            BEFORE INSERT ON graph_mapping_authority_snapshots
            FOR EACH ROW EXECUTE FUNCTION graph_mapping_authority_insert_guard()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION graph_mapping_authority_immutable_guard()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'mapping authority snapshot rows are immutable';
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER graph_mapping_authority_immutable_guard
            BEFORE UPDATE OR DELETE ON graph_mapping_authority_snapshots
            FOR EACH ROW EXECUTE FUNCTION graph_mapping_authority_immutable_guard()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION graph_claim_mappings_insert_guard()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                projection jsonb := NEW.result_projection;
                remap jsonb := projection->'remap_provenance';
                authority_registry jsonb;
                authority_row record;
                job_row record;
                unit_row record;
                claim_row record;
                occurrence_row record;
                decision_row record;
                evidence_item jsonb;
                binding_item jsonb;
                expected_raw_claim_hash text;
                expected_attempt_payload jsonb;
                expected_attempt_fingerprint text;
                expected_result_payload jsonb;
                expected_result_fingerprint text;
                expected_decision_fingerprint text;
                expected_decision_payload_hash text;
                expected_raw_claim_payload jsonb;
                expected_claim_snapshot jsonb;
                expected_semantic_core jsonb;
                expected_semantic_projection jsonb;
                expected_frozen_hash text;
                expected_registry_hash text;
                expected_evidence_identity_hashes jsonb;
                expected_evidence_bindings jsonb;
                expected_attestation jsonb;
                expected_binding jsonb;
                expected_binding_identity_payload jsonb;
                registry_entry jsonb;
                expected_root_id uuid;
                expected_root_fingerprint text;
                expected_remap jsonb;
                predecessor_row record;
            BEGIN
                IF NOT graph_mapping_json_exact_keys(projection, ARRAY[
                    'schema_version', 'mapping_result_id', 'mapping_attempt_id',
                    'mapping_attempt_fingerprint', 'mapping_result_fingerprint',
                    'mapping_version', 'mapping_schema_hash', 'canonical_schema_hash',
                    'raw_claim_authority_sha256', 'library_id', 'document_id',
                    'document_revision_id', 'revision_no', 'job_id',
                    'extraction_unit_id', 'claim_id',
                    'claim_content_scoped_fingerprint', 'extraction_occurrence_id',
                    'extraction_occurrence_fingerprint', 'surface_raw_predicate',
                    'surface_direction', 'ontology_version_id',
                    'ontology_snapshot_hash', 'ontology_contract_version',
                    'claim_snapshot', 'endpoint_type_binding',
                    'source_endpoint_resolution', 'target_endpoint_resolution',
                    'semantic_projection', 'frozen_ontology',
                    'authorization_registry_snapshot', 'authorization_provenance',
                    'provenance', 'canonical_source_endpoint',
                    'canonical_target_endpoint',
                    'source_provenance', 'actor_provenance',
                    'outcome', 'semantic_status', 'evidence_bindings',
                    'source_evidence_ref_ids', 'target_evidence_ref_ids',
                    'mapping_evidence_ref_ids', 'verified_evidence_identity_hashes',
                    'mapping_confidence',
                    'reason_code', 'canonical_relation_key',
                    'canonical_direction', 'endpoint_transform',
                    'predicate_transform', 'remap_provenance',
                    'decision_binding', 'decision_id',
                    'decision_fingerprint', 'decision_kind', 'created_at'
                ]::text[]) THEN
                    RAISE EXCEPTION 'canonical mapping projection is missing required keys';
                END IF;
                IF (projection->>'mapping_schema_hash') IS DISTINCT FROM
                   '56b1fa1029a2e882f5dc1ffbe7b9ca97f66255356f0bf71575b0307cebe5385c'
                   OR (projection->>'canonical_schema_hash') IS DISTINCT FROM
                   '4b42d28b3d7ef7398d0d7669797357501f51ba83ffe792d8033966c4addbdd41' THEN
                    RAISE EXCEPTION 'canonical mapping schema manifest hash is not the frozen M3 contract';
                END IF;
                IF NOT graph_mapping_json_exact_keys(
                    projection->'endpoint_type_binding',
                    ARRAY['source_type_key', 'target_type_key']::text[]
                )
                   OR NOT graph_mapping_json_exact_keys(
                       projection->'claim_snapshot',
                       ARRAY[
                           'snapshot_version', 'claim_id', 'library_id', 'document_id',
                           'document_revision_id', 'revision_no', 'job_id',
                           'extraction_unit_id', 'extraction_occurrence_id',
                           'claim_content_scoped_fingerprint', 'extraction_occurrence_fingerprint',
                           'raw_predicate_sha256', 'surface_direction',
                           'source_mention_local_id', 'target_mention_local_id',
                           'source_mention_surface_sha256', 'target_mention_surface_sha256',
                           'source_mention_evidence_ref', 'target_mention_evidence_ref',
                           'evidence_ref_ids', 'evidence_identity_hashes', 'evidence_snapshots',
                           'semantic_projection_fingerprint', 'claim_json_sha256',
                           'snapshot_fingerprint'
                       ]::text[]
                   )
                   OR NOT graph_mapping_json_exact_keys(
                       projection->'semantic_projection',
                       ARRAY[
                           'semantic_projection_version', 'semantic_projection_fingerprint',
                           'negation_value', 'negation_evidence_identity_hash',
                           'modality_present', 'modality_value_hash',
                           'modality_evidence_identity_hash', 'qualifier_present',
                           'qualifier_set_hash', 'qualifier_evidence_identity_hashes',
                           'valid_time_present', 'valid_time_hash',
                           'valid_time_evidence_identity_hash', 'effective_time_present',
                           'effective_time_hash', 'effective_time_evidence_identity_hash',
                           'evidence_ref_ids', 'evidence_identity_hashes'
                       ]::text[]
                   )
                   OR NOT graph_mapping_json_exact_keys(
                       projection->'frozen_ontology',
                       ARRAY[
                           'ontology_version_id', 'ontology_snapshot_hash',
                           'ontology_contract_version', 'entity_type_keys',
                           'relation_type_keys', 'constraints'
                       ]::text[]
                   )
                   OR NOT graph_mapping_json_exact_keys(
                       projection->'authorization_registry_snapshot',
                       ARRAY[
                           'registry_snapshot_version', 'scope', 'ontology_snapshot_hash',
                           'entries', 'registry_snapshot_hash'
                       ]::text[]
                   )
                   OR NOT graph_mapping_json_exact_keys(
                       projection->'authorization_registry_snapshot'->'scope',
                       ARRAY[
                           'library_id', 'document_id', 'document_revision_id',
                           'revision_no', 'job_id', 'extraction_unit_id',
                           'claim_id', 'extraction_occurrence_id'
                       ]::text[]
                   )
                   OR NOT graph_mapping_json_exact_keys(
                       projection->'provenance',
                       ARRAY[
                           'mapper_algorithm_key', 'mapper_algorithm_version',
                           'mapper_key', 'mapper_version', 'mapper_version_hash',
                           'model_provider', 'model_version_hash', 'prompt_version',
                           'prompt_content_hash', 'config_version', 'config_hash',
                           'provenance_fingerprint'
                       ]::text[]
                   )
                   OR NOT graph_mapping_json_exact_keys(
                       projection->'source_provenance',
                       ARRAY[
                           'source_kind', 'source_key', 'source_version', 'source_hash',
                           'source_precedence', 'provenance_fingerprint'
                       ]::text[]
                   )
                   OR NOT graph_mapping_json_exact_keys(
                       projection->'actor_provenance',
                       ARRAY[
                           'actor_kind', 'actor_key', 'actor_precedence',
                           'provenance_fingerprint'
                       ]::text[]
                   )
                   OR (
                       projection->'authorization_provenance' IS NOT NULL
                       AND jsonb_typeof(projection->'authorization_provenance') <> 'null'
                       AND NOT graph_mapping_json_exact_keys(
                           projection->'authorization_provenance',
                           ARRAY[
                               'authorization_key', 'authorization_version',
                               'registry_snapshot', 'registry_hash',
                               'authorized_surface_predicate_sha256',
                               'authorized_canonical_relation_key_sha256',
                               'authorization_fingerprint'
                           ]::text[]
                       )
                   )
                   OR (
                       projection->'decision_binding' IS NOT NULL
                       AND jsonb_typeof(projection->'decision_binding') <> 'null'
                       AND NOT graph_mapping_json_exact_keys(
                           projection->'decision_binding',
                           ARRAY[
                               'decision_schema_version', 'decision_id',
                               'decision_fingerprint', 'decision_version',
                               'decision_kind', 'status', 'reason_code',
                               'library_id', 'document_id', 'document_revision_id',
                               'revision_no', 'job_id', 'extraction_unit_id',
                               'claim_id', 'extraction_occurrence_id',
                               'decision_payload_sha256', 'binding_fingerprint'
                           ]::text[]
                       )
                   )
                   OR (
                       projection->'remap_provenance' IS NOT NULL
                       AND jsonb_typeof(projection->'remap_provenance') <> 'null'
                       AND NOT graph_mapping_json_exact_keys(
                           projection->'remap_provenance',
                           ARRAY[
                               'remap_generation', 'remap_version',
                               'supersedes_mapping_result_id',
                               'supersedes_mapping_result_fingerprint',
                               'supersedes_scope', 'prior_remap_generation',
                               'lineage_root_mapping_result_id',
                               'lineage_root_mapping_result_fingerprint',
                               'supersedes_lineage_root_mapping_result_id',
                               'supersedes_lineage_root_mapping_result_fingerprint',
                               'supersedes_source_precedence',
                               'supersedes_actor_precedence', 'reason_code',
                               'remap_fingerprint'
                           ]::text[]
                       )
                   )
                   OR (
                       projection->'source_endpoint_resolution' IS NOT NULL
                       AND jsonb_typeof(projection->'source_endpoint_resolution') <> 'null'
                       AND NOT graph_mapping_json_exact_keys(
                           projection->'source_endpoint_resolution',
                           ARRAY[
                               'attestation_version', 'mention_role',
                               'mention_local_id', 'mention_surface_sha256',
                               'entity_type_key', 'entity_link', 'evidence_ref_ids',
                               'scope', 'attestation_fingerprint'
                           ]::text[]
                       )
                   )
                   OR (
                       projection->'target_endpoint_resolution' IS NOT NULL
                       AND jsonb_typeof(projection->'target_endpoint_resolution') <> 'null'
                       AND NOT graph_mapping_json_exact_keys(
                           projection->'target_endpoint_resolution',
                           ARRAY[
                               'attestation_version', 'mention_role',
                               'mention_local_id', 'mention_surface_sha256',
                               'entity_type_key', 'entity_link', 'evidence_ref_ids',
                               'scope', 'attestation_fingerprint'
                           ]::text[]
                       )
                   )
                   OR (
                       projection->'canonical_source_endpoint' IS NOT NULL
                       AND jsonb_typeof(projection->'canonical_source_endpoint') <> 'null'
                       AND NOT graph_mapping_json_exact_keys(
                           projection->'canonical_source_endpoint',
                           ARRAY[
                               'role', 'mention_local_id', 'entity_type_key',
                               'entity_link', 'resolution_attestation',
                               'endpoint_fingerprint'
                           ]::text[]
                       )
                   )
                   OR (
                       projection->'canonical_target_endpoint' IS NOT NULL
                       AND jsonb_typeof(projection->'canonical_target_endpoint') <> 'null'
                       AND NOT graph_mapping_json_exact_keys(
                           projection->'canonical_target_endpoint',
                           ARRAY[
                               'role', 'mention_local_id', 'entity_type_key',
                               'entity_link', 'resolution_attestation',
                               'endpoint_fingerprint'
                           ]::text[]
                       )
                   ) THEN
                    RAISE EXCEPTION 'canonical mapping typed projection contains an unknown or missing key';
                END IF;
                IF EXISTS (
                    SELECT 1
                      FROM jsonb_array_elements(projection->'frozen_ontology'->'constraints') AS constraint_item(value)
                     WHERE NOT graph_mapping_json_exact_keys(
                         constraint_item.value,
                         ARRAY['relation_key', 'source_type_key', 'target_type_key', 'direction']::text[]
                     )
                ) OR EXISTS (
                    SELECT 1
                      FROM jsonb_array_elements(projection->'authorization_registry_snapshot'->'entries') AS registry_item(value)
                     WHERE NOT graph_mapping_json_exact_keys(
                         registry_item.value,
                         ARRAY[
                             'registry_entry_version', 'authorization_key',
                             'authorization_version', 'surface_predicate_sha256',
                             'canonical_relation_key_sha256',
                             'allowed_endpoint_transforms', 'allowed_predicate_transforms',
                             'allowed_canonical_directions', 'allowed_source_type_keys',
                             'allowed_target_type_keys', 'entry_fingerprint'
                         ]::text[]
                     )
                ) THEN
                    RAISE EXCEPTION 'canonical mapping ontology or registry object contains an unknown or missing key';
                END IF;
                IF NOT graph_mapping_validate_timestamp_text(projection->'created_at') THEN
                    RAISE EXCEPTION 'canonical mapping created_at is not a finite timezone-aware RFC3339 timestamp';
                END IF;
                SELECT *
                  INTO authority_row
                  FROM graph_mapping_authority_snapshots
                 WHERE authority_id = NEW.authority_id;
                authority_registry := authority_row.registry_snapshot;
                IF authority_row IS NULL
                   OR authority_registry IS NULL
                   OR authority_registry IS DISTINCT FROM (projection->'authorization_registry_snapshot') THEN
                    RAISE EXCEPTION 'canonical mapping authorization is not bound to the authority row';
                END IF;
                IF NEW.authority_fingerprint IS DISTINCT FROM authority_row.authority_fingerprint
                   OR NEW.library_id IS DISTINCT FROM authority_row.library_id
                   OR NEW.document_id IS DISTINCT FROM authority_row.document_id
                   OR NEW.document_revision_id IS DISTINCT FROM authority_row.document_revision_id
                   OR NEW.revision_no IS DISTINCT FROM authority_row.revision_no
                   OR NEW.job_id IS DISTINCT FROM authority_row.job_id
                   OR NEW.extraction_unit_id IS DISTINCT FROM authority_row.extraction_unit_id
                   OR NEW.claim_id IS DISTINCT FROM authority_row.claim_id
                   OR NEW.extraction_occurrence_id IS DISTINCT FROM authority_row.extraction_occurrence_id
                   OR NEW.ontology_version_id IS DISTINCT FROM authority_row.ontology_version_id
                   OR NEW.ontology_snapshot_hash IS DISTINCT FROM authority_row.ontology_snapshot_hash
                   OR NEW.ontology_contract_version IS DISTINCT FROM authority_row.ontology_contract_version THEN
                    RAISE EXCEPTION 'canonical mapping authority or scope projection is not bound';
                END IF;
                IF authority_row.authority_fingerprint IS DISTINCT FROM graph_mapping_authority_fingerprint(
                    authority_row.authority_schema_version,
                    authority_row.registry_snapshot->'scope',
                    authority_row.ontology_snapshot_hash,
                    authority_row.registry_snapshot_hash,
                    authority_row.registry_snapshot,
                    authority_row.source_table,
                    authority_row.source_id,
                    authority_row.source_kind,
                    authority_row.source_key,
                    authority_row.source_version,
                    authority_row.source_hash
                ) THEN
                    RAISE EXCEPTION 'canonical mapping authority fingerprint is not recomputed from its source row';
                END IF;
                IF projection->>'surface_direction' NOT IN
                   ('source_to_target','target_to_source','undirected','unknown')
                   OR (projection->>'canonical_direction' IS NOT NULL AND
                       projection->>'canonical_direction' NOT IN
                       ('source_to_target','target_to_source','undirected'))
                   OR (projection->>'endpoint_transform' IS NOT NULL AND
                       projection->>'endpoint_transform' NOT IN ('identity','swap'))
                   OR (projection->>'predicate_transform' IS NOT NULL AND
                       projection->>'predicate_transform' NOT IN ('identity','inverse','symmetric')) THEN
                    RAISE EXCEPTION 'canonical mapping transform or direction is invalid';
                END IF;
                SELECT *
                  INTO job_row
                  FROM graph_extraction_jobs
                 WHERE id = NEW.job_id;
                SELECT *
                  INTO unit_row
                  FROM graph_extraction_units
                 WHERE id = NEW.extraction_unit_id;
                SELECT *
                  INTO claim_row
                  FROM graph_raw_claims
                 WHERE id = NEW.claim_id;
                SELECT *
                  INTO occurrence_row
                  FROM graph_raw_claim_occurrences
                 WHERE extraction_occurrence_id = NEW.extraction_occurrence_id;
                IF job_row IS NULL
                   OR unit_row IS NULL
                   OR claim_row IS NULL
                   OR occurrence_row IS NULL
                   OR job_row.library_id IS DISTINCT FROM NEW.library_id
                   OR job_row.document_id IS DISTINCT FROM NEW.document_id
                   OR job_row.document_revision_id IS DISTINCT FROM NEW.document_revision_id
                   OR job_row.ontology_version_id IS DISTINCT FROM NEW.ontology_version_id
                   OR job_row.ontology_snapshot_hash IS DISTINCT FROM NEW.ontology_snapshot_hash
                   OR (job_row.ontology_snapshot->'frozen_ontology') IS DISTINCT FROM (projection->'frozen_ontology')
                   OR (job_row.ontology_snapshot->'authorization_registry_snapshot') IS DISTINCT FROM
                      (projection->'authorization_registry_snapshot')
                   OR unit_row.job_id IS DISTINCT FROM NEW.job_id
                   OR unit_row.library_id IS DISTINCT FROM NEW.library_id
                   OR unit_row.document_revision_id IS DISTINCT FROM NEW.document_revision_id
                   OR claim_row.library_id IS DISTINCT FROM NEW.library_id
                   OR claim_row.document_id IS DISTINCT FROM NEW.document_id
                   OR claim_row.document_revision_id IS DISTINCT FROM NEW.document_revision_id
                   OR claim_row.revision_no IS DISTINCT FROM NEW.revision_no
                   OR occurrence_row.claim_id IS DISTINCT FROM NEW.claim_id
                   OR occurrence_row.job_id IS DISTINCT FROM NEW.job_id
                   OR occurrence_row.extraction_unit_id IS DISTINCT FROM NEW.extraction_unit_id
                   OR occurrence_row.extraction_occurrence_fingerprint IS DISTINCT FROM NEW.extraction_occurrence_fingerprint
                   OR occurrence_row.ontology_snapshot_hash IS DISTINCT FROM NEW.ontology_snapshot_hash THEN
                    RAISE EXCEPTION 'canonical mapping source scope is not bound to immutable rows';
                END IF;
                IF jsonb_typeof(occurrence_row.evidence_refs) <> 'array'
                   OR jsonb_array_length(occurrence_row.evidence_refs) < 1
                   OR EXISTS (
                       SELECT 1
                         FROM jsonb_array_elements(occurrence_row.evidence_refs) AS reference(value)
                        WHERE NOT graph_mapping_validate_evidence_reference(reference.value)
                   ) THEN
                    RAISE EXCEPTION 'canonical mapping evidence references are not strict and locator-bound';
                END IF;
                IF jsonb_typeof(claim_row.source_mention) <> 'object'
                   OR jsonb_typeof(claim_row.target_mention) <> 'object'
                   OR jsonb_typeof(claim_row.negation) <> 'object'
                   OR jsonb_typeof(claim_row.modality) <> 'object'
                   OR jsonb_typeof(claim_row.qualifiers) <> 'array'
                   OR (claim_row.valid_time IS NOT NULL
                       AND jsonb_typeof(claim_row.valid_time) NOT IN ('object', 'null'))
                   OR (claim_row.effective_time IS NOT NULL
                       AND jsonb_typeof(claim_row.effective_time) NOT IN ('object', 'null'))
                   OR jsonb_typeof(claim_row.negation->'value') <> 'boolean'
                   OR claim_row.surface_direction NOT IN
                      ('source_to_target', 'target_to_source', 'undirected', 'unknown') THEN
                    RAISE EXCEPTION 'canonical mapping raw claim projection has invalid JSON types';
                END IF;
                IF (
                    (claim_row.source_mention->>'evidence_ref') IS NOT NULL
                    AND graph_mapping_restore_evidence_ref(
                        claim_row.source_mention, occurrence_row.evidence_refs
                    ) IS NULL
                ) OR (
                    (claim_row.target_mention->>'evidence_ref') IS NOT NULL
                    AND graph_mapping_restore_evidence_ref(
                        claim_row.target_mention, occurrence_row.evidence_refs
                    ) IS NULL
                ) OR (
                    (claim_row.negation->>'evidence_ref') IS NOT NULL
                    AND graph_mapping_restore_evidence_ref(
                        claim_row.negation, occurrence_row.evidence_refs
                    ) IS NULL
                ) OR (
                    (claim_row.modality->>'evidence_ref') IS NOT NULL
                    AND graph_mapping_restore_evidence_ref(
                        claim_row.modality, occurrence_row.evidence_refs
                    ) IS NULL
                ) OR (
                    (claim_row.valid_time->>'evidence_ref') IS NOT NULL
                    AND graph_mapping_restore_evidence_ref(
                        claim_row.valid_time, occurrence_row.evidence_refs
                    ) IS NULL
                ) OR (
                    (claim_row.effective_time->>'evidence_ref') IS NOT NULL
                    AND graph_mapping_restore_evidence_ref(
                        claim_row.effective_time, occurrence_row.evidence_refs
                    ) IS NULL
                ) OR EXISTS (
                    SELECT 1
                      FROM jsonb_array_elements(claim_row.qualifiers) AS qualifier(value)
                     WHERE (qualifier.value->>'evidence_ref') IS NOT NULL
                       AND graph_mapping_restore_evidence_ref(
                           qualifier.value, occurrence_row.evidence_refs
                       ) IS NULL
                ) THEN
                    RAISE EXCEPTION 'canonical mapping raw claim evidence identity is unknown or ambiguous';
                END IF;
                expected_raw_claim_payload := jsonb_build_object(
                         'claim_schema_version', claim_row.claim_schema_version,
                         'claim_id', claim_row.id::text,
                        'library_id', claim_row.library_id::text,
                        'document_id', claim_row.document_id::text,
                        'document_revision_id', claim_row.document_revision_id::text,
                        'revision_no', claim_row.revision_no,
                        'job_id', occurrence_row.job_id::text,
                        'extraction_unit_id', occurrence_row.extraction_unit_id::text,
                         'source_mention', graph_mapping_restore_evidence_ref(
                             claim_row.source_mention, occurrence_row.evidence_refs
                         ),
                         'raw_predicate', claim_row.raw_predicate,
                         'target_mention', graph_mapping_restore_evidence_ref(
                             claim_row.target_mention, occurrence_row.evidence_refs
                         ),
                         'surface_direction', claim_row.surface_direction,
                         'negation', graph_mapping_restore_evidence_ref(
                             claim_row.negation, occurrence_row.evidence_refs
                         ),
                         'modality', graph_mapping_restore_evidence_ref(
                             claim_row.modality, occurrence_row.evidence_refs
                         ),
                         'qualifiers', graph_mapping_restore_qualifiers(
                             claim_row.qualifiers, occurrence_row.evidence_refs
                         ),
                         'valid_time', graph_mapping_restore_evidence_ref(
                             claim_row.valid_time, occurrence_row.evidence_refs
                         ),
                         'effective_time', graph_mapping_restore_evidence_ref(
                             claim_row.effective_time, occurrence_row.evidence_refs
                         ),
                        'evidence_refs', occurrence_row.evidence_refs,
                        'extractor_version', occurrence_row.extractor_version,
                        'prompt_version', occurrence_row.prompt_version,
                        'model_provider', occurrence_row.model_provider,
                        'model_name', occurrence_row.model_name,
                        'model_config_hash', occurrence_row.model_config_hash,
                        'prompt_content_hash', occurrence_row.prompt_content_hash,
                        'parser_version', occurrence_row.parser_version,
                        'normalization_rule_version', occurrence_row.normalization_rule_version,
                         'ontology_snapshot_hash', occurrence_row.ontology_snapshot_hash,
                         'content_scoped_claim_fingerprint', claim_row.content_scoped_claim_fingerprint,
                         'extraction_occurrence_id', occurrence_row.extraction_occurrence_id::text,
                         'extraction_occurrence_fingerprint', occurrence_row.extraction_occurrence_fingerprint
                );
                expected_raw_claim_hash := graph_mapping_json_sha256(expected_raw_claim_payload);
                expected_semantic_projection := graph_mapping_semantic_projection(
                    expected_raw_claim_payload,
                    occurrence_row.evidence_refs
                );
                expected_claim_snapshot := graph_mapping_claim_snapshot(
                    expected_raw_claim_payload,
                    expected_semantic_projection
                );
                IF NOT graph_mapping_semantic_evidence_bound(
                    expected_raw_claim_payload,
                    occurrence_row.evidence_refs
                ) THEN
                    RAISE EXCEPTION 'canonical mapping semantic evidence is not bound to the occurrence';
                END IF;
                IF NEW.raw_claim_authority_sha256 IS DISTINCT FROM expected_raw_claim_hash
                   OR (projection->>'raw_claim_authority_sha256') IS DISTINCT FROM expected_raw_claim_hash
                   OR (projection->'claim_snapshot') IS DISTINCT FROM expected_claim_snapshot
                   OR (projection->'semantic_projection') IS DISTINCT FROM expected_semantic_projection
                   OR (projection->'claim_snapshot'->>'claim_content_scoped_fingerprint') IS DISTINCT FROM claim_row.content_scoped_claim_fingerprint
                   OR (projection->'claim_snapshot'->>'extraction_occurrence_fingerprint') IS DISTINCT FROM occurrence_row.extraction_occurrence_fingerprint
                   OR (projection->'claim_snapshot'->>'raw_predicate_sha256') IS DISTINCT FROM graph_mapping_text_sha256(claim_row.raw_predicate)
                   OR (projection->>'surface_raw_predicate') IS DISTINCT FROM claim_row.raw_predicate
                   OR (projection->>'surface_direction') IS DISTINCT FROM claim_row.surface_direction
                   OR (projection->'claim_snapshot'->>'surface_direction') IS DISTINCT FROM claim_row.surface_direction
                   OR (projection->'claim_snapshot'->>'claim_id') IS DISTINCT FROM claim_row.id::text
                   OR (projection->'claim_snapshot'->>'job_id') IS DISTINCT FROM occurrence_row.job_id::text
                   OR (projection->'claim_snapshot'->>'extraction_unit_id') IS DISTINCT FROM occurrence_row.extraction_unit_id::text
                   OR (projection->'claim_snapshot'->>'extraction_occurrence_id') IS DISTINCT FROM occurrence_row.extraction_occurrence_id::text THEN
                    RAISE EXCEPTION 'canonical mapping raw claim identity is not recomputed from source rows';
                END IF;
                IF (
                    SELECT COUNT(*)
                      FROM jsonb_array_elements(occurrence_row.evidence_refs) AS reference(value)
                ) <> (
                    SELECT COUNT(DISTINCT value->>'ref_id')
                      FROM jsonb_array_elements(occurrence_row.evidence_refs) AS reference(value)
                )
                   OR (
                       SELECT COUNT(*)
                         FROM jsonb_array_elements(occurrence_row.evidence_refs) AS reference(value)
                   ) <> (
                       SELECT COUNT(DISTINCT graph_mapping_json_sha256(
                           value - ARRAY['ref_id', 'job_id', 'extraction_unit_id']::text[]
                       ))
                         FROM jsonb_array_elements(occurrence_row.evidence_refs) AS reference(value)
                   ) THEN
                    RAISE EXCEPTION 'canonical mapping occurrence evidence identities are duplicated';
                END IF;
                IF jsonb_array_length(projection->'evidence_bindings') < 1
                   OR jsonb_array_length(projection->'evidence_bindings') >
                      jsonb_array_length(occurrence_row.evidence_refs)
                   OR jsonb_array_length(projection->'evidence_bindings') <> (
                       SELECT COUNT(DISTINCT value->>'evidence_ref_id')
                         FROM jsonb_array_elements(projection->'evidence_bindings') AS binding(value)
                   ) THEN
                    RAISE EXCEPTION 'canonical mapping evidence subset is invalid';
                END IF;
                expected_evidence_identity_hashes := (
                    SELECT COALESCE(
                        jsonb_agg(to_jsonb(value->>'stable_evidence_identity_hash') ORDER BY value->>'stable_evidence_identity_hash'),
                        '[]'::jsonb
                    )
                      FROM jsonb_array_elements(projection->'evidence_bindings') AS selected(value)
                );
                IF (projection->'verified_evidence_identity_hashes') IS DISTINCT FROM
                   expected_evidence_identity_hashes
                   OR (projection->'mapping_evidence_ref_ids') IS DISTINCT FROM (
                       SELECT COALESCE(
                           jsonb_agg(to_jsonb(value->>'evidence_ref_id') ORDER BY value->>'evidence_ref_id'),
                           '[]'::jsonb
                         )
                         FROM jsonb_array_elements(projection->'evidence_bindings') AS selected(value)
                   ) THEN
                    RAISE EXCEPTION 'canonical mapping evidence subset identity is not bound';
                END IF;
                IF jsonb_typeof(projection->'source_evidence_ref_ids') <> 'array'
                   OR jsonb_typeof(projection->'target_evidence_ref_ids') <> 'array'
                   OR jsonb_typeof(projection->'mapping_evidence_ref_ids') <> 'array'
                   OR jsonb_array_length(expected_evidence_identity_hashes) < 1
                   OR jsonb_array_length(expected_evidence_identity_hashes) <> (
                       SELECT COUNT(DISTINCT value->>'stable_evidence_identity_hash')
                         FROM jsonb_array_elements(projection->'evidence_bindings') AS selected(value)
                   )
                   OR (projection->'evidence_bindings') IS DISTINCT FROM (
                       SELECT COALESCE(
                           jsonb_agg(value ORDER BY value->>'evidence_ref_id', value->>'stable_evidence_identity_hash'),
                           '[]'::jsonb
                       )
                         FROM jsonb_array_elements(projection->'evidence_bindings') AS ordered(value)
                   )
                   OR (projection->'source_evidence_ref_ids') IS DISTINCT FROM
                      jsonb_build_array(
                          graph_mapping_restore_evidence_ref(
                              claim_row.source_mention, occurrence_row.evidence_refs
                          )->>'evidence_ref'
                      )
                   OR (projection->'target_evidence_ref_ids') IS DISTINCT FROM
                      jsonb_build_array(
                          graph_mapping_restore_evidence_ref(
                              claim_row.target_mention, occurrence_row.evidence_refs
                          )->>'evidence_ref'
                      ) THEN
                    RAISE EXCEPTION 'canonical mapping evidence subset ordering or endpoint binding is invalid';
                END IF;
                IF graph_mapping_restore_evidence_ref(
                       claim_row.source_mention, occurrence_row.evidence_refs
                   ) IS NULL
                   OR graph_mapping_restore_evidence_ref(
                       claim_row.target_mention, occurrence_row.evidence_refs
                   ) IS NULL
                   OR NOT EXISTS (
                       SELECT 1
                         FROM jsonb_array_elements(projection->'evidence_bindings') AS binding(value)
                        WHERE binding.value->>'evidence_ref_id' =
                           graph_mapping_restore_evidence_ref(
                               claim_row.source_mention, occurrence_row.evidence_refs
                           )->>'evidence_ref'
                   )
                   OR NOT EXISTS (
                       SELECT 1
                         FROM jsonb_array_elements(projection->'evidence_bindings') AS binding(value)
                        WHERE binding.value->>'evidence_ref_id' =
                           graph_mapping_restore_evidence_ref(
                               claim_row.target_mention, occurrence_row.evidence_refs
                           )->>'evidence_ref'
                   ) THEN
                    RAISE EXCEPTION 'canonical mapping endpoint evidence is not in the verified binding subset';
                END IF;
                FOR binding_item IN
                    SELECT value FROM jsonb_array_elements(projection->'evidence_bindings')
                LOOP
                    SELECT value
                      INTO evidence_item
                      FROM jsonb_array_elements(occurrence_row.evidence_refs) AS reference(value)
                     WHERE (value->>'ref_id') = (binding_item->>'evidence_ref_id');
                    IF NOT graph_mapping_json_exact_keys(
                        binding_item,
                        ARRAY[
                            'binding_version', 'evidence_ref_id',
                            'stable_evidence_identity_hash', 'library_id',
                            'document_id', 'document_revision_id', 'revision_no',
                            'job_id', 'extraction_unit_id', 'evidence_id',
                            'unit_id', 'unit_kind', 'ordinal', 'quote_sha256',
                            'unit_text_sha256', 'locator_version',
                            'provenance_status', 'source_span_hash',
                            'evidence_reference_sha256', 'attestation',
                            'binding_fingerprint'
                        ]::text[]
                    )
                       OR NOT graph_mapping_json_exact_keys(
                           binding_item->'attestation',
                           ARRAY[
                               'attestation_version', 'validation_status',
                               'validated_at', 'evidence_reference_sha256',
                               'locator_sha256', 'stable_evidence_identity_hash',
                               'validation_fingerprint'
                           ]::text[]
                       )
                       OR binding_item IS NULL
                       OR evidence_item IS NULL
                       OR (evidence_item->'locator') IS NULL
                       OR jsonb_typeof(evidence_item->'locator') <> 'object'
                       OR (binding_item->>'binding_version') IS DISTINCT FROM 'evidence_validation_v1'
                       OR (binding_item->>'stable_evidence_identity_hash') IS DISTINCT FROM
                            graph_mapping_json_sha256(evidence_item - ARRAY['ref_id','job_id','extraction_unit_id']::text[])
                       OR (binding_item->>'evidence_reference_sha256') IS DISTINCT FROM
                           graph_mapping_json_sha256(evidence_item)
                       OR (binding_item->>'library_id') IS DISTINCT FROM (evidence_item->>'library_id')
                       OR (binding_item->>'document_id') IS DISTINCT FROM (evidence_item->>'document_id')
                       OR (binding_item->>'document_revision_id') IS DISTINCT FROM (evidence_item->>'document_revision_id')
                       OR (binding_item->>'revision_no') IS DISTINCT FROM (evidence_item->>'revision_no')
                       OR (binding_item->>'job_id') IS DISTINCT FROM (evidence_item->>'job_id')
                       OR (binding_item->>'extraction_unit_id') IS DISTINCT FROM (evidence_item->>'extraction_unit_id')
                       OR (binding_item->>'evidence_id') IS DISTINCT FROM (evidence_item->>'evidence_id')
                       OR (binding_item->>'unit_id') IS DISTINCT FROM (evidence_item->>'unit_id')
                       OR (binding_item->>'quote_sha256') IS DISTINCT FROM (evidence_item->>'quote_sha256')
                       OR (binding_item->>'unit_text_sha256') IS DISTINCT FROM (evidence_item->>'unit_text_sha256')
                       OR (binding_item->>'unit_kind') IS DISTINCT FROM (evidence_item->'locator'->>'unit_kind')
                       OR (binding_item->>'ordinal') IS DISTINCT FROM (evidence_item->'locator'->>'ordinal')
                       OR (binding_item->>'locator_version') IS DISTINCT FROM (evidence_item->'locator'->>'locator_version')
                       OR (
                           (evidence_item->'source_span' IS NULL
                            OR jsonb_typeof(evidence_item->'source_span') = 'null')
                           AND binding_item->>'source_span_hash' IS NOT NULL
                       )
                       OR (
                           evidence_item->'source_span' IS NOT NULL
                           AND jsonb_typeof(evidence_item->'source_span') <> 'null'
                           AND (binding_item->>'source_span_hash') IS DISTINCT FROM
                               graph_mapping_json_sha256(evidence_item->'source_span')
                       )
                       OR binding_item->>'provenance_status' <> 'verified'
                       OR jsonb_typeof(binding_item->'attestation') <> 'object'
                       OR (binding_item->'attestation'->>'attestation_version') IS DISTINCT FROM
                           'evidence_validation_attestation_v1'
                       OR binding_item->'attestation'->>'validation_status' <> 'verified'
                       OR NOT graph_mapping_validate_timestamp_text(
                           binding_item->'attestation'->'validated_at'
                       )
                       OR (binding_item->'attestation'->>'stable_evidence_identity_hash') IS DISTINCT FROM
                            graph_mapping_json_sha256(evidence_item - ARRAY['ref_id','job_id','extraction_unit_id']::text[])
                       OR (binding_item->'attestation'->>'evidence_reference_sha256') IS DISTINCT FROM
                           graph_mapping_json_sha256(evidence_item)
                       OR (binding_item->'attestation'->>'locator_sha256') IS DISTINCT FROM
                           graph_mapping_json_sha256(evidence_item->'locator') THEN
                         RAISE EXCEPTION 'canonical mapping evidence identity is not recomputed from source rows';
                    END IF;
                    expected_attestation := jsonb_build_object(
                        'attestation_version', 'evidence_validation_attestation_v1',
                        'validation_status', 'verified',
                        'evidence_reference_sha256', graph_mapping_json_sha256(evidence_item),
                        'locator_sha256', graph_mapping_json_sha256(evidence_item->'locator'),
                        'stable_evidence_identity_hash', graph_mapping_json_sha256(
                            evidence_item - ARRAY['ref_id','job_id','extraction_unit_id']::text[]
                        )
                    );
                    expected_attestation := expected_attestation || jsonb_build_object(
                        'validation_fingerprint', graph_mapping_json_sha256(expected_attestation)
                    );
                    expected_binding_identity_payload := (binding_item - 'binding_fingerprint'::text) ||
                        jsonb_build_object(
                            'attestation', (binding_item->'attestation') - 'validated_at'::text
                        );
                    IF (binding_item->'attestation') IS DISTINCT FROM (
                           expected_attestation || jsonb_build_object(
                               'validated_at', (binding_item->'attestation'->'validated_at')
                           )
                       )
                       OR (binding_item->>'binding_fingerprint') IS DISTINCT FROM
                           graph_mapping_json_sha256(expected_binding_identity_payload) THEN
                        RAISE EXCEPTION 'canonical mapping evidence attestation fingerprint is not bound';
                    END IF;
                END LOOP;
                IF EXISTS (
                    SELECT 1
                      FROM jsonb_array_elements(projection->'evidence_bindings') AS binding(value)
                     WHERE NOT EXISTS (
                         SELECT 1
                           FROM jsonb_array_elements(occurrence_row.evidence_refs) AS reference(value)
                          WHERE reference.value->>'ref_id' = binding.value->>'evidence_ref_id'
                     )
                ) THEN
                    RAISE EXCEPTION 'canonical mapping evidence binding crosses the RawClaim';
                END IF;
                IF EXISTS (
                    SELECT 1
                      FROM jsonb_array_elements_text(
                          (projection->'source_evidence_ref_ids') ||
                          (projection->'target_evidence_ref_ids') ||
                          (projection->'mapping_evidence_ref_ids')
                      ) AS selected_ref(value)
                     WHERE NOT EXISTS (
                         SELECT 1
                           FROM jsonb_array_elements(occurrence_row.evidence_refs) AS reference(value)
                          WHERE reference.value->>'ref_id' = selected_ref.value
                     )
                ) THEN
                    RAISE EXCEPTION 'canonical mapping selected evidence crosses the RawClaim';
                END IF;
                IF EXISTS (
                    SELECT 1
                      FROM jsonb_array_elements(projection->'claim_snapshot'->'evidence_ref_ids') AS snapshot_ref(value)
                     WHERE NOT EXISTS (
                         SELECT 1
                           FROM jsonb_array_elements(occurrence_row.evidence_refs) AS reference(value)
                          WHERE reference.value->>'ref_id' = snapshot_ref.value #>> '{}'
                     )
                ) OR EXISTS (
                    SELECT 1
                      FROM jsonb_array_elements(occurrence_row.evidence_refs) AS reference(value)
                     WHERE NOT EXISTS (
                         SELECT 1
                           FROM jsonb_array_elements(projection->'claim_snapshot'->'evidence_ref_ids') AS snapshot_ref(value)
                          WHERE (snapshot_ref.value #>> '{}') = (reference.value->>'ref_id')
                     )
                ) THEN
                    RAISE EXCEPTION 'canonical mapping claim evidence snapshot is not bound';
                END IF;
                IF EXISTS (
                    SELECT 1
                      FROM jsonb_array_elements(projection->'claim_snapshot'->'evidence_snapshots') AS snapshot_item(value)
                     WHERE NOT graph_mapping_json_exact_keys(
                         snapshot_item.value,
                         ARRAY[
                             'evidence_ref_id', 'stable_evidence_identity_hash',
                             'evidence_reference_sha256', 'quote_sha256',
                             'unit_text_sha256', 'locator_sha256',
                             'source_span_hash', 'snapshot_fingerprint'
                         ]::text[]
                     )
                ) THEN
                    RAISE EXCEPTION 'canonical mapping evidence snapshot contains an unknown or missing key';
                END IF;
                IF (
                    jsonb_typeof(projection->'source_endpoint_resolution') = 'object'
                    AND (
                        NOT graph_mapping_json_exact_keys(
                            projection->'source_endpoint_resolution'->'scope',
                            ARRAY[
                                'library_id', 'document_id', 'document_revision_id',
                                'revision_no', 'job_id', 'extraction_unit_id',
                                'claim_id', 'extraction_occurrence_id'
                            ]::text[]
                        )
                        OR NOT graph_mapping_json_exact_keys(
                            projection->'source_endpoint_resolution'->'entity_link',
                            ARRAY[
                                'mention_local_id', 'status', 'confidence', 'entity_id',
                                'entity_candidate_key_hash', 'resolver_provenance',
                                'link_decision_fingerprint'
                            ]::text[]
                        )
                        OR NOT graph_mapping_json_exact_keys(
                            projection->'source_endpoint_resolution'->'entity_link'->'resolver_provenance',
                            ARRAY[
                                'resolver_key', 'resolver_version', 'resolver_version_hash',
                                'config_hash', 'provenance_fingerprint'
                            ]::text[]
                        )
                    )
                ) OR (
                    jsonb_typeof(projection->'target_endpoint_resolution') = 'object'
                    AND (
                        NOT graph_mapping_json_exact_keys(
                            projection->'target_endpoint_resolution'->'scope',
                            ARRAY[
                                'library_id', 'document_id', 'document_revision_id',
                                'revision_no', 'job_id', 'extraction_unit_id',
                                'claim_id', 'extraction_occurrence_id'
                            ]::text[]
                        )
                        OR NOT graph_mapping_json_exact_keys(
                            projection->'target_endpoint_resolution'->'entity_link',
                            ARRAY[
                                'mention_local_id', 'status', 'confidence', 'entity_id',
                                'entity_candidate_key_hash', 'resolver_provenance',
                                'link_decision_fingerprint'
                            ]::text[]
                        )
                        OR NOT graph_mapping_json_exact_keys(
                            projection->'target_endpoint_resolution'->'entity_link'->'resolver_provenance',
                            ARRAY[
                                'resolver_key', 'resolver_version', 'resolver_version_hash',
                                'config_hash', 'provenance_fingerprint'
                            ]::text[]
                        )
                    )
                ) THEN
                    RAISE EXCEPTION 'canonical mapping endpoint typed authority contains an unknown or missing key';
                END IF;
                IF projection->'source_endpoint_resolution' IS NULL
                   OR jsonb_typeof(projection->'source_endpoint_resolution') = 'null' THEN
                    IF projection->'endpoint_type_binding'->>'source_type_key' IS NOT NULL THEN
                        RAISE EXCEPTION 'canonical source endpoint authority is missing';
                    END IF;
                ELSE
                    IF projection->'endpoint_type_binding'->>'source_type_key' IS NULL
                       OR projection->'source_endpoint_resolution'->>'mention_role' <> 'source'
                       OR (projection->'source_endpoint_resolution'->>'mention_local_id') IS DISTINCT FROM
                          (claim_row.source_mention->>'local_id')
                       OR (projection->'source_endpoint_resolution'->>'mention_surface_sha256') IS DISTINCT FROM
                          graph_mapping_text_sha256(claim_row.source_mention->>'surface')
                       OR (projection->'source_endpoint_resolution'->>'entity_type_key') IS DISTINCT FROM
                          (projection->'endpoint_type_binding'->>'source_type_key')
                       OR NOT EXISTS (
                           SELECT 1
                             FROM jsonb_array_elements_text(
                                 projection->'source_endpoint_resolution'->'evidence_ref_ids'
                             ) AS endpoint_ref(value)
                            WHERE endpoint_ref.value = (
                                graph_mapping_restore_evidence_ref(
                                    claim_row.source_mention, occurrence_row.evidence_refs
                                )->>'evidence_ref'
                            )
                       )
                       OR EXISTS (
                           SELECT 1
                             FROM jsonb_array_elements_text(
                                 projection->'source_endpoint_resolution'->'evidence_ref_ids'
                             ) AS endpoint_ref(value)
                            WHERE NOT EXISTS (
                                SELECT 1
                                  FROM jsonb_array_elements(projection->'evidence_bindings') AS binding(value)
                                 WHERE binding.value->>'evidence_ref_id' = endpoint_ref.value
                            )
                       ) THEN
                        RAISE EXCEPTION 'canonical source endpoint authority is not claim-bound';
                    END IF;
                END IF;
                IF projection->'target_endpoint_resolution' IS NULL
                   OR jsonb_typeof(projection->'target_endpoint_resolution') = 'null' THEN
                    IF projection->'endpoint_type_binding'->>'target_type_key' IS NOT NULL THEN
                        RAISE EXCEPTION 'canonical target endpoint authority is missing';
                    END IF;
                ELSE
                    IF projection->'endpoint_type_binding'->>'target_type_key' IS NULL
                       OR projection->'target_endpoint_resolution'->>'mention_role' <> 'target'
                       OR (projection->'target_endpoint_resolution'->>'mention_local_id') IS DISTINCT FROM
                          (claim_row.target_mention->>'local_id')
                       OR (projection->'target_endpoint_resolution'->>'mention_surface_sha256') IS DISTINCT FROM
                          graph_mapping_text_sha256(claim_row.target_mention->>'surface')
                       OR (projection->'target_endpoint_resolution'->>'entity_type_key') IS DISTINCT FROM
                          (projection->'endpoint_type_binding'->>'target_type_key')
                       OR NOT EXISTS (
                           SELECT 1
                             FROM jsonb_array_elements_text(
                                 projection->'target_endpoint_resolution'->'evidence_ref_ids'
                             ) AS endpoint_ref(value)
                            WHERE endpoint_ref.value = (
                                graph_mapping_restore_evidence_ref(
                                    claim_row.target_mention, occurrence_row.evidence_refs
                                )->>'evidence_ref'
                            )
                       )
                       OR EXISTS (
                           SELECT 1
                             FROM jsonb_array_elements_text(
                                 projection->'target_endpoint_resolution'->'evidence_ref_ids'
                             ) AS endpoint_ref(value)
                            WHERE NOT EXISTS (
                                SELECT 1
                                  FROM jsonb_array_elements(projection->'evidence_bindings') AS binding(value)
                                 WHERE binding.value->>'evidence_ref_id' = endpoint_ref.value
                            )
                       ) THEN
                        RAISE EXCEPTION 'canonical target endpoint authority is not claim-bound';
                    END IF;
                END IF;
                IF (projection->>'reason_code' = 'unknown_direction'
                    AND projection->>'surface_direction' <> 'unknown')
                   OR (projection->>'reason_code' = 'unknown_source_type'
                       AND projection->'endpoint_type_binding'->>'source_type_key' IS NOT NULL)
                   OR (projection->>'reason_code' = 'unknown_target_type'
                       AND projection->'endpoint_type_binding'->>'target_type_key' IS NOT NULL)
                   OR (projection->>'reason_code' = 'unknown_predicate'
                       AND EXISTS (
                           SELECT 1
                             FROM jsonb_array_elements_text(
                                 projection->'frozen_ontology'->'relation_type_keys'
                             ) AS relation(value)
                            WHERE relation.value = projection->>'surface_raw_predicate'
                       )) THEN
                    RAISE EXCEPTION 'canonical mapping unknown reason is not bound to source facts';
                END IF;
                IF (
                    (projection->>'reason_code' = 'unsupported_negation'
                     AND projection->'semantic_projection'->>'negation_value' <> 'true')
                    OR (projection->>'reason_code' = 'unsupported_modality'
                        AND projection->'semantic_projection'->>'modality_present' <> 'true')
                    OR (projection->>'reason_code' = 'unsupported_qualifier'
                        AND projection->'semantic_projection'->>'qualifier_present' <> 'true')
                    OR (projection->>'reason_code' = 'unsupported_valid_time'
                        AND projection->'semantic_projection'->>'valid_time_present' <> 'true')
                    OR (projection->>'reason_code' = 'unsupported_effective_time'
                        AND projection->'semantic_projection'->>'effective_time_present' <> 'true')
                    OR (
                        projection->'semantic_projection'->>'negation_value' = 'true'
                        OR projection->'semantic_projection'->>'modality_present' = 'true'
                        OR projection->'semantic_projection'->>'qualifier_present' = 'true'
                        OR projection->'semantic_projection'->>'valid_time_present' = 'true'
                        OR projection->'semantic_projection'->>'effective_time_present' = 'true'
                    )
                    AND projection->>'reason_code' NOT IN (
                        'unsupported_negation', 'unsupported_modality', 'unsupported_qualifier',
                        'unsupported_valid_time', 'unsupported_effective_time'
                    )
                    OR (
                        projection->>'reason_code' IN (
                            'unsupported_negation', 'unsupported_modality', 'unsupported_qualifier',
                            'unsupported_valid_time', 'unsupported_effective_time'
                        )
                        AND projection->>'semantic_status' = 'preserved'
                    )
                ) THEN
                    RAISE EXCEPTION 'canonical mapping semantic reason is not bound to the projection';
                END IF;
                IF projection->>'decision_id' IS NULL THEN
                    IF COALESCE(jsonb_typeof(projection->'decision_binding'), 'null') <> 'null'
                       OR projection->>'decision_fingerprint' IS NOT NULL
                       OR projection->>'decision_kind' IS NOT NULL THEN
                        RAISE EXCEPTION 'canonical mapping decision binding is inconsistent';
                    END IF;
                ELSE
                    SELECT *
                      INTO decision_row
                      FROM graph_claim_decisions
                     WHERE decision_id = (projection->>'decision_id')::uuid;
                    IF decision_row IS NULL
                       OR decision_row.library_id IS DISTINCT FROM NEW.library_id
                       OR decision_row.document_id IS DISTINCT FROM NEW.document_id
                       OR decision_row.document_revision_id IS DISTINCT FROM NEW.document_revision_id
                       OR decision_row.revision_no IS DISTINCT FROM NEW.revision_no
                       OR decision_row.claim_id IS DISTINCT FROM NEW.claim_id
                       OR (decision_row.extraction_occurrence_id IS NOT NULL
                           AND decision_row.extraction_occurrence_id IS DISTINCT FROM NEW.extraction_occurrence_id)
                       OR decision_row.decision_fingerprint IS DISTINCT FROM (projection->>'decision_fingerprint')
                       OR decision_row.decision_kind IS DISTINCT FROM (projection->>'decision_kind')
                       OR (projection->'decision_binding'->>'decision_id') IS DISTINCT FROM decision_row.decision_id::text
                       OR (projection->'decision_binding'->>'decision_fingerprint') IS DISTINCT FROM decision_row.decision_fingerprint
                       OR (projection->'decision_binding'->>'decision_kind') IS DISTINCT FROM decision_row.decision_kind
                       OR (projection->'decision_binding'->>'decision_version') IS DISTINCT FROM decision_row.decision_version::text
                       OR (projection->'decision_binding'->>'status') IS DISTINCT FROM decision_row.status
                       OR (projection->'decision_binding'->>'reason_code') IS DISTINCT FROM decision_row.reason_code
                       OR (projection->'decision_binding'->>'library_id') IS DISTINCT FROM decision_row.library_id::text
                       OR (projection->'decision_binding'->>'document_id') IS DISTINCT FROM decision_row.document_id::text
                       OR (projection->'decision_binding'->>'document_revision_id') IS DISTINCT FROM decision_row.document_revision_id::text
                       OR (projection->'decision_binding'->>'revision_no') IS DISTINCT FROM decision_row.revision_no::text
                       OR (projection->'decision_binding'->>'job_id') IS DISTINCT FROM NEW.job_id::text
                       OR (projection->'decision_binding'->>'extraction_unit_id') IS DISTINCT FROM NEW.extraction_unit_id::text
                       OR (projection->'decision_binding'->>'claim_id') IS DISTINCT FROM decision_row.claim_id::text
                       OR (projection->'decision_binding'->>'extraction_occurrence_id') IS DISTINCT FROM decision_row.extraction_occurrence_id::text THEN
                        RAISE EXCEPTION 'canonical mapping decision crosses the RawClaim scope';
                    END IF;
                    expected_decision_fingerprint := graph_mapping_decision_fingerprint(
                        decision_row.decision_schema_version,
                        decision_row.decision_version,
                        decision_row.library_id,
                        decision_row.document_id,
                        decision_row.document_revision_id,
                        decision_row.revision_no,
                        decision_row.claim_id,
                        decision_row.extraction_occurrence_id,
                        decision_row.decision_kind,
                        decision_row.status,
                        decision_row.reason_code,
                        decision_row.created_by_kind,
                        decision_row.producer_key,
                        decision_row.producer_version,
                        decision_row.proposal
                    );
                    IF expected_decision_fingerprint IS DISTINCT FROM decision_row.decision_fingerprint
                       OR decision_row.decision_id IS DISTINCT FROM graph_mapping_uuid5(
                             '6d26de8e-6a6f-5f5a-9f8c-1cb0a3a8e8e1'::uuid,
                             'claim_decision_projection_v1:' || expected_decision_fingerprint
                           )
                       OR (decision_row.proposal->>'raw_predicate') IS DISTINCT FROM claim_row.raw_predicate
                       OR (decision_row.proposal->>'surface_direction') IS DISTINCT FROM claim_row.surface_direction
                       OR EXISTS (
                           SELECT 1
                             FROM jsonb_array_elements_text(
                                 COALESCE(decision_row.proposal->'evidence_ref_ids', '[]'::jsonb)
                             ) AS selected_ref(value)
                            WHERE NOT EXISTS (
                                SELECT 1
                                  FROM jsonb_array_elements(occurrence_row.evidence_refs) AS reference(value)
                                 WHERE reference.value->>'ref_id' = selected_ref.value
                            )
                       ) THEN
                         RAISE EXCEPTION 'canonical mapping decision identity is not recomputed from the source row';
                    END IF;
                    IF decision_row.decision_kind = 'mapping_candidate'
                       AND ARRAY(
                           SELECT key
                             FROM jsonb_object_keys(decision_row.proposal) AS object_key(key)
                            ORDER BY key
                       ) <> ARRAY[
                           'evidence_ref_ids', 'raw_predicate', 'source_mention',
                           'suggested_canonical_key', 'surface_direction', 'target_mention'
                       ]::text[] THEN
                        RAISE EXCEPTION 'canonical mapping decision proposal shape is invalid';
                    END IF;
                    IF decision_row.decision_kind = 'schema_extension_candidate'
                       AND ARRAY(
                           SELECT key
                             FROM jsonb_object_keys(decision_row.proposal) AS object_key(key)
                            ORDER BY key
                       ) <> ARRAY[
                           'evidence_ref_ids', 'raw_predicate', 'source_endpoint',
                           'surface_direction', 'target_endpoint'
                       ]::text[] THEN
                        RAISE EXCEPTION 'canonical schema extension proposal shape is invalid';
                    END IF;
                    IF decision_row.decision_kind = 'mapping_candidate'
                       AND (
                           (decision_row.proposal->'source_mention') IS DISTINCT FROM
                               graph_mapping_restore_evidence_ref(
                                   claim_row.source_mention, occurrence_row.evidence_refs
                               )
                           OR (decision_row.proposal->'target_mention') IS DISTINCT FROM
                               graph_mapping_restore_evidence_ref(
                                   claim_row.target_mention, occurrence_row.evidence_refs
                               )
                           OR NOT EXISTS (
                               SELECT 1
                                 FROM jsonb_array_elements_text(
                                     decision_row.proposal->'evidence_ref_ids'
                                 ) AS selected_ref(value)
                                WHERE selected_ref.value = (
                                    graph_mapping_restore_evidence_ref(
                                        claim_row.source_mention, occurrence_row.evidence_refs
                                    )->>'evidence_ref'
                                )
                           )
                           OR NOT EXISTS (
                               SELECT 1
                                 FROM jsonb_array_elements_text(
                                     decision_row.proposal->'evidence_ref_ids'
                                 ) AS selected_ref(value)
                                WHERE selected_ref.value = (
                                    graph_mapping_restore_evidence_ref(
                                        claim_row.target_mention, occurrence_row.evidence_refs
                                    )->>'evidence_ref'
                                )
                           )
                       ) THEN
                        RAISE EXCEPTION 'canonical mapping decision proposal is not claim-bound';
                    END IF;
                    IF decision_row.decision_kind = 'schema_extension_candidate'
                       AND (
                           ARRAY(
                               SELECT key
                                 FROM jsonb_object_keys(decision_row.proposal->'source_endpoint') AS object_key(key)
                                ORDER BY key
                           ) <> ARRAY[
                               'entity_type_hint', 'evidence_ref_ids', 'local_id', 'surface'
                           ]::text[]
                           OR ARRAY(
                               SELECT key
                                 FROM jsonb_object_keys(decision_row.proposal->'target_endpoint') AS object_key(key)
                                ORDER BY key
                           ) <> ARRAY[
                               'entity_type_hint', 'evidence_ref_ids', 'local_id', 'surface'
                           ]::text[]
                           OR
                           (decision_row.proposal->'source_endpoint'->>'local_id') IS DISTINCT FROM
                               (claim_row.source_mention->>'local_id')
                           OR (decision_row.proposal->'source_endpoint'->>'surface') IS DISTINCT FROM
                               (claim_row.source_mention->>'surface')
                           OR (decision_row.proposal->'source_endpoint'->>'entity_type_hint') IS DISTINCT FROM
                               (claim_row.source_mention->>'entity_type_hint')
                           OR (decision_row.proposal->'target_endpoint'->>'local_id') IS DISTINCT FROM
                               (claim_row.target_mention->>'local_id')
                           OR (decision_row.proposal->'target_endpoint'->>'surface') IS DISTINCT FROM
                               (claim_row.target_mention->>'surface')
                           OR (decision_row.proposal->'target_endpoint'->>'entity_type_hint') IS DISTINCT FROM
                               (claim_row.target_mention->>'entity_type_hint')
                           OR NOT EXISTS (
                               SELECT 1
                                 FROM jsonb_array_elements_text(
                                     decision_row.proposal->'source_endpoint'->'evidence_ref_ids'
                                 ) AS selected_ref(value)
                                WHERE selected_ref.value = (
                                    graph_mapping_restore_evidence_ref(
                                        claim_row.source_mention, occurrence_row.evidence_refs
                                    )->>'evidence_ref'
                                )
                           )
                           OR NOT EXISTS (
                               SELECT 1
                                 FROM jsonb_array_elements_text(
                                     decision_row.proposal->'target_endpoint'->'evidence_ref_ids'
                                 ) AS selected_ref(value)
                                WHERE selected_ref.value = (
                                    graph_mapping_restore_evidence_ref(
                                        claim_row.target_mention, occurrence_row.evidence_refs
                                    )->>'evidence_ref'
                                )
                           )
                           OR EXISTS (
                               SELECT 1
                                 FROM jsonb_array_elements_text(
                                     COALESCE((decision_row.proposal->'source_endpoint'->'evidence_ref_ids'), '[]'::jsonb)
                                     || COALESCE((decision_row.proposal->'target_endpoint'->'evidence_ref_ids'), '[]'::jsonb)
                                 ) AS selected_ref(value)
                                WHERE NOT EXISTS (
                                    SELECT 1
                                      FROM jsonb_array_elements(occurrence_row.evidence_refs) AS reference(value)
                                     WHERE (reference.value->>'ref_id') = selected_ref.value
                                )
                           )
                       ) THEN
                        RAISE EXCEPTION 'canonical schema extension decision proposal is not claim-bound';
                    END IF;
                    expected_decision_payload_hash := graph_mapping_json_sha256(
                        jsonb_build_object(
                            'decision_id', decision_row.decision_id::text,
                            'decision_fingerprint', decision_row.decision_fingerprint,
                            'decision_schema_version', decision_row.decision_schema_version,
                            'decision_version', decision_row.decision_version,
                            'library_id', decision_row.library_id::text,
                            'document_id', decision_row.document_id::text,
                            'document_revision_id', decision_row.document_revision_id::text,
                            'revision_no', decision_row.revision_no,
                            'claim_id', decision_row.claim_id::text,
                            'extraction_occurrence_id', decision_row.extraction_occurrence_id::text,
                            'decision_kind', decision_row.decision_kind,
                            'status', decision_row.status,
                            'reason_code', decision_row.reason_code,
                            'created_by_kind', decision_row.created_by_kind,
                            'producer_key', decision_row.producer_key,
                            'producer_version', decision_row.producer_version,
                            'created_at', graph_mapping_timestamp_text(decision_row.created_at),
                             'proposal', decision_row.proposal
                         )
                     );
                    expected_binding := jsonb_build_object(
                        'decision_schema_version', decision_row.decision_schema_version,
                        'decision_id', decision_row.decision_id::text,
                        'decision_fingerprint', decision_row.decision_fingerprint,
                        'decision_version', decision_row.decision_version,
                        'decision_kind', decision_row.decision_kind,
                        'status', decision_row.status,
                        'reason_code', decision_row.reason_code,
                        'library_id', decision_row.library_id::text,
                        'document_id', decision_row.document_id::text,
                        'document_revision_id', decision_row.document_revision_id::text,
                        'revision_no', decision_row.revision_no,
                        'job_id', NEW.job_id::text,
                        'extraction_unit_id', NEW.extraction_unit_id::text,
                        'claim_id', decision_row.claim_id::text,
                        'extraction_occurrence_id', decision_row.extraction_occurrence_id::text,
                        'decision_payload_sha256', expected_decision_payload_hash
                    );
                    expected_binding := expected_binding || jsonb_build_object(
                        'binding_fingerprint', graph_mapping_json_sha256(expected_binding)
                    );
                    IF (projection->'decision_binding') IS DISTINCT FROM expected_binding THEN
                         RAISE EXCEPTION 'canonical mapping decision payload identity is not recomputed';
                    END IF;
                END IF;
                expected_attempt_payload := jsonb_build_object(
                    'mapping_schema_hash', projection->'mapping_schema_hash',
                    'canonical_schema_hash', projection->'canonical_schema_hash',
                    'raw_claim_authority_sha256', projection->'raw_claim_authority_sha256',
                    'mapping_version', projection->'mapping_version',
                    'scope', jsonb_build_object(
                        'library_id', projection->'library_id',
                        'document_id', projection->'document_id',
                        'document_revision_id', projection->'document_revision_id',
                        'revision_no', projection->'revision_no',
                        'job_id', projection->'job_id',
                        'extraction_unit_id', projection->'extraction_unit_id'
                    ),
                    'claim_id', projection->'claim_id',
                    'claim_content_scoped_fingerprint', projection->'claim_content_scoped_fingerprint',
                    'extraction_occurrence_id', projection->'extraction_occurrence_id',
                    'extraction_occurrence_fingerprint', projection->'extraction_occurrence_fingerprint',
                    'surface_raw_predicate', projection->'surface_raw_predicate',
                    'surface_direction', projection->'surface_direction',
                    'endpoint_type_binding', projection->'endpoint_type_binding',
                    'source_endpoint_resolution', projection->'source_endpoint_resolution',
                    'target_endpoint_resolution', projection->'target_endpoint_resolution',
                    'evidence_bindings', graph_mapping_evidence_bindings_attempt(projection->'evidence_bindings'),
                    'claim_snapshot', projection->'claim_snapshot',
                    'frozen_ontology', projection->'frozen_ontology',
                    'ontology_version_id', projection->'ontology_version_id',
                    'ontology_snapshot_hash', projection->'ontology_snapshot_hash',
                    'ontology_contract_version', projection->'ontology_contract_version',
                    'authorization_registry_snapshot', projection->'authorization_registry_snapshot',
                    'semantic_projection_fingerprint', projection->'semantic_projection'->'semantic_projection_fingerprint',
                    'provenance', projection->'provenance',
                    'authorization_provenance', projection->'authorization_provenance',
                    'decision_binding', projection->'decision_binding',
                    'decision_id', projection->'decision_id',
                    'decision_fingerprint', projection->'decision_fingerprint',
                    'decision_kind', projection->'decision_kind'
                );
                expected_attempt_fingerprint := graph_mapping_json_sha256(expected_attempt_payload);
                expected_result_payload := (projection - ARRAY[
                     'mapping_attempt_id', 'mapping_attempt_fingerprint',
                     'mapping_result_id', 'mapping_result_fingerprint', 'created_at'
                ]::text[]) || jsonb_build_object(
                    'mapping_attempt_fingerprint', expected_attempt_fingerprint,
                    'evidence_bindings', graph_mapping_evidence_bindings_attempt(
                        projection->'evidence_bindings'
                    )
                );
                expected_result_fingerprint := graph_mapping_json_sha256(expected_result_payload);
                IF NEW.mapping_attempt_fingerprint IS DISTINCT FROM expected_attempt_fingerprint
                   OR (projection->>'mapping_attempt_fingerprint') IS DISTINCT FROM expected_attempt_fingerprint
                   OR NEW.mapping_attempt_id IS DISTINCT FROM graph_mapping_uuid5(
                        '2cb2c2a1-5d36-5b6b-9c3e-21a96e8b7f40'::uuid,
                        'canonical_mapping_attempt_v1:' || expected_attempt_fingerprint
                      )
                   OR (projection->>'mapping_attempt_id') IS DISTINCT FROM NEW.mapping_attempt_id::text
                   OR NEW.mapping_result_fingerprint IS DISTINCT FROM expected_result_fingerprint
                   OR (projection->>'mapping_result_fingerprint') IS DISTINCT FROM expected_result_fingerprint
                   OR NEW.mapping_result_id IS DISTINCT FROM graph_mapping_uuid5(
                        '2cb2c2a1-5d36-5b6b-9c3e-21a96e8b7f40'::uuid,
                        'canonical_mapping_result_v1:' || expected_result_fingerprint
                      )
                    OR (projection->>'mapping_result_id') IS DISTINCT FROM NEW.mapping_result_id::text THEN
                     RAISE EXCEPTION 'canonical mapping result identity is not recomputed from the complete projection';
                END IF;
                IF (projection->'frozen_ontology'->>'ontology_snapshot_hash') IS DISTINCT FROM
                   graph_mapping_json_sha256((projection->'frozen_ontology') - 'ontology_snapshot_hash'::text)
                   OR (
                       projection->'authorization_provenance' IS NOT NULL
                       AND jsonb_typeof(projection->'authorization_provenance') <> 'null'
                       AND (projection->'authorization_provenance'->>'authorization_fingerprint') IS DISTINCT FROM
                           graph_mapping_json_sha256(
                               (projection->'authorization_provenance') - 'authorization_fingerprint'::text
                           )
                   )
                   OR (projection->'provenance'->>'provenance_fingerprint') IS DISTINCT FROM
                   graph_mapping_json_sha256(
                       (projection->'provenance') - 'provenance_fingerprint'::text
                   )
                   OR (projection->'source_provenance'->>'provenance_fingerprint') IS DISTINCT FROM
                   graph_mapping_json_sha256(
                       (projection->'source_provenance') - 'provenance_fingerprint'::text
                   )
                   OR (projection->'actor_provenance'->>'provenance_fingerprint') IS DISTINCT FROM
                   graph_mapping_json_sha256(
                       (projection->'actor_provenance') - 'provenance_fingerprint'::text
                   )
                   OR jsonb_typeof(projection->'source_provenance'->'source_kind') <> 'string'
                   OR projection->'source_provenance'->>'source_kind' IS NULL
                   OR projection->'source_provenance'->>'source_kind' NOT IN
                      ('raw_claim', 'decision_projection', 'explicit_proposal')
                   OR jsonb_typeof(projection->'source_provenance'->'source_key') <> 'string'
                   OR projection->'source_provenance'->>'source_key' IS NULL
                   OR btrim(projection->'source_provenance'->>'source_key') = ''
                   OR btrim(projection->'source_provenance'->>'source_key') <>
                      projection->'source_provenance'->>'source_key'
                   OR length(projection->'source_provenance'->>'source_key') > 128
                   OR jsonb_typeof(projection->'source_provenance'->'source_version') <> 'string'
                   OR projection->'source_provenance'->>'source_version' IS NULL
                   OR btrim(projection->'source_provenance'->>'source_version') = ''
                   OR btrim(projection->'source_provenance'->>'source_version') <>
                      projection->'source_provenance'->>'source_version'
                   OR length(projection->'source_provenance'->>'source_version') > 128
                   OR jsonb_typeof(projection->'source_provenance'->'source_hash') <> 'string'
                   OR projection->'source_provenance'->>'source_hash' IS NULL
                   OR projection->'source_provenance'->>'source_hash' !~ '^[0-9a-f]{64}$'
                   OR jsonb_typeof(projection->'source_provenance'->'source_precedence') <> 'number'
                   OR jsonb_typeof(projection->'actor_provenance'->'actor_kind') <> 'string'
                   OR projection->'actor_provenance'->>'actor_kind' IS NULL
                   OR projection->'actor_provenance'->>'actor_kind' NOT IN ('system', 'external', 'human')
                   OR jsonb_typeof(projection->'actor_provenance'->'actor_key') <> 'string'
                   OR projection->'actor_provenance'->>'actor_key' IS NULL
                   OR btrim(projection->'actor_provenance'->>'actor_key') = ''
                   OR btrim(projection->'actor_provenance'->>'actor_key') <>
                      projection->'actor_provenance'->>'actor_key'
                   OR length(projection->'actor_provenance'->>'actor_key') > 128
                   OR jsonb_typeof(projection->'actor_provenance'->'actor_precedence') <> 'number'
                   OR (
                       (projection->'source_provenance'->>'source_kind' = 'raw_claim'
                        AND (projection->'source_provenance'->>'source_precedence') IS DISTINCT FROM '1')
                       OR (projection->'source_provenance'->>'source_kind' = 'decision_projection'
                           AND (projection->'source_provenance'->>'source_precedence') IS DISTINCT FROM '2')
                       OR (projection->'source_provenance'->>'source_kind' = 'explicit_proposal'
                           AND (projection->'source_provenance'->>'source_precedence') IS DISTINCT FROM '3')
                       OR (projection->'source_provenance'->>'source_precedence') IS DISTINCT FROM
                           CASE (projection->'source_provenance'->>'source_kind')
                               WHEN 'raw_claim' THEN '1'
                               WHEN 'decision_projection' THEN '2'
                               WHEN 'explicit_proposal' THEN '3'
                               ELSE NULL
                           END
                   )
                   OR (
                       (projection->'actor_provenance'->>'actor_kind' = 'system'
                        AND (projection->'actor_provenance'->>'actor_precedence') IS DISTINCT FROM '1')
                       OR (projection->'actor_provenance'->>'actor_kind' = 'external'
                           AND (projection->'actor_provenance'->>'actor_precedence') IS DISTINCT FROM '2')
                       OR (projection->'actor_provenance'->>'actor_kind' = 'human'
                           AND (projection->'actor_provenance'->>'actor_precedence') IS DISTINCT FROM '3')
                       OR (projection->'actor_provenance'->>'actor_precedence') IS DISTINCT FROM
                           CASE (projection->'actor_provenance'->>'actor_kind')
                               WHEN 'system' THEN '1'
                               WHEN 'external' THEN '2'
                               WHEN 'human' THEN '3'
                               ELSE NULL
                           END
                   ) THEN
                    RAISE EXCEPTION 'canonical mapping provenance is not content-bound';
                END IF;
                IF projection->'source_endpoint_resolution' IS NOT NULL
                   AND jsonb_typeof(projection->'source_endpoint_resolution') <> 'null'
                   AND (
                       (projection->'source_endpoint_resolution'->>'attestation_fingerprint') IS DISTINCT FROM
                           graph_mapping_json_sha256(
                               (projection->'source_endpoint_resolution') - 'attestation_fingerprint'::text
                           )
                       OR projection->'source_endpoint_resolution'->>'mention_role' <> 'source'
                       OR (projection->'source_endpoint_resolution'->>'mention_local_id') IS DISTINCT FROM
                           (projection->'claim_snapshot'->>'source_mention_local_id')
                       OR (projection->'source_endpoint_resolution'->>'mention_surface_sha256') IS DISTINCT FROM
                           (projection->'claim_snapshot'->>'source_mention_surface_sha256')
                       OR (projection->'source_endpoint_resolution'->'scope') IS DISTINCT FROM
                           jsonb_build_object(
                               'library_id', NEW.library_id::text,
                               'document_id', NEW.document_id::text,
                               'document_revision_id', NEW.document_revision_id::text,
                               'revision_no', NEW.revision_no,
                               'job_id', NEW.job_id::text,
                               'extraction_unit_id', NEW.extraction_unit_id::text,
                               'claim_id', NEW.claim_id::text,
                               'extraction_occurrence_id', NEW.extraction_occurrence_id::text
                           )
                       OR EXISTS (
                           SELECT 1
                             FROM jsonb_array_elements_text(
                                 projection->'source_endpoint_resolution'->'evidence_ref_ids'
                             ) AS endpoint_ref(value)
                            WHERE NOT EXISTS (
                                SELECT 1
                                  FROM jsonb_array_elements(projection->'evidence_bindings') AS binding(value)
                                 WHERE binding.value->>'evidence_ref_id' = endpoint_ref.value
                            )
                       )
                       OR (projection->'source_endpoint_resolution'->'entity_link'->>'mention_local_id') IS DISTINCT FROM
                           (projection->'source_endpoint_resolution'->>'mention_local_id')
                       OR jsonb_typeof(projection->'source_endpoint_resolution'->'entity_link') <> 'object'
                       OR projection->'source_endpoint_resolution'->'entity_link'->>'status' NOT IN
                          ('resolved', 'candidate', 'unresolved', 'rejected')
                       OR (
                           projection->'source_endpoint_resolution'->'entity_link'->>'status' = 'resolved'
                           AND (
                               projection->'source_endpoint_resolution'->'entity_link'->>'entity_id' IS NULL
                               OR projection->'source_endpoint_resolution'->'entity_link'->>'confidence' IS NULL
                               OR jsonb_typeof(projection->'source_endpoint_resolution'->'entity_link'->'confidence') <> 'number'
                               OR projection->'source_endpoint_resolution'->'entity_link'->>'entity_candidate_key_hash' IS NOT NULL
                           )
                       )
                       OR (
                           projection->'source_endpoint_resolution'->'entity_link'->>'status' = 'candidate'
                           AND (
                               projection->'source_endpoint_resolution'->'entity_link'->>'entity_id' IS NOT NULL
                               OR projection->'source_endpoint_resolution'->'entity_link'->>'confidence' IS NULL
                               OR jsonb_typeof(projection->'source_endpoint_resolution'->'entity_link'->'confidence') <> 'number'
                               OR projection->'source_endpoint_resolution'->'entity_link'->>'entity_candidate_key_hash' IS NULL
                           )
                       )
                       OR (
                           projection->'source_endpoint_resolution'->'entity_link'->>'status' IN ('unresolved', 'rejected')
                           AND (
                               projection->'source_endpoint_resolution'->'entity_link'->>'entity_id' IS NOT NULL
                               OR projection->'source_endpoint_resolution'->'entity_link'->>'confidence' IS NOT NULL
                               OR projection->'source_endpoint_resolution'->'entity_link'->>'entity_candidate_key_hash' IS NOT NULL
                           )
                       )
                       OR (projection->'source_endpoint_resolution'->'entity_link'->'resolver_provenance'->>'provenance_fingerprint') IS DISTINCT FROM
                           graph_mapping_json_sha256(
                               (projection->'source_endpoint_resolution'->'entity_link'->'resolver_provenance')
                               - 'provenance_fingerprint'::text
                           )
                       OR (projection->'source_endpoint_resolution'->'entity_link'->>'link_decision_fingerprint') IS DISTINCT FROM
                           graph_mapping_json_sha256(
                               (projection->'source_endpoint_resolution'->'entity_link')
                               - 'link_decision_fingerprint'::text
                           )
                       OR projection->'source_endpoint_resolution'->'entity_link'->>'link_decision_fingerprint' IS NULL
                       OR projection->'source_endpoint_resolution'->'entity_link'->'resolver_provenance'->>'resolver_version_hash' IS NULL
                       OR projection->'source_endpoint_resolution'->'entity_link'->'resolver_provenance'->>'config_hash' IS NULL
                   ) THEN
                    RAISE EXCEPTION 'canonical source endpoint authority is not content-bound';
                END IF;
                IF projection->'target_endpoint_resolution' IS NOT NULL
                   AND jsonb_typeof(projection->'target_endpoint_resolution') <> 'null'
                   AND (
                       (projection->'target_endpoint_resolution'->>'attestation_fingerprint') IS DISTINCT FROM
                           graph_mapping_json_sha256(
                               (projection->'target_endpoint_resolution') - 'attestation_fingerprint'::text
                           )
                       OR projection->'target_endpoint_resolution'->>'mention_role' <> 'target'
                       OR (projection->'target_endpoint_resolution'->>'mention_local_id') IS DISTINCT FROM
                           (projection->'claim_snapshot'->>'target_mention_local_id')
                       OR (projection->'target_endpoint_resolution'->>'mention_surface_sha256') IS DISTINCT FROM
                           (projection->'claim_snapshot'->>'target_mention_surface_sha256')
                       OR (projection->'target_endpoint_resolution'->'scope') IS DISTINCT FROM
                           jsonb_build_object(
                               'library_id', NEW.library_id::text,
                               'document_id', NEW.document_id::text,
                               'document_revision_id', NEW.document_revision_id::text,
                               'revision_no', NEW.revision_no,
                               'job_id', NEW.job_id::text,
                               'extraction_unit_id', NEW.extraction_unit_id::text,
                               'claim_id', NEW.claim_id::text,
                               'extraction_occurrence_id', NEW.extraction_occurrence_id::text
                           )
                       OR EXISTS (
                           SELECT 1
                             FROM jsonb_array_elements_text(
                                 projection->'target_endpoint_resolution'->'evidence_ref_ids'
                             ) AS endpoint_ref(value)
                            WHERE NOT EXISTS (
                                SELECT 1
                                  FROM jsonb_array_elements(projection->'evidence_bindings') AS binding(value)
                                 WHERE binding.value->>'evidence_ref_id' = endpoint_ref.value
                            )
                       )
                       OR (projection->'target_endpoint_resolution'->'entity_link'->>'mention_local_id') IS DISTINCT FROM
                           (projection->'target_endpoint_resolution'->>'mention_local_id')
                       OR jsonb_typeof(projection->'target_endpoint_resolution'->'entity_link') <> 'object'
                       OR projection->'target_endpoint_resolution'->'entity_link'->>'status' NOT IN
                          ('resolved', 'candidate', 'unresolved', 'rejected')
                       OR (
                           projection->'target_endpoint_resolution'->'entity_link'->>'status' = 'resolved'
                           AND (
                               projection->'target_endpoint_resolution'->'entity_link'->>'entity_id' IS NULL
                               OR projection->'target_endpoint_resolution'->'entity_link'->>'confidence' IS NULL
                               OR jsonb_typeof(projection->'target_endpoint_resolution'->'entity_link'->'confidence') <> 'number'
                               OR projection->'target_endpoint_resolution'->'entity_link'->>'entity_candidate_key_hash' IS NOT NULL
                           )
                       )
                       OR (
                           projection->'target_endpoint_resolution'->'entity_link'->>'status' = 'candidate'
                           AND (
                               projection->'target_endpoint_resolution'->'entity_link'->>'entity_id' IS NOT NULL
                               OR projection->'target_endpoint_resolution'->'entity_link'->>'confidence' IS NULL
                               OR jsonb_typeof(projection->'target_endpoint_resolution'->'entity_link'->'confidence') <> 'number'
                               OR projection->'target_endpoint_resolution'->'entity_link'->>'entity_candidate_key_hash' IS NULL
                           )
                       )
                       OR (
                           projection->'target_endpoint_resolution'->'entity_link'->>'status' IN ('unresolved', 'rejected')
                           AND (
                               projection->'target_endpoint_resolution'->'entity_link'->>'entity_id' IS NOT NULL
                               OR projection->'target_endpoint_resolution'->'entity_link'->>'confidence' IS NOT NULL
                               OR projection->'target_endpoint_resolution'->'entity_link'->>'entity_candidate_key_hash' IS NOT NULL
                           )
                       )
                       OR (projection->'target_endpoint_resolution'->'entity_link'->'resolver_provenance'->>'provenance_fingerprint') IS DISTINCT FROM
                           graph_mapping_json_sha256(
                               (projection->'target_endpoint_resolution'->'entity_link'->'resolver_provenance')
                               - 'provenance_fingerprint'::text
                           )
                       OR (projection->'target_endpoint_resolution'->'entity_link'->>'link_decision_fingerprint') IS DISTINCT FROM
                           graph_mapping_json_sha256(
                               (projection->'target_endpoint_resolution'->'entity_link')
                               - 'link_decision_fingerprint'::text
                           )
                       OR projection->'target_endpoint_resolution'->'entity_link'->>'link_decision_fingerprint' IS NULL
                       OR projection->'target_endpoint_resolution'->'entity_link'->'resolver_provenance'->>'resolver_version_hash' IS NULL
                       OR projection->'target_endpoint_resolution'->'entity_link'->'resolver_provenance'->>'config_hash' IS NULL
                   ) THEN
                    RAISE EXCEPTION 'canonical target endpoint authority is not content-bound';
                END IF;
                IF projection->>'outcome' = 'mapped' THEN
                    IF NOT graph_mapping_json_exact_keys(
                        projection->'canonical_source_endpoint'->'entity_link',
                        ARRAY[
                            'mention_local_id', 'status', 'confidence', 'entity_id',
                            'entity_candidate_key_hash', 'resolver_provenance',
                            'link_decision_fingerprint'
                        ]::text[]
                    )
                       OR NOT graph_mapping_json_exact_keys(
                           projection->'canonical_source_endpoint'->'entity_link'->'resolver_provenance',
                           ARRAY[
                               'resolver_key', 'resolver_version', 'resolver_version_hash',
                               'config_hash', 'provenance_fingerprint'
                           ]::text[]
                       )
                       OR NOT graph_mapping_json_exact_keys(
                           projection->'canonical_source_endpoint'->'resolution_attestation',
                           ARRAY[
                               'attestation_version', 'mention_role',
                               'mention_local_id', 'mention_surface_sha256',
                               'entity_type_key', 'entity_link', 'evidence_ref_ids',
                               'scope', 'attestation_fingerprint'
                           ]::text[]
                       )
                       OR NOT graph_mapping_json_exact_keys(
                           projection->'canonical_target_endpoint'->'entity_link',
                           ARRAY[
                               'mention_local_id', 'status', 'confidence', 'entity_id',
                               'entity_candidate_key_hash', 'resolver_provenance',
                               'link_decision_fingerprint'
                           ]::text[]
                       )
                       OR NOT graph_mapping_json_exact_keys(
                           projection->'canonical_target_endpoint'->'entity_link'->'resolver_provenance',
                           ARRAY[
                               'resolver_key', 'resolver_version', 'resolver_version_hash',
                               'config_hash', 'provenance_fingerprint'
                           ]::text[]
                       )
                       OR NOT graph_mapping_json_exact_keys(
                           projection->'canonical_target_endpoint'->'resolution_attestation',
                           ARRAY[
                               'attestation_version', 'mention_role',
                               'mention_local_id', 'mention_surface_sha256',
                               'entity_type_key', 'entity_link', 'evidence_ref_ids',
                               'scope', 'attestation_fingerprint'
                           ]::text[]
                       ) THEN
                        RAISE EXCEPTION 'mapped canonical endpoint contains an unknown or missing key';
                    END IF;
                    IF (projection->'source_endpoint_resolution'->'scope') IS DISTINCT FROM
                       jsonb_build_object(
                           'library_id', NEW.library_id::text,
                           'document_id', NEW.document_id::text,
                           'document_revision_id', NEW.document_revision_id::text,
                           'revision_no', NEW.revision_no,
                           'job_id', NEW.job_id::text,
                           'extraction_unit_id', NEW.extraction_unit_id::text,
                           'claim_id', NEW.claim_id::text,
                           'extraction_occurrence_id', NEW.extraction_occurrence_id::text
                       )
                       OR (projection->'target_endpoint_resolution'->'scope') IS DISTINCT FROM
                       jsonb_build_object(
                           'library_id', NEW.library_id::text,
                           'document_id', NEW.document_id::text,
                           'document_revision_id', NEW.document_revision_id::text,
                           'revision_no', NEW.revision_no,
                           'job_id', NEW.job_id::text,
                           'extraction_unit_id', NEW.extraction_unit_id::text,
                           'claim_id', NEW.claim_id::text,
                           'extraction_occurrence_id', NEW.extraction_occurrence_id::text
                       ) THEN
                        RAISE EXCEPTION 'canonical endpoint resolution scope is not bound';
                    END IF;
                    IF EXISTS (
                        SELECT 1
                          FROM jsonb_array_elements_text(projection->'source_endpoint_resolution'->'evidence_ref_ids') AS endpoint_ref(value)
                         WHERE NOT EXISTS (
                             SELECT 1
                               FROM jsonb_array_elements(occurrence_row.evidence_refs) AS reference(value)
                              WHERE reference.value->>'ref_id' = endpoint_ref.value
                         )
                    ) OR EXISTS (
                        SELECT 1
                          FROM jsonb_array_elements_text(projection->'target_endpoint_resolution'->'evidence_ref_ids') AS endpoint_ref(value)
                         WHERE NOT EXISTS (
                             SELECT 1
                               FROM jsonb_array_elements(occurrence_row.evidence_refs) AS reference(value)
                              WHERE reference.value->>'ref_id' = endpoint_ref.value
                         )
                    ) THEN
                        RAISE EXCEPTION 'canonical endpoint resolution evidence crosses the RawClaim';
                    END IF;
                    IF projection->>'decision_kind' = 'schema_extension_candidate'
                       OR projection->>'surface_direction' = 'unknown'
                       OR projection->'authorization_provenance' IS NULL
                       OR jsonb_typeof(projection->'authorization_provenance') <> 'object'
                       OR (projection->'authorization_provenance'->'registry_snapshot') IS DISTINCT FROM
                          (projection->'authorization_registry_snapshot')
                       OR (projection->'authorization_provenance'->>'registry_hash') IS DISTINCT FROM
                          (projection->'authorization_registry_snapshot'->>'registry_snapshot_hash')
                       OR (projection->'authorization_provenance'->>'authorized_surface_predicate_sha256') IS DISTINCT FROM
                          graph_mapping_text_sha256(projection->>'surface_raw_predicate')
                       OR (projection->'authorization_provenance'->>'authorized_canonical_relation_key_sha256') IS DISTINCT FROM
                          graph_mapping_text_sha256(projection->>'canonical_relation_key')
                       OR (projection->'authorization_provenance'->>'authorization_fingerprint') IS DISTINCT FROM
                          graph_mapping_json_sha256(
                              (projection->'authorization_provenance') - 'authorization_fingerprint'::text
                          )
                       OR jsonb_typeof(projection->'canonical_source_endpoint') <> 'object'
                       OR jsonb_typeof(projection->'canonical_target_endpoint') <> 'object'
                       OR jsonb_typeof(projection->'source_endpoint_resolution') <> 'object'
                       OR jsonb_typeof(projection->'target_endpoint_resolution') <> 'object'
                       OR projection->'canonical_source_endpoint'->>'role' <> 'source'
                       OR projection->'canonical_target_endpoint'->>'role' <> 'target'
                       OR (projection->'canonical_source_endpoint'->>'mention_local_id') IS DISTINCT FROM
                           (CASE WHEN (projection->>'endpoint_transform') = 'identity'
                                 THEN projection->'claim_snapshot'->>'source_mention_local_id'
                                 ELSE projection->'claim_snapshot'->>'target_mention_local_id' END)
                       OR (projection->'canonical_target_endpoint'->>'mention_local_id') IS DISTINCT FROM
                           (CASE WHEN (projection->>'endpoint_transform') = 'identity'
                                 THEN projection->'claim_snapshot'->>'target_mention_local_id'
                                 ELSE projection->'claim_snapshot'->>'source_mention_local_id' END)
                       OR (projection->'canonical_source_endpoint'->>'entity_type_key') IS DISTINCT FROM
                           (CASE WHEN (projection->>'endpoint_transform') = 'identity'
                                 THEN projection->'endpoint_type_binding'->>'source_type_key'
                                 ELSE projection->'endpoint_type_binding'->>'target_type_key' END)
                       OR (projection->'canonical_target_endpoint'->>'entity_type_key') IS DISTINCT FROM
                           (CASE WHEN (projection->>'endpoint_transform') = 'identity'
                                 THEN projection->'endpoint_type_binding'->>'target_type_key'
                                 ELSE projection->'endpoint_type_binding'->>'source_type_key' END)
                       OR (projection->'source_endpoint_resolution'->>'mention_surface_sha256') IS DISTINCT FROM
                           graph_mapping_text_sha256(claim_row.source_mention->>'surface')
                       OR (projection->'target_endpoint_resolution'->>'mention_surface_sha256') IS DISTINCT FROM
                           graph_mapping_text_sha256(claim_row.target_mention->>'surface')
                       OR (projection->'canonical_source_endpoint'->'resolution_attestation') IS DISTINCT FROM
                           (CASE WHEN (projection->>'endpoint_transform') = 'identity'
                                 THEN projection->'source_endpoint_resolution'
                                 ELSE projection->'target_endpoint_resolution' END)
                       OR (projection->'canonical_target_endpoint'->'resolution_attestation') IS DISTINCT FROM
                          (CASE WHEN (projection->>'endpoint_transform') = 'identity'
                                THEN projection->'target_endpoint_resolution'
                                ELSE projection->'source_endpoint_resolution' END)
                       OR projection->'canonical_source_endpoint'->'entity_link' IS NULL
                       OR projection->'canonical_target_endpoint'->'entity_link' IS NULL
                       OR (projection->'canonical_source_endpoint'->'entity_link'->>'mention_local_id') IS DISTINCT FROM
                          (projection->'canonical_source_endpoint'->>'mention_local_id')
                       OR (projection->'canonical_target_endpoint'->'entity_link'->>'mention_local_id') IS DISTINCT FROM
                          (projection->'canonical_target_endpoint'->>'mention_local_id')
                       OR projection->'canonical_source_endpoint'->'entity_link'->>'status' NOT IN ('resolved','candidate')
                       OR projection->'canonical_target_endpoint'->'entity_link'->>'status' NOT IN ('resolved','candidate') THEN
                         RAISE EXCEPTION 'mapped canonical endpoint or authorization projection is invalid';
                    END IF;
                    IF (projection->'canonical_source_endpoint'->>'endpoint_fingerprint') IS DISTINCT FROM
                           graph_mapping_json_sha256(
                               (projection->'canonical_source_endpoint') - 'endpoint_fingerprint'::text
                           )
                       OR (projection->'canonical_target_endpoint'->>'endpoint_fingerprint') IS DISTINCT FROM
                           graph_mapping_json_sha256(
                               (projection->'canonical_target_endpoint') - 'endpoint_fingerprint'::text
                           )
                       OR (projection->'canonical_source_endpoint'->'entity_link') IS DISTINCT FROM
                           (projection->'canonical_source_endpoint'->'resolution_attestation'->'entity_link')
                       OR (projection->'canonical_target_endpoint'->'entity_link') IS DISTINCT FROM
                           (projection->'canonical_target_endpoint'->'resolution_attestation'->'entity_link') THEN
                        RAISE EXCEPTION 'mapped canonical endpoint identity is not bound';
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1
                          FROM jsonb_array_elements(projection->'frozen_ontology'->'relation_type_keys') AS relation_key
                         WHERE (relation_key #>> '{}') = (projection->>'canonical_relation_key')
                    ) OR NOT EXISTS (
                        SELECT 1
                          FROM jsonb_array_elements(projection->'frozen_ontology'->'constraints') AS constraint_row
                         WHERE (constraint_row->>'relation_key') = (projection->>'canonical_relation_key')
                           AND (constraint_row->>'source_type_key') =
                               (projection->'canonical_source_endpoint'->>'entity_type_key')
                           AND (constraint_row->>'target_type_key') =
                               (projection->'canonical_target_endpoint'->>'entity_type_key')
                           AND (constraint_row->>'direction') = (projection->>'canonical_direction')
                    ) THEN
                        RAISE EXCEPTION 'mapped canonical relation is not in the frozen ontology';
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1
                          FROM jsonb_array_elements(projection->'authorization_registry_snapshot'->'entries') AS entry
                         WHERE (entry->>'authorization_key') =
                               (projection->'authorization_provenance'->>'authorization_key')
                           AND (entry->>'authorization_version') =
                               (projection->'authorization_provenance'->>'authorization_version')
                           AND (entry->>'surface_predicate_sha256') =
                               (projection->'authorization_provenance'->>'authorized_surface_predicate_sha256')
                           AND (entry->>'canonical_relation_key_sha256') =
                               (projection->'authorization_provenance'->>'authorized_canonical_relation_key_sha256')
                           AND (entry->'allowed_endpoint_transforms' ? (projection->>'endpoint_transform'))
                           AND (entry->'allowed_predicate_transforms' ? (projection->>'predicate_transform'))
                           AND (entry->'allowed_canonical_directions' ? (projection->>'canonical_direction'))
                           AND (entry->'allowed_source_type_keys' ?
                                (projection->'canonical_source_endpoint'->>'entity_type_key'))
                           AND (entry->'allowed_target_type_keys' ?
                                (projection->'canonical_target_endpoint'->>'entity_type_key'))
                    ) THEN
                        RAISE EXCEPTION 'mapped canonical relation lacks exact registry authorization';
                    END IF;
                    IF ((projection->>'predicate_transform') = 'identity' AND
                        (projection->>'canonical_direction') <> (projection->>'surface_direction'))
                       OR ((projection->>'predicate_transform') = 'inverse' AND
                            (projection->>'canonical_direction') <> (CASE (projection->>'surface_direction')
                                WHEN 'source_to_target' THEN 'target_to_source'
                                WHEN 'target_to_source' THEN 'source_to_target'
                                ELSE '' END))
                       OR ((projection->>'predicate_transform') = 'symmetric' AND
                           (projection->>'canonical_direction') <> 'undirected') THEN
                        RAISE EXCEPTION 'mapped predicate transform does not match direction';
                    END IF;
                ELSE
                    IF (projection->>'canonical_relation_key') IS NOT NULL
                       OR (projection->>'canonical_direction') IS NOT NULL
                       OR COALESCE(jsonb_typeof(projection->'canonical_source_endpoint'), 'null') <> 'null'
                       OR COALESCE(jsonb_typeof(projection->'canonical_target_endpoint'), 'null') <> 'null'
                       OR (projection->>'endpoint_transform') IS NOT NULL
                       OR (projection->>'predicate_transform') IS NOT NULL THEN
                        RAISE EXCEPTION 'non-mapped canonical relation projection is not empty';
                    END IF;
                END IF;
                IF NEW.mapping_schema_version IS DISTINCT FROM (projection->>'schema_version') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: mapping_schema_version';
                END IF;
                IF NEW.mapping_result_id::text IS DISTINCT FROM (projection->>'mapping_result_id') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: mapping_result_id';
                END IF;
                IF NEW.mapping_attempt_id::text IS DISTINCT FROM (projection->>'mapping_attempt_id') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: mapping_attempt_id';
                END IF;
                IF NEW.mapping_attempt_fingerprint IS DISTINCT FROM (projection->>'mapping_attempt_fingerprint') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: mapping_attempt_fingerprint';
                END IF;
                IF NEW.mapping_result_fingerprint IS DISTINCT FROM (projection->>'mapping_result_fingerprint') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: mapping_result_fingerprint';
                END IF;
                IF NEW.mapping_version::text IS DISTINCT FROM (projection->>'mapping_version') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: mapping_version';
                END IF;
                IF NEW.mapping_schema_hash IS DISTINCT FROM (projection->>'mapping_schema_hash') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: mapping_schema_hash';
                END IF;
                IF NEW.canonical_schema_hash IS DISTINCT FROM (projection->>'canonical_schema_hash') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: canonical_schema_hash';
                END IF;
                IF NEW.raw_claim_authority_sha256 IS DISTINCT FROM (projection->>'raw_claim_authority_sha256') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: raw_claim_authority_sha256';
                END IF;
                IF NEW.library_id::text IS DISTINCT FROM (projection->>'library_id') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: library_id';
                END IF;
                IF NEW.document_id::text IS DISTINCT FROM (projection->>'document_id') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: document_id';
                END IF;
                IF NEW.document_revision_id::text IS DISTINCT FROM (projection->>'document_revision_id') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: document_revision_id';
                END IF;
                IF NEW.revision_no::text IS DISTINCT FROM (projection->>'revision_no') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: revision_no';
                END IF;
                IF NEW.job_id::text IS DISTINCT FROM (projection->>'job_id') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: job_id';
                END IF;
                IF NEW.extraction_unit_id::text IS DISTINCT FROM (projection->>'extraction_unit_id') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: extraction_unit_id';
                END IF;
                IF NEW.claim_id::text IS DISTINCT FROM (projection->>'claim_id') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: claim_id';
                END IF;
                IF NEW.claim_content_scoped_fingerprint IS DISTINCT FROM (projection->>'claim_content_scoped_fingerprint') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: claim_content_scoped_fingerprint';
                END IF;
                IF NEW.extraction_occurrence_id::text IS DISTINCT FROM (projection->>'extraction_occurrence_id') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: extraction_occurrence_id';
                END IF;
                IF NEW.extraction_occurrence_fingerprint IS DISTINCT FROM (projection->>'extraction_occurrence_fingerprint') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: extraction_occurrence_fingerprint';
                END IF;
                IF NEW.surface_raw_predicate IS DISTINCT FROM (projection->>'surface_raw_predicate') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: surface_raw_predicate';
                END IF;
                IF NEW.surface_direction IS DISTINCT FROM (projection->>'surface_direction') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: surface_direction';
                END IF;
                IF NEW.ontology_version_id::text IS DISTINCT FROM (projection->>'ontology_version_id') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: ontology_version_id';
                END IF;
                IF NEW.ontology_snapshot_hash IS DISTINCT FROM (projection->>'ontology_snapshot_hash') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: ontology_snapshot_hash';
                END IF;
                IF NEW.ontology_contract_version IS DISTINCT FROM (projection->>'ontology_contract_version') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: ontology_contract_version';
                END IF;
                IF NEW.decision_id::text IS DISTINCT FROM (projection->>'decision_id') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: decision_id';
                END IF;
                IF NEW.decision_fingerprint IS DISTINCT FROM (projection->>'decision_fingerprint') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: decision_fingerprint';
                END IF;
                IF NEW.decision_kind IS DISTINCT FROM (projection->>'decision_kind') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: decision_kind';
                END IF;
                IF NEW.outcome IS DISTINCT FROM (projection->>'outcome') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: outcome';
                END IF;
                IF NEW.reason_code IS DISTINCT FROM (projection->>'reason_code') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: reason_code';
                END IF;
                IF NEW.semantic_status IS DISTINCT FROM (projection->>'semantic_status') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: semantic_status';
                END IF;
                IF NEW.canonical_relation_key IS DISTINCT FROM (projection->>'canonical_relation_key') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: canonical_relation_key';
                END IF;
                IF NEW.canonical_direction IS DISTINCT FROM (projection->>'canonical_direction') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: canonical_direction';
                END IF;
                IF NEW.endpoint_transform IS DISTINCT FROM (projection->>'endpoint_transform') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: endpoint_transform';
                END IF;
                IF NEW.predicate_transform IS DISTINCT FROM (projection->>'predicate_transform') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: predicate_transform';
                END IF;
                IF NEW.mapping_confidence IS DISTINCT FROM ((projection->>'mapping_confidence')::double precision) THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: mapping_confidence';
                END IF;
                IF NEW.evidence_bindings IS DISTINCT FROM (projection->'evidence_bindings') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: evidence_bindings';
                END IF;
                IF NEW.source_evidence_ref_ids IS DISTINCT FROM (projection->'source_evidence_ref_ids') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: source_evidence_ref_ids';
                END IF;
                IF NEW.target_evidence_ref_ids IS DISTINCT FROM (projection->'target_evidence_ref_ids') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: target_evidence_ref_ids';
                END IF;
                IF NEW.mapping_evidence_ref_ids IS DISTINCT FROM (projection->'mapping_evidence_ref_ids') THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: mapping_evidence_ref_ids';
                END IF;
                IF NEW.created_at IS DISTINCT FROM (projection->>'created_at')::timestamptz THEN
                    RAISE EXCEPTION 'canonical mapping scalar projection conflicts: created_at';
                END IF;
                IF remap IS NULL OR jsonb_typeof(remap) = 'null' THEN
                    IF NEW.remap_generation <> 0
                       OR NEW.supersedes_mapping_result_id IS NOT NULL
                       OR NEW.supersedes_mapping_result_fingerprint IS NOT NULL
                       OR NEW.lineage_root_mapping_result_id IS NOT NULL
                       OR NEW.lineage_root_mapping_result_fingerprint IS NOT NULL THEN
                        RAISE EXCEPTION 'canonical mapping remap scalar fields are inconsistent';
                    END IF;
                ELSE
                    IF jsonb_typeof(remap) <> 'object'
                       OR ARRAY(
                           SELECT key
                             FROM jsonb_object_keys(remap) AS object_key(key)
                            ORDER BY key
                       ) <> ARRAY[
                           'lineage_root_mapping_result_fingerprint',
                           'lineage_root_mapping_result_id',
                           'prior_remap_generation',
                           'reason_code',
                           'remap_fingerprint',
                           'remap_generation',
                           'remap_version',
                           'supersedes_actor_precedence',
                           'supersedes_lineage_root_mapping_result_fingerprint',
                           'supersedes_lineage_root_mapping_result_id',
                           'supersedes_mapping_result_fingerprint',
                           'supersedes_mapping_result_id',
                           'supersedes_scope',
                           'supersedes_source_precedence'
                       ]::text[]
                       OR remap->>'reason_code' NOT IN
                          ('mapper_refresh', 'human_correction', 'evidence_correction')
                       OR remap->>'remap_generation' IS NULL
                       OR (remap->>'remap_generation')::integer <> NEW.remap_generation
                       OR (remap->>'remap_version') IS DISTINCT FROM NEW.remap_generation::text
                       OR (remap->>'remap_fingerprint') IS DISTINCT FROM
                          graph_mapping_json_sha256((remap) - 'remap_fingerprint'::text) THEN
                        RAISE EXCEPTION 'canonical mapping remap envelope is invalid';
                    END IF;
                    IF NEW.remap_generation = 0 THEN
                        IF NEW.supersedes_mapping_result_id IS NOT NULL
                           OR NEW.supersedes_mapping_result_fingerprint IS NOT NULL
                           OR NEW.lineage_root_mapping_result_id IS NOT NULL
                           OR NEW.lineage_root_mapping_result_fingerprint IS NOT NULL
                           OR remap->>'supersedes_mapping_result_id' IS NOT NULL
                           OR remap->>'supersedes_mapping_result_fingerprint' IS NOT NULL
                           OR remap->>'supersedes_scope' IS NOT NULL
                           OR remap->>'prior_remap_generation' IS NOT NULL
                           OR remap->>'lineage_root_mapping_result_id' IS NOT NULL
                           OR remap->>'lineage_root_mapping_result_fingerprint' IS NOT NULL
                           OR remap->>'supersedes_lineage_root_mapping_result_id' IS NOT NULL
                           OR remap->>'supersedes_lineage_root_mapping_result_fingerprint' IS NOT NULL
                           OR remap->>'supersedes_source_precedence' IS NOT NULL
                           OR remap->>'supersedes_actor_precedence' IS NOT NULL THEN
                            RAISE EXCEPTION 'generation zero cannot carry remap predecessor authority';
                        END IF;
                    ELSE
                        IF NEW.supersedes_mapping_result_id IS NULL
                           OR NEW.supersedes_mapping_result_fingerprint IS NULL
                           OR NEW.lineage_root_mapping_result_id IS NULL
                           OR NEW.lineage_root_mapping_result_fingerprint IS NULL
                           OR NEW.supersedes_mapping_result_id::text IS DISTINCT FROM (remap->>'supersedes_mapping_result_id')
                           OR NEW.supersedes_mapping_result_fingerprint IS DISTINCT FROM (remap->>'supersedes_mapping_result_fingerprint')
                           OR NEW.lineage_root_mapping_result_id::text IS DISTINCT FROM (remap->>'lineage_root_mapping_result_id')
                           OR NEW.lineage_root_mapping_result_fingerprint IS DISTINCT FROM (remap->>'lineage_root_mapping_result_fingerprint')
                        THEN
                            RAISE EXCEPTION 'positive remap projection is incomplete or inconsistent';
                        END IF;
                        SELECT *
                          INTO predecessor_row
                          FROM graph_claim_mappings
                         WHERE mapping_result_id = NEW.supersedes_mapping_result_id
                           AND mapping_result_fingerprint = NEW.supersedes_mapping_result_fingerprint
                           AND library_id = NEW.library_id
                           AND document_id = NEW.document_id
                           AND document_revision_id = NEW.document_revision_id
                           AND revision_no = NEW.revision_no
                           AND job_id = NEW.job_id
                           AND extraction_unit_id = NEW.extraction_unit_id
                           AND claim_id = NEW.claim_id
                           AND extraction_occurrence_id = NEW.extraction_occurrence_id
                         FOR UPDATE;
                        IF predecessor_row IS NULL THEN
                            RAISE EXCEPTION 'positive remap predecessor is missing or out of scope';
                        END IF;
                        IF NEW.remap_generation <> predecessor_row.remap_generation + 1
                           OR (remap->>'prior_remap_generation')::integer <> predecessor_row.remap_generation
                           OR NEW.authority_id IS DISTINCT FROM predecessor_row.authority_id
                           OR NEW.authority_fingerprint IS DISTINCT FROM predecessor_row.authority_fingerprint
                           OR NEW.mapping_schema_hash IS DISTINCT FROM predecessor_row.mapping_schema_hash
                           OR NEW.canonical_schema_hash IS DISTINCT FROM predecessor_row.canonical_schema_hash
                           OR NEW.claim_content_scoped_fingerprint IS DISTINCT FROM predecessor_row.claim_content_scoped_fingerprint
                           OR NEW.extraction_occurrence_fingerprint IS DISTINCT FROM predecessor_row.extraction_occurrence_fingerprint
                           OR NEW.ontology_version_id IS DISTINCT FROM predecessor_row.ontology_version_id
                           OR NEW.ontology_snapshot_hash IS DISTINCT FROM predecessor_row.ontology_snapshot_hash
                           OR NEW.ontology_contract_version IS DISTINCT FROM predecessor_row.ontology_contract_version
                           OR (projection->'authorization_registry_snapshot') IS DISTINCT FROM
                              (predecessor_row.result_projection->'authorization_registry_snapshot')
                           OR (projection->'decision_binding') IS DISTINCT FROM
                              (predecessor_row.result_projection->'decision_binding')
                           OR (projection->>'decision_id') IS DISTINCT FROM
                              (predecessor_row.result_projection->>'decision_id')
                           OR (projection->>'decision_fingerprint') IS DISTINCT FROM
                              (predecessor_row.result_projection->>'decision_fingerprint')
                           OR (projection->>'decision_kind') IS DISTINCT FROM
                              (predecessor_row.result_projection->>'decision_kind')
                           OR (remap->>'supersedes_source_precedence') IS DISTINCT FROM
                              (predecessor_row.result_projection->'source_provenance'->>'source_precedence')
                           OR (remap->>'supersedes_actor_precedence') IS DISTINCT FROM
                              (predecessor_row.result_projection->'actor_provenance'->>'actor_precedence')
                           OR (projection->'source_provenance'->>'source_precedence')::integer <
                              (predecessor_row.result_projection->'source_provenance'->>'source_precedence')::integer
                           OR (projection->'actor_provenance'->>'actor_precedence')::integer <
                              (predecessor_row.result_projection->'actor_provenance'->>'actor_precedence')::integer THEN
                            RAISE EXCEPTION 'positive remap predecessor continuity is invalid';
                        END IF;
                        expected_root_id := COALESCE(
                            predecessor_row.lineage_root_mapping_result_id,
                            predecessor_row.mapping_result_id
                        );
                        expected_root_fingerprint := COALESCE(
                            predecessor_row.lineage_root_mapping_result_fingerprint,
                            predecessor_row.mapping_result_fingerprint
                        );
                        IF NEW.lineage_root_mapping_result_id IS DISTINCT FROM expected_root_id
                           OR NEW.lineage_root_mapping_result_fingerprint IS DISTINCT FROM expected_root_fingerprint
                           OR (remap->>'supersedes_lineage_root_mapping_result_id') IS DISTINCT FROM expected_root_id::text
                           OR (remap->>'supersedes_lineage_root_mapping_result_fingerprint') IS DISTINCT FROM expected_root_fingerprint
                           OR (remap->'supersedes_scope') IS DISTINCT FROM jsonb_build_object(
                               'library_id', predecessor_row.library_id::text,
                               'document_id', predecessor_row.document_id::text,
                               'document_revision_id', predecessor_row.document_revision_id::text,
                               'revision_no', predecessor_row.revision_no,
                               'job_id', predecessor_row.job_id::text,
                               'extraction_unit_id', predecessor_row.extraction_unit_id::text,
                               'claim_id', predecessor_row.claim_id::text,
                               'extraction_occurrence_id', predecessor_row.extraction_occurrence_id::text
                           ) THEN
                            RAISE EXCEPTION 'positive remap predecessor scope or lineage is invalid';
                        END IF;
                    END IF;
                    expected_remap := jsonb_build_object(
                        'remap_generation', NEW.remap_generation,
                        'remap_version', NEW.remap_generation,
                        'supersedes_mapping_result_id', CASE
                            WHEN NEW.remap_generation = 0 THEN NULL
                            ELSE NEW.supersedes_mapping_result_id::text END,
                        'supersedes_mapping_result_fingerprint', CASE
                            WHEN NEW.remap_generation = 0 THEN NULL
                            ELSE NEW.supersedes_mapping_result_fingerprint END,
                        'supersedes_scope', CASE
                            WHEN NEW.remap_generation = 0 THEN NULL
                            ELSE jsonb_build_object(
                                'library_id', predecessor_row.library_id::text,
                                'document_id', predecessor_row.document_id::text,
                                'document_revision_id', predecessor_row.document_revision_id::text,
                                'revision_no', predecessor_row.revision_no,
                                'job_id', predecessor_row.job_id::text,
                                'extraction_unit_id', predecessor_row.extraction_unit_id::text,
                                'claim_id', predecessor_row.claim_id::text,
                                'extraction_occurrence_id', predecessor_row.extraction_occurrence_id::text
                            ) END,
                        'prior_remap_generation', CASE
                            WHEN NEW.remap_generation = 0 THEN NULL
                            ELSE predecessor_row.remap_generation END,
                        'lineage_root_mapping_result_id', CASE
                            WHEN NEW.remap_generation = 0 THEN NULL
                            ELSE expected_root_id::text END,
                        'lineage_root_mapping_result_fingerprint', CASE
                            WHEN NEW.remap_generation = 0 THEN NULL
                            ELSE expected_root_fingerprint END,
                        'supersedes_lineage_root_mapping_result_id', CASE
                            WHEN NEW.remap_generation = 0 THEN NULL
                            ELSE expected_root_id::text END,
                        'supersedes_lineage_root_mapping_result_fingerprint', CASE
                            WHEN NEW.remap_generation = 0 THEN NULL
                            ELSE expected_root_fingerprint END,
                        'supersedes_source_precedence', CASE
                            WHEN NEW.remap_generation = 0 THEN NULL
                            ELSE ((predecessor_row.result_projection->'source_provenance'->>'source_precedence')::integer) END,
                        'supersedes_actor_precedence', CASE
                            WHEN NEW.remap_generation = 0 THEN NULL
                            ELSE ((predecessor_row.result_projection->'actor_provenance'->>'actor_precedence')::integer) END,
                        'reason_code', remap->>'reason_code'
                    );
                    expected_remap := expected_remap || jsonb_build_object(
                        'remap_fingerprint', graph_mapping_json_sha256(
                            expected_remap
                        )
                    );
                    IF remap IS DISTINCT FROM expected_remap THEN
                        RAISE EXCEPTION 'canonical mapping remap envelope is not recomputed from the predecessor';
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER graph_claim_mappings_insert_guard
            BEFORE INSERT ON graph_claim_mappings
            FOR EACH ROW EXECUTE FUNCTION graph_claim_mappings_insert_guard()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION graph_claim_mappings_immutable_guard()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'canonical mapping result rows are immutable';
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER graph_claim_mappings_immutable_guard
            BEFORE UPDATE OR DELETE ON graph_claim_mappings
            FOR EACH ROW EXECUTE FUNCTION graph_claim_mappings_immutable_guard()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION graph_mapping_authority_truncate_guard()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'mapping authority snapshot rows are immutable';
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER graph_mapping_authority_truncate_guard
            BEFORE TRUNCATE ON graph_mapping_authority_snapshots
            FOR EACH STATEMENT EXECUTE FUNCTION graph_mapping_authority_truncate_guard()
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE FUNCTION graph_claim_mappings_truncate_guard()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'canonical mapping result rows are immutable';
            END;
            $$
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER graph_claim_mappings_truncate_guard
            BEFORE TRUNCATE ON graph_claim_mappings
            FOR EACH STATEMENT EXECUTE FUNCTION graph_claim_mappings_truncate_guard()
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS graph_extraction_jobs_scope_guard "
            "ON graph_extraction_jobs"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_extraction_jobs_scope_guard()"))
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS graph_extraction_jobs_ontology_snapshot_immutable_guard "
            "ON graph_extraction_jobs"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_extraction_jobs_ontology_snapshot_immutable_guard()"))
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS graph_claim_mappings_truncate_guard "
            "ON graph_claim_mappings"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_claim_mappings_truncate_guard()"))
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS graph_mapping_authority_truncate_guard "
            "ON graph_mapping_authority_snapshots"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_authority_truncate_guard()"))
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS graph_mapping_authority_insert_guard "
            "ON graph_mapping_authority_snapshots"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_authority_insert_guard()"))
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS graph_mapping_authority_immutable_guard "
            "ON graph_mapping_authority_snapshots"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_authority_immutable_guard()"))
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS graph_claim_mappings_insert_guard "
            "ON graph_claim_mappings"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_claim_mappings_insert_guard()"))
    op.execute(
        sa.text(
            "DROP TRIGGER IF EXISTS graph_claim_mappings_immutable_guard "
            "ON graph_claim_mappings"
        )
    )
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_claim_mappings_immutable_guard()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_claim_snapshot(jsonb,jsonb)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_semantic_projection(jsonb,jsonb)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_qualifier_identity_hashes(jsonb,jsonb)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_evidence_snapshots(jsonb)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_semantic_identity_hashes(jsonb,jsonb)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_semantic_evidence_bound(jsonb,jsonb)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_semantic_ref_ids(jsonb)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_semantic_qualifiers(jsonb,jsonb)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_semantic_value(jsonb,jsonb)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_evidence_identity(jsonb,text)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_validate_evidence_reference(jsonb)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_validate_text_span(jsonb)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_json_number(jsonb)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_json_integer(jsonb)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_evidence_bindings_attempt(jsonb)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_evidence_bindings_identity(jsonb)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_restore_qualifiers(jsonb,jsonb)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_restore_evidence_ref(jsonb,jsonb)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_validate_timestamp_text(jsonb)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_json_exact_keys(jsonb,text[])"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_timestamp_text(timestamptz)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_decision_fingerprint(text,integer,uuid,uuid,uuid,integer,uuid,uuid,text,text,text,text,text,text,jsonb)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_authority_fingerprint(text,jsonb,text,text,jsonb,text,uuid,text,text,text,text)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_uuid5(uuid,text)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_text_sha256(text)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_json_sha256(jsonb)"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS graph_mapping_canonical_json(jsonb)"))
    op.drop_index(
        "ix_graph_claim_mappings_scope_occurrence",
        table_name="graph_claim_mappings",
    )
    op.drop_index(
        "ix_graph_claim_mappings_scope_claim",
        table_name="graph_claim_mappings",
    )
    op.drop_index(
        "ix_graph_claim_mappings_scope_created",
        table_name="graph_claim_mappings",
    )
    op.drop_table("graph_claim_mappings")
    op.drop_index(
        "ix_graph_mapping_authority_scope",
        table_name="graph_mapping_authority_snapshots",
    )
    op.drop_table("graph_mapping_authority_snapshots")
