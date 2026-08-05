"""Enable default document classification for existing libraries.

Revision ID: 0048
Revises: 0047
Create Date: 2026-07-31
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0048"
down_revision: Union[str, None] = "0047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE sys_libraries
            SET classification_auto_enabled = true,
                classification_external_model_enabled = true,
                classification_allowed_security_levels = '["internal"]'::jsonb
            WHERE deleted_at IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO classification_taxonomies (
                id, organization_id, version_key, version_no, status,
                description, activated_at, created_at, updated_at
            )
            SELECT
                md5(o.id::text || chr(58) || 'general-enterprise' || chr(58) || 'v1')::uuid,
                o.id,
                'general-enterprise',
                1,
                'active',
                'Default enterprise document categories.',
                now(),
                now(),
                now()
            FROM sys_organizations AS o
            WHERE NOT EXISTS (
                SELECT 1
                FROM classification_taxonomies AS existing
                WHERE existing.organization_id = o.id
            )
            ON CONFLICT DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            r"""
            WITH defaults(key, label, description, sort_order) AS (
                VALUES
                    ('governance-policy', U&'\5236\5EA6\4E0E\6CBB\7406',
                     'Policies, rules, procedures, and governance.', 10),
                    ('contracts-legal', U&'\5408\540C\4E0E\6CD5\52A1',
                     'Contracts, legal opinions, agreements, and disputes.', 20),
                    ('finance-tax', U&'\8D22\52A1\4E0E\7A0E\52A1',
                     'Finance, invoices, tax, payments, and accounting.', 30),
                    ('human-resources', U&'\4EBA\529B\8D44\6E90',
                     'Employment, payroll, benefits, and personnel.', 40),
                    ('projects-operations', U&'\9879\76EE\4E0E\8FD0\8425',
                     'Project delivery, procurement, and operations.', 50),
                    ('safety-compliance', U&'\5B89\5168\4E0E\5408\89C4',
                     'Safety, inspections, compliance, and risk.', 60),
                    ('technology-product', U&'\6280\672F\4E0E\4EA7\54C1',
                     'Technical designs, systems, data, and products.', 70),
                    ('reference-other', U&'\53C2\8003\4E0E\5176\4ED6',
                     'Reference information and other materials.', 80)
            ),
            taxonomies AS (
                SELECT id
                FROM classification_taxonomies
                WHERE version_key = 'general-enterprise'
                  AND version_no = 1
                  AND status = 'active'
            )
            INSERT INTO classification_labels (
                id, taxonomy_version_id, key, label, description,
                sort_order, status, created_at, updated_at
            )
            SELECT
                md5(t.id::text || chr(58) || d.key)::uuid,
                t.id,
                d.key,
                d.label,
                d.description,
                d.sort_order,
                'active',
                now(),
                now()
            FROM taxonomies AS t
            CROSS JOIN defaults AS d
            ON CONFLICT (taxonomy_version_id, key) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO library_classification_labels (
                id, library_id, taxonomy_version_id, label_id,
                ordinal, created_at, updated_at
            )
            SELECT
                md5(lib.id::text || chr(58) || label.id::text)::uuid,
                lib.id,
                taxonomy.id,
                label.id,
                ((row_number() OVER (
                    PARTITION BY lib.id
                    ORDER BY label.sort_order, label.key
                )) - 1)::integer,
                now(),
                now()
            FROM sys_libraries AS lib
            JOIN classification_taxonomies AS taxonomy
              ON taxonomy.organization_id = lib.organization_id
             AND taxonomy.status = 'active'
            JOIN classification_labels AS label
              ON label.taxonomy_version_id = taxonomy.id
             AND label.status = 'active'
            WHERE lib.deleted_at IS NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM library_classification_labels AS existing
                  WHERE existing.library_id = lib.id
              )
            ON CONFLICT DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE sys_libraries
            SET classification_auto_enabled = false,
                classification_external_model_enabled = false,
                classification_allowed_security_levels = '[]'::jsonb
            WHERE deleted_at IS NULL
            """
        )
    )
