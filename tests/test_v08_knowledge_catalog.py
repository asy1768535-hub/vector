from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from app.config import Settings, settings, validate_knowledge_catalog_startup
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.document_revision_file import DocumentRevisionFile
from app.models.library import Library
from app.models.knowledge_artifact import KnowledgeArtifact
from app.schemas.knowledge_catalog import (
    CatalogClassificationRead,
    CatalogDocumentListItemRead,
    CatalogGraphCountsRead,
)
from app.schemas.storage import StorageLocatorV1
from app.services import knowledge_catalog as service
from app.services.knowledge_catalog_contracts import (
    CatalogDocumentCursor,
    CatalogDocumentQuery,
    KnowledgeCatalogError,
    catalog_filter_fingerprint,
    decode_catalog_document_cursor,
    encode_catalog_document_cursor,
    project_catalog_capabilities,
)


NOW = datetime(2026, 7, 22, 23, 30, tzinfo=timezone.utc)
HASH = "a" * 64


class _Result:
    def __init__(self, rows=(), scalar=None):
        self.rows = list(rows)
        self.scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar_one(self):
        return self.scalar

    def scalar_one_or_none(self):
        return self.scalar


class _DB:
    def __init__(self, *results):
        self.results = list(results)
        self.statements = []
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def execute(self, statement):
        self.statements.append(statement)
        assert self.results, "unexpected Catalog query"
        return self.results.pop(0)


def _library() -> Library:
    return Library(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        slug="catalog",
        name="Catalog",
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection="catalog",
        index_state="ready",
        summary_artifact_enabled=True,
        outline_artifact_enabled=True,
        created_at=NOW,
    )


def _current(library: Library):
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


def _empty_classification() -> CatalogClassificationRead:
    return CatalogClassificationRead(
        state="unclassified",
        decision_set_id=None,
        taxonomy_version_id=None,
        source=None,
        labels=[],
        latest_run_id=None,
        latest_run_status=None,
    )


def _list_item(library: Library, document: Document, revision: DocumentRevision):
    return CatalogDocumentListItemRead(
        document_id=document.id,
        library_id=library.id,
        title="Contract",
        document_status="ready",
        revision_id=revision.id,
        revision_no=1,
        revision_status="ready",
        revision_content_hash=HASH,
        updated_at=NOW,
        overall_state="usable",
        capabilities={
            "source": "ready",
            "search": "ready",
            "chat": "ready",
            "summary": "disabled",
            "outline": "disabled",
            "classification": "disabled",
            "graph": "disabled",
        },
        classification=_empty_classification(),
        summary_excerpt=None,
        graph_counts=CatalogGraphCountsRead(entities=0, relations=0),
    )


def test_catalog_startup_default_off_and_limits_fail_closed():
    assert settings.knowledge_catalog_enabled is False
    validate_knowledge_catalog_startup(Settings(_env_file=None))
    with pytest.raises(RuntimeError, match="requires Organization"):
        validate_knowledge_catalog_startup(
            Settings(_env_file=None, knowledge_catalog_enabled=True)
        )
    validate_knowledge_catalog_startup(
        Settings(
            _env_file=None,
            knowledge_catalog_enabled=True,
            organization_authorization_enabled=True,
        )
    )
    with pytest.raises(RuntimeError, match="entity limit"):
        validate_knowledge_catalog_startup(
            Settings(_env_file=None, knowledge_catalog_max_entity_cards=101)
        )
    env_example = Path(".env.example").read_text(encoding="utf-8")
    assert "KNOWLEDGE_CATALOG_ENABLED=false" in env_example
    assert "KNOWLEDGE_CATALOG_MAX_EVIDENCE_PER_FACT=20" in env_example


def test_catalog_cursor_is_canonical_filter_bound_and_tamper_evident():
    library_id = uuid.uuid4()
    query = CatalogDocumentQuery(title_query="  Finance  ", limit=25)
    fingerprint = catalog_filter_fingerprint(library_id, query)
    cursor = CatalogDocumentCursor(NOW, uuid.uuid4(), fingerprint)
    encoded = encode_catalog_document_cursor(cursor)
    assert decode_catalog_document_cursor(
        encoded,
        expected_filter_fingerprint=fingerprint,
    ) == cursor
    with pytest.raises(KnowledgeCatalogError) as mismatch:
        decode_catalog_document_cursor(
            encoded,
            expected_filter_fingerprint=catalog_filter_fingerprint(
                library_id,
                CatalogDocumentQuery(title_query="Legal", limit=25),
            ),
        )
    assert mismatch.value.code == "catalog_cursor_invalid"
    with pytest.raises(KnowledgeCatalogError):
        decode_catalog_document_cursor(
            encoded[:-1] + ("A" if encoded[-1] != "A" else "B"),
            expected_filter_fingerprint=fingerprint,
        )


