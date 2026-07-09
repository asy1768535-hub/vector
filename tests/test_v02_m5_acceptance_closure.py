from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

from app.schemas.dify import DifyRecord, DifyRetrievalResponse
from app.workers.embedder import _build_payload
from scripts.v02_acceptance_manifest import build_manifest


def _gate(manifest: dict, gate_id: str) -> dict:
    return next(gate for gate in manifest["gates"] if gate["id"] == gate_id)


def test_v02_m5_manifest_covers_done_when_gates():
    manifest = build_manifest()

    assert manifest["version"] == "v0.2"
    assert manifest["phase"] == "M5"

    gate_ids = {gate["id"] for gate in manifest["gates"]}
    assert {
        "old_tests",
        "new_v02_tests",
        "real_postgresql_qdrant_acceptance",
        "dify_retrieval_compatibility",
        "migration_dry_run",
        "rollback_dry_run",
        "v03_evidence_handoff",
    } <= gate_ids

    assert "tests/test_dify_contract.py" in _gate(manifest, "dify_retrieval_compatibility")["command"]
    assert "tests/test_v02_m5_real_stack_acceptance.py" in _gate(
        manifest, "real_postgresql_qdrant_acceptance"
    )["command"]
    assert "alembic upgrade 0018" in _gate(manifest, "migration_dry_run")["operator_steps"]
    assert "alembic downgrade 0017" in _gate(manifest, "rollback_dry_run")["operator_steps"]


def test_old_tests_gate_is_full_non_v02_regression_not_compatibility_slice():
    gate = _gate(build_manifest(), "old_tests")

    assert "tests -q" in gate["command"]
    assert "not v02" in gate["command"]
    assert "test_dify_contract.py tests/test_query_import_api.py" not in gate["command"]


def test_real_stack_acceptance_test_exercises_retrieval_trace_and_tombstone():
    test_text = Path("tests/test_v02_m5_real_stack_acceptance.py").read_text(encoding="utf-8")

    for required in (
        "qdrant.upsert_points",
        "run_retrieval",
        "metadata[\"evidence_id\"]",
        "metadata[\"document_revision_id\"]",
        "deleted_at",
        "assert hidden.records == []",
    ):
        assert required in test_text


def test_dify_response_shape_keeps_v02_provenance_additive():
    response = DifyRetrievalResponse(
        records=[
            DifyRecord(
                content="body",
                score=0.91,
                title="title",
                metadata={
                    "document_id": "doc-1",
                    "chunk_id": "chunk-1",
                    "document_revision": 2,
                    "document_revision_id": "rev-1",
                    "evidence_id": "ev-1",
                },
            )
        ]
    )

    payload = response.model_dump()

    assert set(payload) == {"records"}
    assert set(payload["records"][0]) == {"content", "score", "title", "metadata"}
    assert payload["records"][0]["metadata"]["evidence_id"] == "ev-1"
    assert "evidence_id" not in payload["records"][0]
    assert "document_revision_id" not in payload["records"][0]


def test_qdrant_payload_keeps_legacy_and_v02_revision_fields():
    revision_id = uuid.uuid4()
    evidence_id = uuid.uuid4()

    payload = _build_payload(
        SimpleNamespace(id=uuid.uuid4()),
        SimpleNamespace(
            id=uuid.uuid4(),
            title="Doc",
            external_id="ext-1",
            current_revision=7,
            doc_metadata={"document_revision": 999, "evidence_id": "forged"},
            visibility_scope=None,
            security_level=None,
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            seq=3,
            text="chunk",
            document_revision_id=revision_id,
            evidence_id=evidence_id,
            block_id=None,
            chunk_kind="text",
            page_start=None,
            page_end=None,
            title_path=None,
            source_start=None,
            source_end=None,
            position=None,
        ),
        job=SimpleNamespace(
            document_revision=7,
            document_revision_id=revision_id,
            document_revision_no=7,
        ),
    )

    assert payload["document_revision"] == 7
    assert payload["document_revision_no"] == 7
    assert payload["document_revision_id"] == str(revision_id)
    assert payload["evidence_id"] == str(evidence_id)


def test_v03_handoff_tables_bind_to_evidence_id_without_chunk_dependency():
    tables = build_manifest()["v03_handoff_tables"]

    assert tables["entity_mentions"]["foreign_keys"] == {"evidence_id": "evidence_units.id"}
    assert tables["relation_evidence"]["foreign_keys"] == {"evidence_id": "evidence_units.id"}
    assert "chunk_id" not in tables["entity_mentions"]["columns"]
    assert "chunk_id" not in tables["relation_evidence"]["columns"]


def test_m5_docs_cover_release_gates_and_are_indexed():
    runbook = Path("docs/30-v0.2-evidence-acceptance-runbook.md").read_text(encoding="utf-8")
    release = Path("docs/31-upgrade-summary-v0.2-evidence-foundation.md").read_text(encoding="utf-8")
    index = Path("docs/README.md").read_text(encoding="utf-8")

    for required in (
        "migration dry-run",
        "rollback dry-run",
        "real PostgreSQL/Qdrant integration acceptance",
        "old /retrieval compatibility acceptance",
        "VECTOR_KB_PG_TEST_DSN",
        "VECTOR_KB_QDRANT_TEST_URL",
        "entity_mentions(evidence_id)",
        "relation_evidence(evidence_id)",
    ):
        assert required in runbook

    assert "documents.current_revision" in release
    assert "embedding_jobs.document_revision" in release
    assert "Qdrant payload.document_revision" in release
    assert "30-v0.2-evidence-acceptance-runbook.md" in index
    assert "31-upgrade-summary-v0.2-evidence-foundation.md" in index
