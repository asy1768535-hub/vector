from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.models.classification_job import DocumentClassificationJob
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.knowledge_artifact_job import KnowledgeArtifactJob
from app.models.library import Library
from app.services import document_processing_diagnostics as service
from app.services.document_processing_contracts import DocumentProcessingError


NOW = datetime(2026, 7, 22, 10, 30, tzinfo=timezone.utc)
HASH = "a" * 64


class _Result:
    def __init__(self, *, rows=(), scalar=None):
        self.rows = list(rows)
        self.scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)

    def scalar_one_or_none(self):
        return self.scalar


class _DB:
    def __init__(self, *results):
        self.results = list(results)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        assert self.results, "unexpected processing diagnostics query"
        return self.results.pop(0)


def _library() -> Library:
    return Library(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        slug="contracts",
        name="Contracts",
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection="contracts",
        index_state="ready",
        summary_artifact_enabled=True,
        outline_artifact_enabled=True,
        graph_extraction_enabled=True,
        created_at=NOW,
    )


def _scope(library: Library) -> tuple[Document, DocumentRevision]:
    document_id, revision_id = uuid.uuid4(), uuid.uuid4()
    document = Document(
        id=document_id,
        library_id=library.id,
        title="Contract",
        content_hash=HASH,
        current_revision=1,
        current_revision_id=revision_id,
        latest_revision_id=revision_id,
        status="ready",
        created_at=NOW,
        updated_at=NOW,
    )
    revision = DocumentRevision(
        id=revision_id,
        document_id=document_id,
        library_id=library.id,
        revision_no=1,
        title="Contract",
        content_hash=HASH,
        normalized_text="Contract source",
        parser_name="test",
        parser_version="1",
        chunking_strategy="fixed",
        chunking_strategy_version="1",
        status="ready",
        created_at=NOW,
        updated_at=NOW,
        finished_at=NOW,
    )
    return document, revision


def _artifact_job(
    library: Library,
    document: Document,
    revision: DocumentRevision,
    *,
    artifact_type: str,
    status: str = "failed",
    retry_generation: int = 0,
) -> KnowledgeArtifactJob:
    return KnowledgeArtifactJob(
        id=uuid.uuid4(),
        library_id=library.id,
        document_id=document.id,
        document_revision_id=revision.id,
        artifact_type=artifact_type,
        contract_version=f"{artifact_type}-v1",
        extractor_version="v1",
        generation_mode="deterministic",
        input_fingerprint=HASH,
        idempotency_key=uuid.uuid4().hex,
        retry_generation=retry_generation,
        attempt_count=2,
        trigger_type="manual",
        status=status,
        error_code="raw_provider_secret_detail",
        error_message="provider response with secret",
        created_at=NOW,
        updated_at=NOW,
        finished_at=NOW,
    )


def _classification_job(
    library: Library,
    document: Document,
    revision: DocumentRevision,
) -> DocumentClassificationJob:
    return DocumentClassificationJob(
        id=uuid.uuid4(),
        library_id=library.id,
        document_id=document.id,
        document_revision_id=revision.id,
        revision_content_hash=HASH,
        taxonomy_version_id=uuid.uuid4(),
        enabled_label_set_hash=HASH,
        classifier_version="v1",
        model_provider="deepseek",
        model_name="deepseek-v4-pro",
        model_config_hash=HASH,
        prompt_version="v1",
        input_fingerprint=HASH,
        idempotency_key=uuid.uuid4().hex,
        retry_generation=1,
        trigger_type="retry",
        status="failed",
        attempt_count=3,
        error_code="provider_timeout",
        error_message="raw timeout detail",
        created_at=NOW,
        updated_at=NOW,
        finished_at=NOW,
    )


def _graph_job(
    library: Library,
    document: Document,
    revision: DocumentRevision,
) -> GraphExtractionJob:
    return GraphExtractionJob(
        id=uuid.uuid4(),
        library_id=library.id,
        document_id=document.id,
        document_revision_id=revision.id,
        ontology_version_id=uuid.uuid4(),
        trigger_type="manual",
        execution_mode="production",
        status="partially_succeeded",
        retry_generation=2,
        created_at=NOW,
        updated_at=NOW,
        finished_at=NOW,
    )


def _enabled_config() -> Settings:
    return Settings(
        _env_file=None,
        knowledge_artifact_runtime_enabled=True,
        classification_runtime_enabled=True,
        graph_extraction_enabled=True,
    )


