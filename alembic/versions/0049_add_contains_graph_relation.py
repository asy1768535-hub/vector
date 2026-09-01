"""Add an evidence-backed contains relation to enterprise ontologies.

Revision ID: 0049
Revises: 0048
Create Date: 2026-08-03
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0049"
down_revision: Union[str, None] = "0048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO relation_types (
                id, library_id, ontology_version_id, key, label, description,
                direction, requires_evidence, default_review_policy,
                properties_schema, is_seeded, status, created_at, updated_at
            )
            SELECT
                md5(ov.id::text || chr(58) || 'contains')::uuid,
                ov.library_id,
                ov.id,
                'contains',
                'Contains',
                'A page, document, product, or project explicitly contains a component, artifact, or process.',
                'directed',
                true,
                'auto_active',
                NULL,
                true,
                'active',
                now(),
                now()
            FROM ontology_versions AS ov
            WHERE ov.status = 'active'
            ON CONFLICT (library_id, ontology_version_id, key) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            WITH endpoint_pairs(source_key, target_key) AS (
                VALUES
                    ('document', 'document'),
                    ('document', 'process'),
                    ('document', 'product'),
                    ('product', 'document'),
                    ('product', 'process'),
                    ('product', 'product'),
                    ('project', 'document'),
                    ('project', 'process'),
                    ('project', 'product')
            )
            INSERT INTO relation_type_constraints (
                id, library_id, ontology_version_id, relation_type_id,
                source_entity_type_id, target_entity_type_id, cardinality,
                requires_review, status, created_at, updated_at
            )
            SELECT
                md5(rt.id::text || chr(58) || source_type.id::text || chr(58) || target_type.id::text)::uuid,
                rt.library_id,
                rt.ontology_version_id,
                rt.id,
                source_type.id,
                target_type.id,
                'one_to_many',
                false,
                'active',
                now(),
                now()
            FROM relation_types AS rt
            JOIN endpoint_pairs AS pair ON true
            JOIN entity_types AS source_type
              ON source_type.library_id = rt.library_id
             AND source_type.ontology_version_id = rt.ontology_version_id
             AND source_type.key = pair.source_key
             AND source_type.status = 'active'
            JOIN entity_types AS target_type
              ON target_type.library_id = rt.library_id
             AND target_type.ontology_version_id = rt.ontology_version_id
             AND target_type.key = pair.target_key
             AND target_type.status = 'active'
            WHERE rt.key = 'contains' AND rt.status = 'active'
            ON CONFLICT (
                library_id, ontology_version_id, relation_type_id,
                source_entity_type_id, target_entity_type_id
            ) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            DELETE FROM relation_types
            WHERE key = 'contains'
              AND is_seeded = true
              AND NOT EXISTS (
                SELECT 1 FROM knowledge_relations
                WHERE knowledge_relations.relation_type_id = relation_types.id
              )
            """
        )
    )
