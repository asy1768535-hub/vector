from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from app.auth.backend import current_active_user
from app.config import Settings, settings
from app.db import get_db
from app.main import app
from app.models.chunk import Chunk
from app.models.chunk_links import ChunkBlock, ChunkEvidence
from app.models.document import Document
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision
from app.models.embedding_job import EmbeddingJob
from app.models.evidence_unit import EvidenceUnit
from app.models.library import Library
from app.models.user import User
from app.services import ingest as ingest_service


LIB_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
DOC_ID = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000cc")


def _lib() -> Library:
    return Library(
        id=LIB_ID,
        slug="lib",
        name="Library",
        qdrant_collection="lib_col",
        embedding_model="m",
        embedding_dim=8,
        chunk_size=1000,
        chunk_overlap=120,
    )


def _result(*, first=None, count: int = 0):
    result = MagicMock()
    result.scalars.return_value.first.return_value = first
    result.scalar_one.return_value = count
    return result


def _mock_db(added: list[object], *, first=None, count: int = 0):
    db = MagicMock()
    db.execute = AsyncMock(return_value=_result(first=first, count=count))
    db.add = MagicMock(side_effect=lambda obj: added.append(obj))
    db.add_all = MagicMock(side_effect=lambda objs: added.extend(list(objs)))

    async def _flush():
        for obj in added:
            if hasattr(obj, "id") and getattr(obj, "id", None) is None:
                setattr(obj, "id", uuid.uuid4())

    db.flush = AsyncMock(side_effect=_flush)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=None)
    cm.__aexit__ = AsyncMock(return_value=False)
    db.begin_nested = MagicMock(return_value=cm)
    return db


def _only(added: list[object], model):
    rows = [obj for obj in added if isinstance(obj, model)]
    assert len(rows) == 1
    return rows[0]


def _user() -> User:
    return User(id=USER_ID, email="m2@example.com", is_superuser=True, is_active=True)


def _client_with_db(db):
    async def override_db():
        return db

    async def override_user():
        return _user()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[current_active_user] = override_user
    return TestClient(app)


def test_evidence_write_path_flag_defaults_off():
    assert Settings().enable_evidence_write_path is False


def test_metadata_guard_rejects_reserved_system_fields():
    from app.services.metadata_guard import MetadataValidationError, validate_external_metadata

    for key in ("document_id", "chunk_id", "text", "title", "document_revision_id"):
        with pytest.raises(MetadataValidationError):
            validate_external_metadata({key: "bad"})


def test_metadata_guard_rejects_reserved_namespaces():
    from app.services.metadata_guard import MetadataValidationError, validate_external_metadata

    for key in ("_system", "system_owner", "_internal", "internal_owner"):
        with pytest.raises(MetadataValidationError):
            validate_external_metadata({key: "bad"})


def test_ingest_text_flagged_new_doc_creates_pending_revision_evidence_chunks_and_job(monkeypatch):
    monkeypatch.setattr(settings, "enable_evidence_write_path", True, raising=False)
    added: list[object] = []
    db = _mock_db(added)

    doc, job, count, was_existing = asyncio.run(
        ingest_service.ingest_text(
            db=db,
            library=_lib(),
            text="alpha",
            title="Doc",
            external_id="doc-1",
            metadata={"department": "legal"},
            splitter="text",
            created_by=None,
            chunks=[
                {
                    "text": "alpha",
                    "source_start": 0,
                    "source_end": 5,
                    "location": {"type": "line", "start_line": 1, "end_line": 1},
                }
            ],
        )
    )

    revision = _only(added, DocumentRevision)
    block = _only(added, DocumentBlock)
    evidence = _only(added, EvidenceUnit)
    chunk = _only(added, Chunk)
    chunk_block = _only(added, ChunkBlock)
    chunk_evidence = _only(added, ChunkEvidence)

    assert was_existing is False
    assert count == 1
    assert doc.latest_revision_id == revision.id
    assert doc.current_revision_id is None
    assert job.document_revision == 1
    assert job.document_revision_no == 1
    assert job.document_revision_id == revision.id
    assert job.status == "pending"
    assert revision.status == "pending"
    assert revision.normalized_text == "alpha"
    assert revision.document_metadata == {"department": "legal"}
    assert block.document_revision_id == revision.id
    assert block.block_kind == "paragraph"
    assert evidence.document_revision_id == revision.id
    assert evidence.document_block_id == block.id
    assert evidence.text_quote == "alpha"
    assert chunk.document_revision_id == revision.id
    assert chunk.block_id == block.id
    assert chunk.evidence_id == evidence.id
    assert chunk.chunk_kind == "text"
    assert chunk.source_start == 0
    assert chunk.source_end == 5
    assert chunk_block.chunk_id == chunk.id
    assert chunk_block.document_block_id == block.id
    assert chunk_evidence.chunk_id == chunk.id
    assert chunk_evidence.evidence_id == evidence.id


