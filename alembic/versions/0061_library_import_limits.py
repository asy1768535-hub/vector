"""Add configurable per-library import limits."""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0061"
down_revision: Union[str, None] = "0060"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sys_libraries",
        sa.Column("import_max_file_bytes", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "sys_libraries",
        sa.Column("import_max_files_per_selection", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_lib_import_max_file_bytes",
        "sys_libraries",
        "import_max_file_bytes IS NULL OR "
        "import_max_file_bytes BETWEEN 1048576 AND 53687091200",
    )
    op.create_check_constraint(
        "ck_lib_import_max_files_per_selection",
        "sys_libraries",
        "import_max_files_per_selection IS NULL OR "
        "import_max_files_per_selection BETWEEN 1 AND 100000",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_lib_import_max_files_per_selection",
        "sys_libraries",
        type_="check",
    )
    op.drop_constraint(
        "ck_lib_import_max_file_bytes",
        "sys_libraries",
        type_="check",
    )
    op.drop_column("sys_libraries", "import_max_files_per_selection")
    op.drop_column("sys_libraries", "import_max_file_bytes")
