"""Remove automatic semantic readiness from 0066 predicate scaffolds."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0067"
down_revision: Union[str, None] = "0066"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE stable_predicate_identities AS predicate
            SET resolution_status = 'pending'
            FROM stable_predicate_mappings AS mapping
            WHERE mapping.stable_predicate_identity_id = predicate.id
              AND mapping.library_id = predicate.library_id
              AND mapping.mapping_status = 'active'
              AND predicate.namespace = 'legacy.relation_type.' || mapping.relation_type_id::text
              AND predicate.contract_version = 'legacy_v1'
              AND predicate.identity_policy_version = 'legacy_v1'
              AND predicate.temporal_class = 'state_fact'
              AND predicate.resolution_status = 'resolved'
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE stable_predicate_identities AS predicate
            SET resolution_status = 'resolved'
            FROM stable_predicate_mappings AS mapping
            WHERE mapping.stable_predicate_identity_id = predicate.id
              AND mapping.library_id = predicate.library_id
              AND mapping.mapping_status = 'active'
              AND predicate.namespace = 'legacy.relation_type.' || mapping.relation_type_id::text
              AND predicate.contract_version = 'legacy_v1'
              AND predicate.identity_policy_version = 'legacy_v1'
              AND predicate.temporal_class = 'state_fact'
              AND predicate.resolution_status = 'pending'
            """
        )
    )