def test_reingest_flagged_changed_doc_creates_new_pending_revision_without_publishing(monkeypatch):
    monkeypatch.setattr(settings, "enable_evidence_write_path", True, raising=False)
    old_current = uuid.uuid4()
    doc = Document(
        id=DOC_ID,
        library_id=LIB_ID,
        external_id="doc-1",
        title="Old",
        content_hash="old",
        current_revision=1,
        current_revision_id=old_current,
        latest_revision_id=old_current,
        status="ready",
    )
    added: list[object] = []
    db = _mock_db(added)

    with patch("app.services.cleanup.enqueue_delete_before_revision", new_callable=AsyncMock):
        job, count, changed = asyncio.run(
            ingest_service.reingest_document(
                db=db,
                library=_lib(),
                document=doc,
                new_text="beta",
                title="New",
                metadata={"department": "legal"},
                splitter="text",
                force=False,
                chunks=["beta"],
            )
        )

    revision = _only(added, DocumentRevision)
    chunk = _only(added, Chunk)
    assert changed is True
    assert count == 1
    assert doc.current_revision == 2
    assert doc.current_revision_id == old_current
    assert doc.latest_revision_id == revision.id
    assert revision.revision_no == 2
    assert revision.status == "pending"
    assert job is not None
    assert job.document_revision == 2
    assert job.document_revision_no == 2
    assert job.document_revision_id == revision.id
    assert chunk.document_revision_id == revision.id
    assert chunk.evidence_id is not None


def test_reingest_flagged_noop_does_not_create_revision_or_job(monkeypatch):
    monkeypatch.setattr(settings, "enable_evidence_write_path", True, raising=False)
    doc = Document(
        id=DOC_ID,
        library_id=LIB_ID,
        title="Same",
        content_hash=ingest_service._content_hash("same text"),
        current_revision=3,
        status="ready",
        doc_metadata={"department": "legal"},
    )
    added: list[object] = []
    db = _mock_db(added, count=2)

    job, count, changed = asyncio.run(
        ingest_service.reingest_document(
            db=db,
            library=_lib(),
            document=doc,
            new_text="same text",
            title="Same",
            metadata={"department": "legal"},
            splitter="text",
            force=False,
            chunks=["same text"],
        )
    )

    assert job is None
    assert count == 2
    assert changed is False
    assert not [obj for obj in added if isinstance(obj, (DocumentRevision, EmbeddingJob))]