def test_catalog_state_reducer_keeps_derived_failure_partial():
    projection = project_catalog_capabilities(
        document_status="ready",
        revision_status="ready",
        source_available=True,
        summary_state="failed",
        outline_state="ready",
        classification_state="pending_review",
        graph_state="disabled",
    )
    assert projection.overall_state == "partial"
    assert projection.search == "ready" and projection.chat == "ready"
    failed = project_catalog_capabilities(
        document_status="failed",
        revision_status="failed",
        source_available=False,
        summary_state="disabled",
        outline_state="disabled",
        classification_state="disabled",
        graph_state="disabled",
    )
    assert failed.overall_state == "failed"


def test_catalog_sql_fences_current_revision_and_publication_support():
    library_id, document_id, revision_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    document_sql = str(
        service._base_current_document_statement(library_id).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert "documents.current_revision_id = document_revisions.id" in document_sql
    assert "documents.deleted_at is null" in document_sql
    entity_sql = str(
        service._entity_fact_projection(
            library_id=library_id,
            document_id=document_id,
            revision_id=revision_id,
        ).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    assert "graph_publications.status = 'active'" in entity_sql
    assert "ontology_versions.status = 'active'" in entity_sql
    assert "support_evidence_ids ? cast(evidence_units.id as varchar)" in entity_sql
    assert f"entity_mentions.document_revision_id = '{revision_id}'" in entity_sql


def test_catalog_page_uses_count_page_and_one_batch_hydration(monkeypatch):
    library = _library()
    document, revision = _current(library)
    db = _DB(_Result(scalar=1), _Result(rows=((document, revision),)))
    hydrate = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(service, "_hydrate_current_documents", hydrate)
    monkeypatch.setattr(
        service,
        "_list_item",
        lambda *_args, **_kwargs: _list_item(library, document, revision),
    )
    page = asyncio.run(
        service.list_catalog_documents(
            db,
            library=library,
            query=CatalogDocumentQuery(limit=1),
            config=Settings(_env_file=None),
        )
    )
    assert page.total == 1 and len(page.items) == 1
    assert len(db.statements) == 2
    hydrate.assert_awaited_once()
    db.commit.assert_not_awaited()


def test_document_graph_projects_only_supported_current_fact(monkeypatch):
    library = _library()
    document, revision = _current(library)
    publication_id, ontology_id, item_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    entity_id, type_id, evidence_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    entity = SimpleNamespace(
        publication_id=publication_id,
        ontology_version_id=ontology_id,
        item_id=item_id,
        item_hash="b" * 64,
        entity_id=entity_id,
        entity_type_id=type_id,
        entity_type_key="company",
        entity_type_label="Company",
        canonical_name="Acme",
        source_type="extracted",
        confidence=0.9,
    )
    evidence = SimpleNamespace(
        item_id=item_id,
        evidence_id=evidence_id,
        document_id=document.id,
        document_revision_id=revision.id,
        page_start=1,
        page_end=1,
        source_start=0,
        source_end=4,
        evidence_rank=1,
    )
    db = _DB(
        _Result(scalar=1),
        _Result(scalar=0),
        _Result(rows=(entity,)),
        _Result(rows=()),
        _Result(rows=(evidence,)),
    )
    graph = asyncio.run(
        service._document_graph(
            db,
            library_id=library.id,
            document_id=document.id,
            revision_id=revision.id,
            config=Settings(
                _env_file=None,
                graph_retrieval_enabled=True,
            ),
        )
    )
    assert graph.counts.entities == 1 and graph.counts.relations == 0
    assert graph.entities[0].evidence[0].evidence_id == evidence_id
    assert graph.entities[0].publication_id == publication_id


def test_prepare_file_access_requires_exact_current_available_revision():
    library = _library()
    document, revision = _current(library)
    row = DocumentRevisionFile(
        id=uuid.uuid4(),
        document_revision_id=revision.id,
        document_id=document.id,
        library_id=library.id,
        file_name="contract.pdf",
        content_type="application/pdf",
        storage_path="objects/contract.pdf",
        size_bytes=12,
        sha256=HASH,
        storage_provider="local",
        endpoint_ref="primary",
        bucket=None,
        object_key="objects/contract.pdf",
        object_version=None,
        etag=None,
        immutability_mode="content_hash",
        managed_snapshot=True,
        lifecycle_status="available",
        created_at=NOW,
    )
    db = _DB(_Result(rows=((row, document, revision),)))
    prepared = asyncio.run(
        service.prepare_catalog_file_access(
            db,
            library=library,
            revision_file_id=row.id,
        )
    )
    assert prepared.access.locator == StorageLocatorV1(
        provider="local",
        endpoint_ref="primary",
        bucket=None,
        object_key="objects/contract.pdf",
        object_version=None,
        etag=None,
        immutability_mode="content_hash",
    )
    assert prepared.document_revision_id == revision.id
    db.commit.assert_not_awaited()


def test_evidence_detail_requires_current_publication_support(monkeypatch):
    library = _library()
    document, revision = _current(library)
    evidence_id, item_id = uuid.uuid4(), uuid.uuid4()
    detail = SimpleNamespace(
        id=evidence_id,
        document_id=document.id,
        document_revision_id=revision.id,
        evidence_kind="chunk",
        text_quote="quoted source",
        text_window="bounded source window",
        window_start=0,
        window_end=21,
        source_start=0,
        source_end=13,
        page_start=1,
        page_end=1,
        title_path=["Agreement", {"private": "metadata"}, "Terms"],
    )
    monkeypatch.setattr(service, "get_evidence_detail", AsyncMock(return_value=detail))
    fact = SimpleNamespace(
        publication_id=uuid.uuid4(),
        ontology_version_id=uuid.uuid4(),
        item_id=item_id,
        fact_id=uuid.uuid4(),
        item_hash="b" * 64,
    )
    db = _DB(_Result(rows=(fact,)), _Result(rows=()), _Result(scalar=None))
    result = asyncio.run(
        service.get_catalog_evidence_detail(
            db,
            library=library,
            evidence_id=evidence_id,
        )
    )
    assert result.fact_refs[0].item_id == item_id
    assert result.library_id == library.id
    assert result.fact_refs[0].chunk_id is None
    assert result.title_path == ["Agreement", "Terms"]
    assert "private" not in result.model_dump_json()

    unpublished = _DB(_Result(rows=()), _Result(rows=()))
    with pytest.raises(KnowledgeCatalogError) as exc_info:
        asyncio.run(
            service.get_catalog_evidence_detail(
                unpublished,
                library=library,
                evidence_id=evidence_id,
            )
        )
    assert exc_info.value.code == "catalog_not_found"


def test_catalog_revalidates_artifact_payload_and_services_never_commit():
    library = _library()
    document, revision = _current(library)
    artifact = KnowledgeArtifact(
        id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        library_id=library.id,
        document_id=document.id,
        document_revision_id=revision.id,
        artifact_type="summary",
        contract_version="summary-v1",
        extractor_version="summary-extractor-v1",
        generation_mode="deterministic",
        model_provider=None,
        model_name=None,
        model_config_hash=None,
        input_fingerprint="b" * 64,
        payload={
            "schema_version": "summary-v1",
            "summary": "A bounded summary.",
            "generation_mode": "deterministic",
            "source_character_count": 16,
            "truncated": False,
        },
        payload_hash="c" * 64,
        lifecycle_state="current",
        created_at=NOW,
    )
    summary = service._summary_read(artifact)
    assert summary is not None and summary.summary == "A bounded summary."
    artifact.payload = {**artifact.payload, "raw_provider_response": "secret"}
    with pytest.raises(KnowledgeCatalogError) as exc_info:
        service._summary_read(artifact)
    assert exc_info.value.code == "catalog_invariant_failed"
    assert ".commit(" not in inspect.getsource(service)


def test_classification_filters_cannot_bypass_disabled_governance():
    library = _library()
    with pytest.raises(KnowledgeCatalogError) as exc_info:
        asyncio.run(
            service.list_catalog_documents(
                _DB(),
                library=library,
                query=CatalogDocumentQuery(classification_state="classified"),
                config=Settings(
                    _env_file=None,
                    classification_decision_enabled=False,
                ),
            )
        )
    assert exc_info.value.code == "catalog_classification_filter_unavailable"