def test_projection_is_current_revision_bounded_and_redacts_native_errors():
    library = _library()
    document, revision = _scope(library)
    summary = _artifact_job(library, document, revision, artifact_type="summary")
    classification = _classification_job(library, document, revision)
    graph = _graph_job(library, document, revision)
    db = _DB(
        _Result(scalar=document),
        _Result(scalar=revision),
        _Result(rows=(summary,)),
        _Result(scalar=classification),
        _Result(scalar=graph),
        _Result(rows=(("failed", 2, 5, 1), ("succeeded", 3, 3, 0))),
    )

    result = asyncio.run(
        service.get_document_processing_diagnostics(
            db,
            library=library,
            document_id=document.id,
            config=_enabled_config(),
        )
    )

    assert result.document_revision_id == revision.id
    assert [item.stage for item in result.stages] == [
        "summary",
        "outline",
        "classification",
        "graph",
    ]
    by_stage = {item.stage: item for item in result.stages}
    assert by_stage["summary"].safe_error_code == "stage_failed"
    assert by_stage["summary"].retryable is True
    assert by_stage["outline"].status == "not_started"
    assert by_stage["classification"].safe_error_code == "provider_timeout"
    assert by_stage["graph"].retryable is True
    assert by_stage["graph"].attempt_count == 8
    assert by_stage["graph"].graph_counts.retryable_failed == 1
    serialized = result.model_dump_json()
    assert "raw_provider_secret_detail" not in serialized
    assert "provider response with secret" not in serialized
    assert "raw timeout detail" not in serialized


def test_missing_exact_current_revision_fails_without_falling_back():
    library = _library()
    document, _revision = _scope(library)
    db = _DB(_Result(scalar=document), _Result(scalar=None))

    with pytest.raises(DocumentProcessingError) as exc_info:
        asyncio.run(
            service.get_document_processing_diagnostics(
                db,
                library=library,
                document_id=document.id,
                config=_enabled_config(),
            )
        )
    assert exc_info.value.code == "processing_revision_unavailable"
    assert len(db.statements) == 2


def test_artifact_retry_is_generation_fenced_delegated_and_audited(monkeypatch):
    library = _library()
    document, revision = _scope(library)
    source = _artifact_job(library, document, revision, artifact_type="summary")
    successor = _artifact_job(
        library,
        document,
        revision,
        artifact_type="summary",
        status="queued",
        retry_generation=1,
    )
    successor.rerun_of_job_id = source.id
    db = _DB(
        _Result(scalar=document),
        _Result(scalar=revision),
        _Result(scalar=source),
        _Result(rows=(successor,)),
        _Result(scalar=None),
        _Result(scalar=None),
    )
    retry = AsyncMock(return_value=successor)
    audit = AsyncMock()
    monkeypatch.setattr(service, "retry_knowledge_artifact_job", retry)
    monkeypatch.setattr(service.audit_log, "record", audit)
    actor_id = uuid.uuid4()

    result = asyncio.run(
        service.retry_document_processing_stage(
            db,
            library=library,
            document_id=document.id,
            stage="summary",
            source_job_id=source.id,
            observed_retry_generation=0,
            actor_user_id=actor_id,
            config=_enabled_config(),
        )
    )

    retry.assert_awaited_once()
    assert result.stages[0].job_id == successor.id
    assert result.stages[0].status == "queued"
    target = audit.await_args.args[3]
    assert target["document_revision_id"] == str(revision.id)
    assert target["stage"] == "summary"
    assert "error_message" not in target and "error_code" not in target


def test_retry_rejects_an_obsolete_generation_before_dispatch(monkeypatch):
    library = _library()
    document, revision = _scope(library)
    source = _artifact_job(
        library,
        document,
        revision,
        artifact_type="outline",
        retry_generation=2,
    )
    db = _DB(
        _Result(scalar=document),
        _Result(scalar=revision),
        _Result(scalar=source),
    )
    retry = AsyncMock()
    monkeypatch.setattr(service, "retry_knowledge_artifact_job", retry)

    with pytest.raises(DocumentProcessingError) as exc_info:
        asyncio.run(
            service.retry_document_processing_stage(
                db,
                library=library,
                document_id=document.id,
                stage="outline",
                source_job_id=source.id,
                observed_retry_generation=1,
                actor_user_id=uuid.uuid4(),
                config=_enabled_config(),
            )
        )
    assert exc_info.value.code == "processing_job_stale"
    retry.assert_not_awaited()


def test_graph_retry_requires_the_latest_production_job(monkeypatch):
    library = _library()
    document, revision = _scope(library)
    source = _graph_job(library, document, revision)
    latest = _graph_job(library, document, revision)
    latest.retry_generation = source.retry_generation + 1
    db = _DB(
        _Result(scalar=document),
        _Result(scalar=revision),
        _Result(scalar=source),
        _Result(scalar=latest),
    )
    retry = AsyncMock()
    monkeypatch.setattr(service, "retry_graph_extraction_job", retry)

    with pytest.raises(DocumentProcessingError) as exc_info:
        asyncio.run(
            service.retry_document_processing_stage(
                db,
                library=library,
                document_id=document.id,
                stage="graph",
                source_job_id=source.id,
                observed_retry_generation=source.retry_generation,
                actor_user_id=uuid.uuid4(),
                config=_enabled_config(),
            )
        )
    assert exc_info.value.code == "processing_job_stale"
    retry.assert_not_awaited()