def test_ingest_text_flagged_generation_failure_marks_revision_failed(monkeypatch):
    monkeypatch.setattr(settings, "enable_evidence_write_path", True)
    added: list[object] = []
    db = _mock_db(added)

    def fail_add_all(objs):
        added.extend(list(objs))
        raise RuntimeError("write failed")

    db.add_all = MagicMock(side_effect=fail_add_all)

    with pytest.raises(ValueError, match="write failed"):
        asyncio.run(
            ingest_service.ingest_text(
                db=db,
                library=_lib(),
                text="gamma",
                title="Doc",
                external_id=None,
                metadata=None,
                splitter="text",
                created_by=None,
                chunks=["gamma"],
            )
        )

    revision = _only(added, DocumentRevision)
    assert revision.status == "failed"
    assert "write failed" in (revision.last_error or "")


@patch("app.services.ingest.ingest_text", new_callable=AsyncMock)
@patch("app.deps.load_active_library", new_callable=AsyncMock)
@patch("app.api.documents._lock_writable", new_callable=AsyncMock)
def test_api_flagged_new_write_returns_202_and_split_status_fields(mock_lock, mock_load, mock_ingest, monkeypatch):
    monkeypatch.setattr(settings, "enable_evidence_write_path", True)
    revision_id = uuid.uuid4()
    doc = Document(
        id=DOC_ID,
        library_id=LIB_ID,
        title="Doc",
        content_hash="h",
        current_revision=1,
        latest_revision_id=revision_id,
        status="pending",
    )
    job = EmbeddingJob(
        id=uuid.uuid4(),
        library_id=LIB_ID,
        document_id=DOC_ID,
        document_revision=1,
        document_revision_id=revision_id,
        document_revision_no=1,
        status="pending",
    )
    mock_load.return_value = _lib()
    mock_lock.return_value = _lib()
    mock_ingest.return_value = (doc, job, 1, False)
    db = MagicMock()
    db.commit = AsyncMock()

    try:
        response = _client_with_db(db).post(
            "/libraries/lib/documents",
            json={"title": "Doc", "text": "hello", "metadata": {"department": "legal"}},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == status.HTTP_202_ACCEPTED
    data = response.json()
    assert data["status"] == "pending"
    assert data["document_status"] == "pending"
    assert data["revision_status"] == "pending"
    assert data["job_status"] == "pending"
    assert data["document_revision_id"] == str(revision_id)


@patch("app.services.ingest.reingest_document", new_callable=AsyncMock)
@patch("app.deps.load_active_library", new_callable=AsyncMock)
@patch("app.api.documents._lock_writable", new_callable=AsyncMock)
def test_api_flagged_external_id_noop_returns_200(mock_lock, mock_load, mock_reingest, monkeypatch):
    monkeypatch.setattr(settings, "enable_evidence_write_path", True)
    revision_id = uuid.uuid4()
    existing = Document(
        id=DOC_ID,
        library_id=LIB_ID,
        external_id="ext-1",
        title="Doc",
        content_hash=ingest_service._content_hash("same"),
        current_revision=2,
        current_revision_id=revision_id,
        latest_revision_id=revision_id,
        status="ready",
    )
    mock_load.return_value = _lib()
    mock_lock.return_value = _lib()
    mock_reingest.return_value = (None, 2, False)
    db = _mock_db([], first=existing)
    db.commit = AsyncMock()

    try:
        response = _client_with_db(db).post(
            "/libraries/lib/documents",
            json={"external_id": "ext-1", "title": "Doc", "text": "same"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["document_id"] == str(DOC_ID)
    assert data["document_status"] == "ready"
    assert data["job_id"] is None


@patch("app.deps.load_active_library", new_callable=AsyncMock)
@patch("app.api.documents._lock_writable", new_callable=AsyncMock)
def test_api_flagged_reserved_metadata_returns_422(mock_lock, mock_load, monkeypatch):
    monkeypatch.setattr(settings, "enable_evidence_write_path", True)
    mock_load.return_value = _lib()
    mock_lock.return_value = _lib()
    db = MagicMock()
    db.commit = AsyncMock()

    try:
        response = _client_with_db(db).post(
            "/libraries/lib/documents",
            json={"title": "Doc", "text": "hello", "metadata": {"document_id": "bad"}},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
