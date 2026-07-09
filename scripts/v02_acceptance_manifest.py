from __future__ import annotations

import json
from typing import Any


def build_manifest() -> dict[str, Any]:
    return {
        "version": "v0.2",
        "phase": "M5",
        "compatibility_fields": [
            "documents.current_revision",
            "embedding_jobs.document_revision",
            "Qdrant payload.document_revision",
        ],
        "gates": [
            {
                "id": "old_tests",
                "command": (
                    "..\\..\\.venv\\Scripts\\python.exe -m pytest tests -q "
                    '-k "not v02" -p no:cacheprovider'
                ),
                "operator_steps": (
                    "Run the full non-v0.2 regression suite from the M5 worktree. "
                    "The separate Dify gate below keeps the old /retrieval compatibility slice explicit."
                ),
            },
            {
                "id": "new_v02_tests",
                "command": (
                    "..\\..\\.venv\\Scripts\\python.exe -m pytest "
                    "tests/test_v02_m1_schema_models.py "
                    "tests/test_v02_m1_backfill.py "
                    "tests/test_v02_m2_write_path.py "
                    "tests/test_v02_m3_worker_publication.py "
                    "tests/test_v02_m4_folders_sync_evidence.py "
                    "tests/test_v02_m5_acceptance_closure.py "
                    "-q -p no:cacheprovider"
                ),
                "operator_steps": "Run all v0.2 focused tests before release.",
            },
            {
                "id": "real_postgresql_qdrant_acceptance",
                "command": (
                    "$env:VECTOR_KB_PG_TEST_DSN='<disposable-pg-dsn>'; "
                    "$env:VECTOR_KB_QDRANT_TEST_URL='<qdrant-url>'; "
                    "..\\..\\.venv\\Scripts\\python.exe -m pytest "
                    "tests/test_v02_m1_backfill_pg_integration.py "
                    "tests/test_v02_m5_real_stack_acceptance.py -q -p no:cacheprovider"
                ),
                "operator_steps": (
                    "Use disposable PostgreSQL and Qdrant services only; never point this gate at production. "
                    "Run the env-gated PostgreSQL migration/backfill path and Qdrant retrieval/tombstone "
                    "path in the same acceptance window."
                ),
            },
            {
                "id": "dify_retrieval_compatibility",
                "command": (
                    "..\\..\\.venv\\Scripts\\python.exe -m pytest "
                    "tests/test_dify_contract.py "
                    "tests/test_v02_m5_acceptance_closure.py::test_dify_response_shape_keeps_v02_provenance_additive "
                    "-q -p no:cacheprovider"
                ),
                "operator_steps": (
                    "Verify old /retrieval remains Dify-style compatible and v0.2 provenance is additive "
                    "inside record.metadata."
                ),
            },
            {
                "id": "migration_dry_run",
                "command": "alembic upgrade 0018",
                "operator_steps": (
                    "Run alembic upgrade 0018 on a disposable clone; confirm new v0.2 tables and nullable "
                    "columns exist."
                ),
            },
            {
                "id": "rollback_dry_run",
                "command": "alembic downgrade 0017",
                "operator_steps": (
                    "Run alembic downgrade 0017 after the migration dry-run; confirm Alembic current reports "
                    "0017 and v0.2 tables are removed."
                ),
            },
            {
                "id": "v03_evidence_handoff",
                "command": (
                    "..\\..\\.venv\\Scripts\\python.exe -m pytest "
                    "tests/test_v02_m5_acceptance_closure.py::"
                    "test_v03_handoff_tables_bind_to_evidence_id_without_chunk_dependency "
                    "-q -p no:cacheprovider"
                ),
                "operator_steps": (
                    "Verify graph handoff table sketches reference evidence_units.id directly through "
                    "evidence_id."
                ),
            },
        ],
        "v03_handoff_tables": {
            "entity_mentions": {
                "columns": [
                    "id",
                    "library_id",
                    "document_id",
                    "document_revision_id",
                    "evidence_id",
                    "entity_id",
                    "span_start",
                    "span_end",
                ],
                "foreign_keys": {"evidence_id": "evidence_units.id"},
            },
            "relation_evidence": {
                "columns": [
                    "id",
                    "library_id",
                    "relation_id",
                    "evidence_id",
                    "confidence",
                    "created_at",
                ],
                "foreign_keys": {"evidence_id": "evidence_units.id"},
            },
        },
    }


def main() -> None:
    print(json.dumps(build_manifest(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
