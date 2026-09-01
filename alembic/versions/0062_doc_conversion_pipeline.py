"""Add isolated legacy DOC conversion state."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0062"
down_revision: Union[str, None] = "0061"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "document_import_jobs",
        sa.Column("conversion_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "document_import_jobs",
        sa.Column("converter_version", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "document_import_jobs",
        sa.Column(
            "conversion_attempt_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )
    op.drop_constraint(
        "ck_document_import_jobs_stage",
        "document_import_jobs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_document_import_jobs_stage",
        "document_import_jobs",
        "current_stage IN "
        "('uploading','queued','converting','conversion_ready','validating','parsing',"
        "'chunking','embedding','graph','completed')",
    )
    op.create_check_constraint(
        "ck_document_import_jobs_conversion_attempts",
        "document_import_jobs",
        "conversion_attempt_count >= 0",
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE document_import_jobs
        SET status = 'queued', current_stage = 'queued',
            worker_id = NULL, claimed_at = NULL
        WHERE current_stage IN ('converting', 'conversion_ready')
        """
    )
    op.drop_constraint(
        "ck_document_import_jobs_conversion_attempts",
        "document_import_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_document_import_jobs_stage",
        "document_import_jobs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_document_import_jobs_stage",
        "document_import_jobs",
        "current_stage IN "
        "('uploading','queued','validating','parsing','chunking','embedding','graph','completed')",
    )
    op.drop_column("document_import_jobs", "conversion_attempt_count")
    op.drop_column("document_import_jobs", "converter_version")
    op.drop_column("document_import_jobs", "conversion_sha256")
