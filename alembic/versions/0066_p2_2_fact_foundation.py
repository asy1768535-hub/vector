"""Add P2.2 stable predicate and logical fact foundation."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "0066"
down_revision: Union[str, None] = "0065"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))

    # Composite references use the existing library scope without changing
    # RelationType's primary key semantics.
    op.create_unique_constraint(
        "uq_relation_types_id_library",
        "relation_types",
        ["id", "library_id"],
    )
    op.create_unique_constraint(
        "uq_knowledge_relations_id_library",
        "knowledge_relations",
        ["id", "library_id"],
    )
    op.create_unique_constraint(
        "uq_relation_evidence_id_library",
        "relation_evidence",
        ["id", "library_id"],
    )

    op.create_table(
        "stable_predicate_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("namespace", sa.String(length=128), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("contract_version", sa.String(length=64), nullable=False),
        sa.Column("temporal_class", sa.String(length=32), nullable=False),
        sa.Column("identity_policy_version", sa.String(length=64), nullable=False),
        sa.Column("resolution_status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "temporal_class IN ('static_fact','state_fact','measurement_slot','event_fact')",
            name="ck_stable_predicate_identities_temporal_class",
        ),
        sa.CheckConstraint(
            "resolution_status IN ('resolved','pending','ambiguous','rejected')",
            name="ck_stable_predicate_identities_resolution_status",
        ),
        sa.UniqueConstraint(
            "library_id",
            "namespace",
            "key",
            "contract_version",
            name="uq_stable_predicate_identities_scope_key_version",
        ),
        sa.UniqueConstraint(
            "id",
            "library_id",
            name="uq_stable_predicate_identities_id_library",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["sys_libraries.id"],
            ondelete="RESTRICT",
            name="fk_stable_predicate_identities_library",
        ),
    )
    op.create_index(
        "ix_stable_predicate_identities_library_id",
        "stable_predicate_identities",
        ["library_id"],
    )
    op.create_index(
        "ix_stable_predicate_identities_library_status",
        "stable_predicate_identities",
        ["library_id", "resolution_status"],
    )

    op.create_table(
        "stable_predicate_mappings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stable_predicate_identity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relation_type_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mapping_status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "mapping_status IN ('active','superseded','rejected')",
            name="ck_stable_predicate_mappings_status",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["sys_libraries.id"],
            ondelete="RESTRICT",
            name="fk_stable_predicate_mappings_library",
        ),
        sa.ForeignKeyConstraint(
            ["stable_predicate_identity_id", "library_id"],
            ["stable_predicate_identities.id", "stable_predicate_identities.library_id"],
            ondelete="RESTRICT",
            name="fk_stable_predicate_mappings_identity",
        ),
        sa.ForeignKeyConstraint(
            ["relation_type_id", "library_id"],
            ["relation_types.id", "relation_types.library_id"],
            ondelete="RESTRICT",
            name="fk_stable_predicate_mappings_relation_type",
        ),
    )
    op.create_index(
        "ix_stable_predicate_mappings_library_id",
        "stable_predicate_mappings",
        ["library_id"],
    )
    op.create_index(
        "ix_stable_predicate_mappings_library_identity",
        "stable_predicate_mappings",
        ["library_id", "stable_predicate_identity_id"],
    )
    op.create_index(
        "ix_stable_predicate_mappings_relation_type",
        "stable_predicate_mappings",
        ["library_id", "relation_type_id"],
    )
    op.create_index(
        "uq_stable_predicate_mappings_library_relation_type_active",
        "stable_predicate_mappings",
        ["library_id", "relation_type_id"],
        unique=True,
        postgresql_where=sa.text("mapping_status = 'active'"),
    )

    op.create_table(
        "logical_facts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stable_predicate_identity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_canonical_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("object_kind", sa.String(length=16), nullable=True),
        sa.Column("object_canonical_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("object_value", postgresql.JSONB(), nullable=True),
        sa.Column(
            "identity_qualifiers",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("temporal_identity_key", sa.String(length=128), nullable=True),
        sa.Column("identity_policy_version", sa.String(length=64), nullable=False),
        sa.Column("identity_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "object_kind IS NULL OR object_kind IN ('entity','literal','reference')",
            name="ck_logical_facts_object_kind",
        ),
        sa.CheckConstraint(
            "object_value IS NULL OR jsonb_typeof(object_value) = 'object'",
            name="ck_logical_facts_object_value_json",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(identity_qualifiers) = 'object'",
            name="ck_logical_facts_identity_qualifiers_json",
        ),
        sa.CheckConstraint(
            "identity_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_logical_facts_identity_fingerprint",
        ),
        sa.CheckConstraint(
            "status IN ('active','conflicted','inactive')",
            name="ck_logical_facts_status",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["sys_libraries.id"],
            ondelete="RESTRICT",
            name="fk_logical_facts_library",
        ),
        sa.ForeignKeyConstraint(
            ["stable_predicate_identity_id", "library_id"],
            ["stable_predicate_identities.id", "stable_predicate_identities.library_id"],
            ondelete="RESTRICT",
            name="fk_logical_facts_stable_predicate",
        ),
        sa.ForeignKeyConstraint(
            ["subject_canonical_entity_id", "library_id"],
            ["canonical_entities.id", "canonical_entities.library_id"],
            ondelete="RESTRICT",
            name="fk_logical_facts_subject_canonical",
        ),
        sa.ForeignKeyConstraint(
            ["object_canonical_entity_id", "library_id"],
            ["canonical_entities.id", "canonical_entities.library_id"],
            ondelete="RESTRICT",
            name="fk_logical_facts_object_canonical",
        ),
        sa.UniqueConstraint("id", "library_id", name="uq_logical_facts_id_library"),
    )
    op.create_index("ix_logical_facts_library_id", "logical_facts", ["library_id"])
    op.create_index(
        "ix_logical_facts_library_predicate",
        "logical_facts",
        ["library_id", "stable_predicate_identity_id"],
    )
    op.create_index(
        "ix_logical_facts_library_subject",
        "logical_facts",
        ["library_id", "subject_canonical_entity_id"],
    )
    op.create_index(
        "ix_logical_facts_library_fingerprint",
        "logical_facts",
        ["library_id", "identity_fingerprint"],
    )

    op.create_table(
        "fact_assertions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("logical_fact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("knowledge_relation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assertion_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("asserted_object_kind", sa.String(length=16), nullable=True),
        sa.Column("asserted_object_canonical_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("asserted_value", postgresql.JSONB(), nullable=True),
        sa.Column("polarity", sa.String(length=16), server_default="affirmed", nullable=False),
        sa.Column("modality", sa.String(length=16), server_default="unknown", nullable=False),
        sa.Column("qualifiers", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("valid_time", postgresql.JSONB(), nullable=True),
        sa.Column("effective_time", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("source_kind", sa.String(length=32), server_default="manual", nullable=False),
        sa.Column("raw_claim_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("graph_relation_candidate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "asserted_object_kind IS NULL OR asserted_object_kind IN ('entity','literal','reference')",
            name="ck_fact_assertions_asserted_object_kind",
        ),
        sa.CheckConstraint(
            "asserted_value IS NULL OR jsonb_typeof(asserted_value) = 'object'",
            name="ck_fact_assertions_asserted_value_json",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(qualifiers) = 'object'",
            name="ck_fact_assertions_qualifiers_json",
        ),
        sa.CheckConstraint(
            "polarity IN ('affirmed','negated','unknown')",
            name="ck_fact_assertions_polarity",
        ),
        sa.CheckConstraint(
            "modality IN ('planned','possible','expected','confirmed','completed','unknown')",
            name="ck_fact_assertions_modality",
        ),
        sa.CheckConstraint(
            "status IN ('active','stale','superseded','rejected')",
            name="ck_fact_assertions_status",
        ),
        sa.CheckConstraint(
            "source_kind IN ('raw_claim','graph_relation_candidate','knowledge_relation','manual')",
            name="ck_fact_assertions_source_kind",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_fact_assertions_confidence",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["sys_libraries.id"],
            ondelete="RESTRICT",
            name="fk_fact_assertions_library",
        ),
        sa.ForeignKeyConstraint(
            ["logical_fact_id", "library_id"],
            ["logical_facts.id", "logical_facts.library_id"],
            ondelete="RESTRICT",
            name="fk_fact_assertions_logical_fact",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_relation_id", "library_id"],
            ["knowledge_relations.id", "knowledge_relations.library_id"],
            ondelete="SET NULL",
            name="fk_fact_assertions_knowledge_relation",
        ),
        sa.ForeignKeyConstraint(
            ["asserted_object_canonical_entity_id", "library_id"],
            ["canonical_entities.id", "canonical_entities.library_id"],
            ondelete="SET NULL",
            name="fk_fact_assertions_asserted_object_canonical",
        ),
        sa.ForeignKeyConstraint(
            ["raw_claim_id"],
            ["graph_raw_claims.id"],
            ondelete="SET NULL",
            name="fk_fact_assertions_raw_claim",
        ),
        sa.ForeignKeyConstraint(
            ["graph_relation_candidate_id"],
            ["graph_relation_candidates.id"],
            ondelete="SET NULL",
            name="fk_fact_assertions_relation_candidate",
        ),
        sa.UniqueConstraint("id", "library_id", name="uq_fact_assertions_id_library"),
        sa.UniqueConstraint(
            "library_id",
            "assertion_fingerprint",
            name="uq_fact_assertions_library_fingerprint",
        ),
    )
    op.create_index("ix_fact_assertions_library_id", "fact_assertions", ["library_id"])
    op.create_index(
        "ix_fact_assertions_library_fact_status",
        "fact_assertions",
        ["library_id", "logical_fact_id", "status"],
    )
    op.create_index(
        "ix_fact_assertions_library_relation",
        "fact_assertions",
        ["library_id", "knowledge_relation_id"],
    )

    op.create_table(
        "fact_resolution_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("subject_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("decision_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("graph_relation_candidate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("raw_claim_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("stable_predicate_identity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("logical_fact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("fact_assertion_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_snapshot", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("candidate_snapshot", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("method", sa.String(length=64), server_default="none", nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("resolver_version", sa.String(length=64), server_default="fact_resolution_v1", nullable=False),
        sa.Column("supersedes_decision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "source_kind IN ('raw_claim','graph_relation_candidate','knowledge_relation','manual')",
            name="ck_fact_resolution_decisions_source_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending','resolved','rejected','superseded')",
            name="ck_fact_resolution_decisions_status",
        ),
        sa.CheckConstraint(
            "subject_fingerprint ~ '^[0-9a-f]{64}$' AND decision_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_fact_resolution_decisions_fingerprints",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_fact_resolution_decisions_confidence",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_snapshot) = 'object' AND "
            "jsonb_typeof(candidate_snapshot) = 'object' AND "
            "jsonb_typeof(evidence_refs) = 'array'",
            name="ck_fact_resolution_decisions_snapshot_shapes",
        ),
        sa.ForeignKeyConstraint(
            ["library_id"],
            ["sys_libraries.id"],
            ondelete="RESTRICT",
            name="fk_fact_resolution_decisions_library",
        ),
        sa.ForeignKeyConstraint(
            ["graph_relation_candidate_id"],
            ["graph_relation_candidates.id"],
            ondelete="SET NULL",
            name="fk_fact_resolution_decisions_candidate",
        ),
        sa.ForeignKeyConstraint(
            ["raw_claim_id"],
            ["graph_raw_claims.id"],
            ondelete="SET NULL",
            name="fk_fact_resolution_decisions_raw_claim",
        ),
        sa.ForeignKeyConstraint(
            ["stable_predicate_identity_id", "library_id"],
            ["stable_predicate_identities.id", "stable_predicate_identities.library_id"],
            ondelete="SET NULL",
            name="fk_fact_resolution_decisions_stable_predicate",
        ),
        sa.ForeignKeyConstraint(
            ["logical_fact_id", "library_id"],
            ["logical_facts.id", "logical_facts.library_id"],
            ondelete="SET NULL",
            name="fk_fact_resolution_decisions_logical_fact",
        ),
        sa.ForeignKeyConstraint(
            ["fact_assertion_id", "library_id"],
            ["fact_assertions.id", "fact_assertions.library_id"],
            ondelete="SET NULL",
            name="fk_fact_resolution_decisions_assertion",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_decision_id"],
            ["fact_resolution_decisions.id"],
            ondelete="SET NULL",
            name="fk_fact_resolution_decisions_supersedes",
        ),
        sa.UniqueConstraint(
            "library_id",
            "decision_fingerprint",
            name="uq_fact_resolution_decisions_library_fingerprint",
        ),
    )
    op.create_index("ix_fact_resolution_decisions_library_id", "fact_resolution_decisions", ["library_id"])
    op.create_index(
        "ix_fact_resolution_decisions_library_status",
        "fact_resolution_decisions",
        ["library_id", "status", "created_at"],
    )
    op.create_index(
        "ix_fact_resolution_decisions_library_source",
        "fact_resolution_decisions",
        ["library_id", "source_kind", "created_at"],
    )
    op.create_index(
        "ix_fact_resolution_decisions_library_subject",
        "fact_resolution_decisions",
        ["library_id", "subject_fingerprint"],
    )

    op.add_column(
        "knowledge_relations",
        sa.Column("logical_fact_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_knowledge_relations_logical_fact",
        "knowledge_relations",
        "logical_facts",
        ["logical_fact_id", "library_id"],
        ["id", "library_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_knowledge_relations_logical_fact_id",
        "knowledge_relations",
        ["logical_fact_id"],
    )

    op.add_column(
        "relation_evidence",
        sa.Column("fact_assertion_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_relation_evidence_fact_assertion",
        "relation_evidence",
        "fact_assertions",
        ["fact_assertion_id", "library_id"],
        ["id", "library_id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_relation_evidence_fact_assertion_id",
        "relation_evidence",
        ["fact_assertion_id"],
    )

    # One legacy RelationType receives one private scaffold.  The namespace
    # includes its UUID so equal keys from different ontology versions never
    # collapse during backfill.
    op.execute(
        sa.text(
            """
            CREATE TEMP TABLE _p2_2_relation_type_predicate (
                relation_type_id uuid PRIMARY KEY,
                library_id uuid NOT NULL,
                stable_predicate_identity_id uuid NOT NULL
            ) ON COMMIT DROP
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO _p2_2_relation_type_predicate
                (relation_type_id, library_id, stable_predicate_identity_id)
            SELECT id, library_id, gen_random_uuid()
            FROM relation_types
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO stable_predicate_identities (
                id, library_id, namespace, key, contract_version,
                temporal_class, identity_policy_version, resolution_status,
                created_at, updated_at
            )
            SELECT mapping.stable_predicate_identity_id,
                   relation_type.library_id,
                   'legacy.relation_type.' || relation_type.id::text,
                   relation_type.key,
                   'legacy_v1',
                   'state_fact',
                   'legacy_v1',
                   'resolved',
                   relation_type.created_at,
                   relation_type.updated_at
            FROM relation_types AS relation_type
            JOIN _p2_2_relation_type_predicate AS mapping
              ON mapping.relation_type_id = relation_type.id
             AND mapping.library_id = relation_type.library_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO stable_predicate_mappings (
                id, library_id, stable_predicate_identity_id, relation_type_id,
                mapping_status, created_at
            )
            SELECT gen_random_uuid(), library_id, stable_predicate_identity_id,
                   relation_type_id, 'active', now()
            FROM _p2_2_relation_type_predicate
            """
        )
    )

    # Only relations with both canonical endpoints receive the conservative
    # 1:1 fact/assertion scaffold.  Unresolved legacy rows remain nullable.
    op.execute(
        sa.text(
            """
            CREATE TEMP TABLE _p2_2_relation_fact_backfill (
                relation_id uuid PRIMARY KEY,
                logical_fact_id uuid NOT NULL,
                assertion_id uuid NOT NULL
            ) ON COMMIT DROP
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO _p2_2_relation_fact_backfill
                (relation_id, logical_fact_id, assertion_id)
            SELECT relation.id, gen_random_uuid(), gen_random_uuid()
            FROM knowledge_relations AS relation
            JOIN entities AS source_entity
              ON source_entity.id = relation.source_entity_id
             AND source_entity.library_id = relation.library_id
            JOIN entities AS target_entity
              ON target_entity.id = relation.target_entity_id
             AND target_entity.library_id = relation.library_id
            JOIN stable_predicate_mappings AS mapping
              ON mapping.relation_type_id = relation.relation_type_id
             AND mapping.library_id = relation.library_id
             AND mapping.mapping_status = 'active'
            WHERE source_entity.canonical_entity_id IS NOT NULL
              AND target_entity.canonical_entity_id IS NOT NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO logical_facts (
                id, library_id, stable_predicate_identity_id,
                subject_canonical_entity_id, object_kind,
                object_canonical_entity_id, identity_qualifiers,
                temporal_identity_key, identity_policy_version,
                identity_fingerprint, status, created_at, updated_at
            )
            SELECT backfill.logical_fact_id,
                   relation.library_id,
                   mapping.stable_predicate_identity_id,
                   source_entity.canonical_entity_id,
                   'entity',
                   target_entity.canonical_entity_id,
                   '{}'::jsonb,
                   NULL,
                   predicate.identity_policy_version,
                   encode(digest(convert_to(relation.id::text, 'UTF8'), 'sha256'), 'hex'),
                   CASE WHEN relation.status = 'active' THEN 'active' ELSE 'inactive' END,
                   relation.created_at,
                   relation.updated_at
            FROM _p2_2_relation_fact_backfill AS backfill
            JOIN knowledge_relations AS relation ON relation.id = backfill.relation_id
            JOIN entities AS source_entity
              ON source_entity.id = relation.source_entity_id
             AND source_entity.library_id = relation.library_id
            JOIN entities AS target_entity
              ON target_entity.id = relation.target_entity_id
             AND target_entity.library_id = relation.library_id
            JOIN stable_predicate_mappings AS mapping
              ON mapping.relation_type_id = relation.relation_type_id
             AND mapping.library_id = relation.library_id
             AND mapping.mapping_status = 'active'
            JOIN stable_predicate_identities AS predicate
              ON predicate.id = mapping.stable_predicate_identity_id
             AND predicate.library_id = mapping.library_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO fact_assertions (
                id, library_id, logical_fact_id, knowledge_relation_id,
                assertion_fingerprint, asserted_object_kind,
                asserted_object_canonical_entity_id, asserted_value,
                polarity, modality, qualifiers, valid_time, effective_time,
                status, confidence, source_kind, created_at, updated_at
            )
            SELECT backfill.assertion_id,
                   relation.library_id,
                   backfill.logical_fact_id,
                   relation.id,
                   encode(digest(convert_to(relation.id::text || '-legacy', 'UTF8'), 'sha256'), 'hex'),
                   'entity',
                   target_entity.canonical_entity_id,
                   NULL,
                   'affirmed',
                   'unknown',
                   CASE
                       WHEN jsonb_typeof(relation.properties) = 'object' THEN relation.properties
                       ELSE '{}'::jsonb
                   END,
                   CASE WHEN relation.valid_from IS NULL AND relation.valid_to IS NULL
                        THEN NULL
                        ELSE jsonb_build_object('start', relation.valid_from, 'end', relation.valid_to)
                   END,
                   NULL,
                   CASE
                       WHEN relation.status = 'active' THEN 'active'
                       WHEN relation.status = 'stale' THEN 'stale'
                       ELSE 'rejected'
                   END,
                   relation.confidence,
                   'knowledge_relation',
                   relation.created_at,
                   relation.updated_at
            FROM _p2_2_relation_fact_backfill AS backfill
            JOIN knowledge_relations AS relation ON relation.id = backfill.relation_id
            JOIN entities AS target_entity
              ON target_entity.id = relation.target_entity_id
             AND target_entity.library_id = relation.library_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE knowledge_relations AS relation
            SET logical_fact_id = backfill.logical_fact_id
            FROM _p2_2_relation_fact_backfill AS backfill
            WHERE relation.id = backfill.relation_id
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE relation_evidence AS evidence
            SET fact_assertion_id = backfill.assertion_id
            FROM _p2_2_relation_fact_backfill AS backfill
            WHERE evidence.relation_id = backfill.relation_id
              AND evidence.library_id = (
                  SELECT relation.library_id
                  FROM knowledge_relations AS relation
                  WHERE relation.id = backfill.relation_id
              )
            """
        )
    )


def downgrade() -> None:
    op.drop_index("ix_relation_evidence_fact_assertion_id", table_name="relation_evidence")
    op.drop_constraint(
        "fk_relation_evidence_fact_assertion", "relation_evidence", type_="foreignkey"
    )
    op.drop_column("relation_evidence", "fact_assertion_id")

    op.drop_index("ix_knowledge_relations_logical_fact_id", table_name="knowledge_relations")
    op.drop_constraint(
        "fk_knowledge_relations_logical_fact", "knowledge_relations", type_="foreignkey"
    )
    op.drop_column("knowledge_relations", "logical_fact_id")

    op.drop_index("ix_fact_resolution_decisions_library_subject", table_name="fact_resolution_decisions")
    op.drop_index("ix_fact_resolution_decisions_library_source", table_name="fact_resolution_decisions")
    op.drop_index("ix_fact_resolution_decisions_library_status", table_name="fact_resolution_decisions")
    op.drop_index("ix_fact_resolution_decisions_library_id", table_name="fact_resolution_decisions")
    op.drop_table("fact_resolution_decisions")

    op.drop_index("ix_fact_assertions_library_relation", table_name="fact_assertions")
    op.drop_index("ix_fact_assertions_library_fact_status", table_name="fact_assertions")
    op.drop_index("ix_fact_assertions_library_id", table_name="fact_assertions")
    op.drop_table("fact_assertions")

    op.drop_index("ix_logical_facts_library_fingerprint", table_name="logical_facts")
    op.drop_index("ix_logical_facts_library_subject", table_name="logical_facts")
    op.drop_index("ix_logical_facts_library_predicate", table_name="logical_facts")
    op.drop_index("ix_logical_facts_library_id", table_name="logical_facts")
    op.drop_table("logical_facts")

    op.drop_index(
        "uq_stable_predicate_mappings_library_relation_type_active",
        table_name="stable_predicate_mappings",
    )
    op.drop_index("ix_stable_predicate_mappings_relation_type", table_name="stable_predicate_mappings")
    op.drop_index("ix_stable_predicate_mappings_library_identity", table_name="stable_predicate_mappings")
    op.drop_index("ix_stable_predicate_mappings_library_id", table_name="stable_predicate_mappings")
    op.drop_table("stable_predicate_mappings")

    op.drop_index("ix_stable_predicate_identities_library_status", table_name="stable_predicate_identities")
    op.drop_index("ix_stable_predicate_identities_library_id", table_name="stable_predicate_identities")
    op.drop_table("stable_predicate_identities")

    op.drop_constraint("uq_relation_types_id_library", "relation_types", type_="unique")
    op.drop_constraint("uq_relation_evidence_id_library", "relation_evidence", type_="unique")
    op.drop_constraint("uq_knowledge_relations_id_library", "knowledge_relations", type_="unique")
