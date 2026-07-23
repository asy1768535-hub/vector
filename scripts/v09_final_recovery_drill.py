"""Run the disposable PostgreSQL/object-storage v0.9 recovery acceptance drill."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

import asyncpg

ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(os.environ.get("VECTOR_KB_ACCEPTANCE_PYTHON", os.sys.executable))
PG_BIN = Path(
    os.environ.get("VECTOR_KB_ACCEPTANCE_PG_BIN", r"D:\Program Files\PostgreSQL\18\bin")
)
HOST = os.environ.get("VECTOR_KB_ACCEPTANCE_PG_HOST", "127.0.0.1")
PORT = int(os.environ.get("VECTOR_KB_ACCEPTANCE_PG_PORT", "5432"))
USER = os.environ.get("VECTOR_KB_ACCEPTANCE_PG_USER", "postgres")
PASSWORD = os.environ.get("VECTOR_KB_ACCEPTANCE_PG_PASSWORD", "")


async def _admin(*statements: str) -> None:
    connection = await asyncpg.connect(
        host=HOST,
        port=PORT,
        user=USER,
        password=PASSWORD,
        database="postgres",
    )
    try:
        for statement in statements:
            await connection.execute(statement)
    finally:
        await connection.close()


async def _seed(database: str, ids: dict[str, uuid.UUID], content: bytes) -> None:
    digest = hashlib.sha256(content).hexdigest()
    zeros = "0" * 64
    ones = "1" * 64
    connection = await asyncpg.connect(
        host=HOST,
        port=PORT,
        user=USER,
        password=PASSWORD,
        database=database,
    )
    try:
        async with connection.transaction():
            await connection.execute(
                """
                INSERT INTO sys_libraries (
                    id, organization_id, slug, name, embedding_model, embedding_dim,
                    vector_distance, chunk_size, chunk_overlap, qdrant_collection
                ) VALUES (
                    $1, '00000000-0000-0000-0000-000000000001',
                    'acceptance-lib', 'Acceptance', 'bge-m3', 4,
                    'cosine', 1000, 120, 'acceptance-qdrant'
                )
                """,
                ids["library"],
            )
            await connection.execute(
                """
                INSERT INTO documents (
                    id, library_id, external_id, title, content_hash,
                    current_revision, status
                ) VALUES ($1, $2, 'doc-1', 'Acceptance document', $3, 1, 'ready')
                """,
                ids["document"],
                ids["library"],
                digest,
            )
            await connection.execute(
                """
                INSERT INTO document_revisions (
                    id, document_id, library_id, revision_no, title, content_hash,
                    normalized_text, parser_name, parser_version, chunking_strategy,
                    chunking_strategy_version, status
                ) VALUES (
                    $1, $2, $3, 1, 'Acceptance document', $4,
                    'sanitized acceptance text', 'acceptance', 'v1', 'fixed', 'v1', 'ready'
                )
                """,
                ids["revision"],
                ids["document"],
                ids["library"],
                digest,
            )
            await connection.execute(
                """
                UPDATE documents
                SET current_revision_id = $1, latest_revision_id = $1
                WHERE id = $2
                """,
                ids["revision"],
                ids["document"],
            )
            await connection.execute(
                """
                INSERT INTO document_revision_files (
                    id, document_revision_id, document_id, library_id, file_name,
                    content_type, storage_path, size_bytes, sha256, storage_provider,
                    endpoint_ref, object_key, immutability_mode, managed_snapshot,
                    lifecycle_status
                ) VALUES (
                    $1, $2, $3, $4, 'acceptance.txt', 'text/plain', $5, $6, $7,
                    'local', 'primary', $8, 'content_hash', true, 'available'
                )
                """,
                ids["file"],
                ids["revision"],
                ids["document"],
                ids["library"],
                f"objects/{digest}",
                len(content),
                digest,
                digest,
            )
            await connection.execute(
                """
                INSERT INTO evidence_units (
                    id, library_id, document_id, document_revision_id, evidence_kind,
                    source_start, source_end, text_quote, text_quote_hash, status
                ) VALUES (
                    $1, $2, $3, $4, 'direct_statement', 0, 9,
                    'sanitized', $5, 'active'
                )
                """,
                ids["evidence"],
                ids["library"],
                ids["document"],
                ids["revision"],
                hashlib.sha256(b"sanitized").hexdigest(),
            )
            await connection.execute(
                """
                INSERT INTO ontology_versions (
                    id, library_id, version_key, version_no, status
                ) VALUES ($1, $2, 'acceptance', 1, 'active')
                """,
                ids["ontology"],
                ids["library"],
            )
            await connection.execute(
                """
                INSERT INTO entity_types (
                    id, library_id, ontology_version_id, key, label, status
                ) VALUES (
                    $1, $2, $3, 'acceptance_type', 'Acceptance type', 'active'
                )
                """,
                ids["entity_type"],
                ids["library"],
                ids["ontology"],
            )
            await connection.execute(
                """
                INSERT INTO entities (
                    id, library_id, ontology_version_id, entity_type_id,
                    canonical_name, normalized_name, status, source_type
                ) VALUES (
                    $1, $2, $3, $4, 'Acceptance entity',
                    'acceptance entity', 'active', 'imported'
                )
                """,
                ids["entity"],
                ids["library"],
                ids["ontology"],
                ids["entity_type"],
            )
            await connection.execute(
                """
                INSERT INTO graph_publications (
                    id, library_id, ontology_version_id, status, source_mode,
                    manifest_version, policy_version, policy_snapshot, manifest_hash,
                    idempotency_key, include_drafts, plan_options, entity_count,
                    relation_count, blocked_counts, blocked_diagnostics,
                    item_hashes_summary
                ) VALUES (
                    $1, $2, $3, 'planned', 'manual_plan', 'v1', 'v1',
                    '{}'::jsonb, $4, 'acceptance-publication', false,
                    '{}'::jsonb, 1, 0, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb
                )
                """,
                ids["publication"],
                ids["library"],
                ids["ontology"],
                zeros,
            )
            await connection.execute(
                """
                INSERT INTO graph_publication_items (
                    id, publication_id, library_id, ontology_version_id, item_kind,
                    entity_id, item_hash, status, support_evidence_ids,
                    support_counts, fact_snapshot
                ) VALUES (
                    $1, $2, $3, $4, 'entity', $5, $6, 'planned',
                    $7::jsonb, '{}'::jsonb, '{}'::jsonb
                )
                """,
                ids["publication_item"],
                ids["publication"],
                ids["library"],
                ids["ontology"],
                ids["entity"],
                ones,
                json.dumps([str(ids["evidence"])]),
            )
            await connection.execute(
                """
                INSERT INTO sync_sources (
                    id, library_id, source_key, display_name, source_type, status
                ) VALUES (
                    $1, $2, 'acceptance-source', 'Acceptance source',
                    'structured', 'active'
                )
                """,
                ids["sync_source"],
                ids["library"],
            )
            await connection.execute(
                """
                INSERT INTO graph_external_sync_operations (
                    id, library_id, sync_source_id, idempotency_key, request_hash,
                    status, item_count, created_count, updated_count, unchanged_count,
                    deleted_count, conflict_count, stale_count, result_payload,
                    finished_at
                ) VALUES (
                    $1, $2, $3, 'acceptance-operation', $4, 'applied',
                    1, 1, 0, 0, 0, 0, 0, '{}'::jsonb, now()
                )
                """,
                ids["operation"],
                ids["library"],
                ids["sync_source"],
                zeros,
            )
            await connection.execute(
                """
                INSERT INTO graph_external_fact_mappings (
                    id, library_id, sync_source_id, fact_kind, external_type,
                    external_id, entity_id, lifecycle, payload_hash, evidence_id,
                    source_locator, last_operation_id
                ) VALUES (
                    $1, $2, $3, 'entity', 'acceptance', 'entity-1', $4,
                    'active', $5, $6, '{}'::jsonb, $7
                )
                """,
                ids["mapping"],
                ids["library"],
                ids["sync_source"],
                ids["entity"],
                ones,
                ids["evidence"],
                ids["operation"],
            )
    finally:
        await connection.close()


async def _verify(database: str, ids: dict[str, uuid.UUID]) -> tuple[dict, dict, dict]:
    connection = await asyncpg.connect(
        host=HOST,
        port=PORT,
        user=USER,
        password=PASSWORD,
        database=database,
    )
    try:
        tables = (
            "documents",
            "document_revisions",
            "document_revision_files",
            "evidence_units",
            "graph_publications",
            "graph_publication_items",
            "graph_external_fact_mappings",
        )
        counts = {
            table: await connection.fetchval(f"SELECT count(*) FROM {table}")  # noqa: S608
            for table in tables
        }
        file_row = dict(
            await connection.fetchrow(
                """
                SELECT sha256, size_bytes, object_key
                FROM document_revision_files WHERE id = $1
                """,
                ids["file"],
            )
        )
        revision_row = dict(
            await connection.fetchrow(
                """
                SELECT current_revision_id, latest_revision_id
                FROM documents WHERE id = $1
                """,
                ids["document"],
            )
        )
        return counts, file_row, revision_row
    finally:
        await connection.close()


def _run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


async def run_drill(qdrant_snapshot_sha256: str, qdrant_snapshot_size: int) -> dict:
    source = f"vkt_v09_backup_src_{uuid.uuid4().hex[:8]}"
    target = f"vkt_v09_backup_dst_{uuid.uuid4().hex[:8]}"
    ids = {
        key: uuid.uuid4()
        for key in (
            "library",
            "document",
            "revision",
            "file",
            "evidence",
            "ontology",
            "entity_type",
            "entity",
            "publication",
            "publication_item",
            "sync_source",
            "operation",
            "mapping",
        )
    }
    content = b"v0.9 immutable acceptance object\n"
    digest = hashlib.sha256(content).hexdigest()
    await _admin(
        f'DROP DATABASE IF EXISTS "{source}" WITH (FORCE)',
        f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)',
        f'CREATE DATABASE "{source}"',
        f'CREATE DATABASE "{target}"',
    )
    try:
        env = os.environ.copy()
        env.update(
            DB_HOST=HOST,
            DB_PORT=str(PORT),
            DB_USER=USER,
            DB_PASSWORD=PASSWORD,
            DB_NAME=source,
        )
        _run([str(PYTHON), "-m", "alembic", "upgrade", "head"], env=env)
        await _seed(source, ids, content)
        with tempfile.TemporaryDirectory(prefix="vkb-v09-accept-") as temp:
            root = Path(temp).resolve()
            dump = root / "postgresql.dump"
            object_file = root / "immutable-object.bin"
            object_inventory = root / "objects.json"
            qdrant_inventory = root / "qdrant.json"
            manifest = root / "manifest.json"
            object_file.write_bytes(content)
            object_inventory.write_text(
                json.dumps(
                    {
                        "objects": [
                            {
                                "file": object_file.name,
                                "sha256": digest,
                                "size_bytes": len(content),
                            }
                        ]
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            qdrant_inventory.write_text(
                json.dumps(
                    {
                        "snapshot_sha256": qdrant_snapshot_sha256,
                        "snapshot_size_bytes": qdrant_snapshot_size,
                        "restore_status": "PASS",
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            _run(
                [
                    str(PG_BIN / "pg_dump.exe"),
                    "-h",
                    HOST,
                    "-p",
                    str(PORT),
                    "-U",
                    USER,
                    "-F",
                    "c",
                    "-d",
                    source,
                    "-f",
                    str(dump),
                ]
            )
            _run(
                [
                    str(PYTHON),
                    "scripts/deployment_backup_manifest.py",
                    "create",
                    "--postgresql",
                    str(dump),
                    "--object-inventory",
                    str(object_inventory),
                    "--qdrant-inventory",
                    str(qdrant_inventory),
                    "--output",
                    str(manifest),
                ]
            )
            _run(
                [
                    str(PYTHON),
                    "scripts/deployment_backup_manifest.py",
                    "verify",
                    "--manifest",
                    str(manifest),
                ]
            )
            _run(
                [
                    str(PG_BIN / "pg_restore.exe"),
                    "-h",
                    HOST,
                    "-p",
                    str(PORT),
                    "-U",
                    USER,
                    "-d",
                    target,
                    "--no-owner",
                    str(dump),
                ]
            )
            counts, file_row, revision_row = await _verify(target, ids)
            result = {
                "dump_size_bytes": dump.stat().st_size,
                "dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
                "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "counts": counts,
                "object_hash_matches": (
                    hashlib.sha256(object_file.read_bytes()).hexdigest()
                    == file_row["sha256"]
                    == digest
                ),
                "object_size_matches": object_file.stat().st_size
                == file_row["size_bytes"],
                "revision_pointer_matches": (
                    revision_row["current_revision_id"] == ids["revision"]
                    and revision_row["latest_revision_id"] == ids["revision"]
                ),
            }
            result["status"] = (
                "PASS"
                if (
                    all(value == 1 for value in counts.values())
                    and result["object_hash_matches"]
                    and result["object_size_matches"]
                    and result["revision_pointer_matches"]
                )
                else "FAIL"
            )
            return result
    finally:
        await _admin(
            f'DROP DATABASE IF EXISTS "{source}" WITH (FORCE)',
            f'DROP DATABASE IF EXISTS "{target}" WITH (FORCE)',
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qdrant-snapshot-sha256", required=True)
    parser.add_argument("--qdrant-snapshot-size", type=int, required=True)
    args = parser.parse_args()
    if not re_full_sha256(args.qdrant_snapshot_sha256):
        raise SystemExit("invalid Qdrant snapshot SHA-256")
    if args.qdrant_snapshot_size <= 0:
        raise SystemExit("invalid Qdrant snapshot size")
    result = asyncio.run(
        run_drill(args.qdrant_snapshot_sha256, args.qdrant_snapshot_size)
    )
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["status"] == "PASS" else 1)


def re_full_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


if __name__ == "__main__":
    main()
