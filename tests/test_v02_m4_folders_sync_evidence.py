from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.auth.backend import current_active_user
from app.config import settings
from app.db import get_db
from app.main import app
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.evidence_unit import EvidenceUnit
from app.models.folder import Folder
from app.models.library import Library
from app.models.sync_source import SyncSource
from app.models.user import User


LIB_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
DOC_ID = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
REV_ID = uuid.UUID("00000000-0000-0000-0000-0000000000cc")
OLD_REV_ID = uuid.UUID("00000000-0000-0000-0000-0000000000cd")
PARENT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000dd")
CHILD_ID = uuid.UUID("00000000-0000-0000-0000-0000000000ee")
FOLDER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000ff")
SOURCE_ID = uuid.UUID("00000000-0000-0000-0000-000000000012")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000011")
EVIDENCE_ID = uuid.UUID("00000000-0000-0000-0000-000000000013")
CHUNK_ID = uuid.UUID("00000000-0000-0000-0000-000000000014")


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
        lifecycle_mode="managed",
        index_state="ready",
    )


def _user() -> User:
    return User(id=USER_ID, email="m4@example.com", is_superuser=True, is_active=True)


def _client_with_db(db):
    async def override_db():
        return db

    async def override_user():
        return _user()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[current_active_user] = override_user
    return TestClient(app)


def test_m4_feature_flag_defaults_off():
    from app.config import Settings

    assert Settings().enable_sync_source_api is False


def test_m4_router_is_mounted():
    from app.main import app

    paths = set(app.openapi()["paths"])
    assert "/libraries/{slug}/folders" in paths
    assert "/libraries/{slug}/sync-sources" in paths
    assert "/libraries/{slug}/evidence/{evidence_id}" in paths


def test_folder_path_normalization_rejects_empty_segments():
    from app.services.folders import normalize_folder_path

    assert normalize_folder_path("/contracts/sales/") == ["contracts", "sales"]
    assert normalize_folder_path("/") == []

    with pytest.raises(ValueError, match="empty"):
        normalize_folder_path("/contracts//sales")


def test_folder_move_rejects_cycle():
    from app.services.folders import ensure_not_descendant_move

    parent = Folder(id=PARENT_ID, library_id=LIB_ID, name="contracts", path="/contracts")
    child = Folder(
        id=CHILD_ID,
        library_id=LIB_ID,
        parent_id=PARENT_ID,
        name="sales",
        path="/contracts/sales",
    )

    with pytest.raises(ValueError, match="descendant"):
        ensure_not_descendant_move(parent, child)


def test_document_folder_move_does_not_change_revision():
    from app.services.folders import apply_document_folder

    doc = Document(
        id=DOC_ID,
        library_id=LIB_ID,
        folder_id=None,
        title="Doc",
        content_hash="hash",
        current_revision=4,
        current_revision_id=REV_ID,
        latest_revision_id=REV_ID,
        status="ready",
    )

    apply_document_folder(doc, FOLDER_ID)

    assert doc.folder_id == FOLDER_ID
    assert doc.current_revision == 4
    assert doc.current_revision_id == REV_ID
    assert doc.latest_revision_id == REV_ID


def test_document_folder_move_api_returns_revision_unchanged():
    doc = Document(
        id=DOC_ID,
        library_id=LIB_ID,
        folder_id=None,
        title="Doc",
        content_hash="hash",
        current_revision=4,
        current_revision_id=REV_ID,
        latest_revision_id=REV_ID,
        status="ready",
    )
    folder = Folder(id=FOLDER_ID, library_id=LIB_ID, name="contracts", path="/contracts")
    db = AsyncMock()

    async def get_model(model, ident):
        if model is Document and ident == DOC_ID:
            return doc
        if model is Folder and ident == FOLDER_ID:
            return folder
        return None

    db.get = AsyncMock(side_effect=get_model)
    db.commit = AsyncMock()

    try:
        with patch("app.deps.load_active_library", new=AsyncMock(return_value=_lib())):
            response = _client_with_db(db).patch(
                f"/libraries/lib/documents/{DOC_ID}/folder",
                json={"folder_id": str(FOLDER_ID)},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()
    assert data["document_id"] == str(DOC_ID)
    assert data["folder_id"] == str(FOLDER_ID)
    assert data["current_revision"] == 4
    assert data["current_revision_id"] == str(REV_ID)
    assert data["latest_revision_id"] == str(REV_ID)
    db.commit.assert_awaited_once()


def test_sync_source_flag_blocks_sync_source_api_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "enable_sync_source_api", False, raising=False)
    db = AsyncMock()

    try:
        with patch("app.deps.load_active_library", new=AsyncMock(return_value=_lib())):
            response = _client_with_db(db).post(
                "/libraries/lib/sync-sources",
                json={"source_key": "crm", "display_name": "CRM"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_deleted_sync_source_rejects_upsert_service():
    from app.services.sync_sources import ensure_sync_source_writable

    deleted = SyncSource(
        id=uuid.uuid4(),
        library_id=LIB_ID,
        source_key="crm",
        display_name="CRM",
        status="deleted",
        deleted_at=datetime.now(timezone.utc),
    )

    with pytest.raises(ValueError, match="not active"):
        ensure_sync_source_writable(deleted)


def test_disabled_sync_source_rejects_upsert_service():
    from app.services.sync_sources import ensure_sync_source_writable

    disabled = SyncSource(
        id=uuid.uuid4(),
        library_id=LIB_ID,
        source_key="crm",
        display_name="CRM",
        status="disabled",
        deleted_at=None,
    )

    with pytest.raises(ValueError, match="not active"):
        ensure_sync_source_writable(disabled)


def _result(first=None, all_rows=None):
    result = MagicMock()
    result.scalars.return_value.first.return_value = first
    result.scalars.return_value.all.return_value = all_rows or []
    return result


def test_sync_document_identity_statement_filters_by_sync_source_id():
    from app.services.sync_documents import sync_document_identity_statement

    stmt = sync_document_identity_statement(LIB_ID, SOURCE_ID, "contract-1")
    sql = str(stmt).lower()
    params = stmt.compile().params

    assert "sync_source_id" in sql
    assert params["library_id_1"] == LIB_ID
    assert params["sync_source_id_1"] == SOURCE_ID
    assert params["external_id_1"] == "contract-1"


def test_sync_upsert_same_payload_is_idempotent():
    from app.services.sync_documents import content_hash, is_revision_affecting_change

    doc = Document(
        id=DOC_ID,
        library_id=LIB_ID,
        sync_source_id=SOURCE_ID,
        external_id="contract-1",
        title="Contract",
        doc_metadata={"department": "legal"},
        content_hash=content_hash("same text"),
        current_revision=3,
        current_revision_id=REV_ID,
        latest_revision_id=REV_ID,
        visibility_scope="internal",
        security_level="normal",
        status="ready",
    )

    assert (
        is_revision_affecting_change(
            doc,
            text="same text",
            title="Contract",
            metadata={"department": "legal"},
            visibility_scope="internal",
            security_level="normal",
        )
        is False
    )


def test_sync_delete_is_idempotent_for_missing_document():
    from app.schemas.v02_m4 import SyncDocumentDeleteRequest
    from app.services.sync_documents import delete_sync_document

    source = SyncSource(
        id=SOURCE_ID,
        library_id=LIB_ID,
        source_key="crm",
        display_name="CRM",
        status="active",
    )
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_result(first=source), _result(first=None)])
    db.flush = AsyncMock()

    result = asyncio.run(
        delete_sync_document(
            db,
            _lib(),
            "crm",
            SyncDocumentDeleteRequest(external_id="missing"),
        )
    )

    assert result.operation == "not_found"
    assert result.external_id == "missing"
    assert result.document_id is None
    db.flush.assert_not_awaited()


def test_batch_sync_rejects_over_limit(monkeypatch):
    monkeypatch.setattr(settings, "enable_sync_source_api", True, raising=False)
    monkeypatch.setattr(settings, "sync_batch_max_items", 1, raising=False)
    db = AsyncMock()

    try:
        with patch("app.deps.load_active_library", new=AsyncMock(return_value=_lib())):
            response = _client_with_db(db).post(
                "/libraries/lib/sync-sources/crm:sync",
                json={
                    "items": [
                        {"action": "delete", "external_id": "a"},
                        {"action": "delete", "external_id": "b"},
                    ]
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 413


def test_batch_sync_supports_partial_success(monkeypatch):
    from app.schemas.v02_m4 import SyncDocumentResult

    monkeypatch.setattr(settings, "enable_sync_source_api", True, raising=False)
    monkeypatch.setattr(settings, "sync_batch_max_items", 10, raising=False)
    db = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    success = SyncDocumentResult(external_id="a", operation="created", document_id=DOC_ID)

    try:
        with patch("app.deps.load_active_library", new=AsyncMock(return_value=_lib())), patch(
            "app.api.v02_m4.sync_documents_service.upsert_sync_document",
            new=AsyncMock(side_effect=[success, ValueError("bad item")]),
        ):
            response = _client_with_db(db).post(
                "/libraries/lib/sync-sources/crm:sync",
                json={
                    "items": [
                        {"action": "upsert", "external_id": "a", "text": "A"},
                        {"action": "upsert", "external_id": "b", "text": "B"},
                    ]
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "partial"
    assert data["succeeded_count"] == 1
    assert data["failed_count"] == 1
    assert data["results"][0]["external_id"] == "a"
    assert data["errors"][0]["index"] == 1
    assert data["errors"][0]["external_id"] == "b"
    assert "bad item" in data["errors"][0]["error"]
    assert db.commit.await_count == 1
    assert db.rollback.await_count == 1


@patch("app.deps.has_permission")
def test_batch_sync_delete_item_requires_delete_permission(mock_has_permission, monkeypatch):
    from app.schemas.v02_m4 import SyncDocumentResult

    monkeypatch.setattr(settings, "enable_sync_source_api", True, raising=False)
    mock_has_permission.side_effect = lambda _user_id, _slug, action: action == "insert"
    db = AsyncMock()
    db.commit = AsyncMock()
    result = SyncDocumentResult(external_id="a", operation="deleted", document_id=DOC_ID)

    async def override_user():
        user = _user()
        user.is_superuser = False
        return user

    async def override_db():
        return db

    app.dependency_overrides[current_active_user] = override_user
    app.dependency_overrides[get_db] = override_db
    try:
        with patch("app.deps.load_active_library", new=AsyncMock(return_value=_lib())), patch(
            "app.api.v02_m4.sync_documents_service.delete_sync_document",
            new=AsyncMock(return_value=result),
        ) as delete_document:
            response = TestClient(app).post(
                "/libraries/lib/sync-sources/crm:sync",
                json={"items": [{"action": "delete", "external_id": "a"}]},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    delete_document.assert_not_awaited()
    db.commit.assert_not_awaited()


def test_sync_upsert_create_integrity_race_is_idempotent():
    from app.schemas.v02_m4 import SyncDocumentUpsertRequest
    from app.services.sync_documents import content_hash, upsert_sync_document

    source = SyncSource(
        id=SOURCE_ID,
        library_id=LIB_ID,
        source_key="crm",
        display_name="CRM",
        status="active",
    )
    winner = Document(
        id=DOC_ID,
        library_id=LIB_ID,
        sync_source_id=SOURCE_ID,
        external_id="contract-1",
        title="Contract",
        content_hash=content_hash("same text"),
        current_revision=1,
        latest_revision_id=REV_ID,
        status="pending",
    )
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_result(first=source), _result(first=None), _result(first=winner)])
    db.add = MagicMock()
    db.expunge = MagicMock()
    db.flush = AsyncMock(side_effect=IntegrityError("insert", {}, Exception("duplicate")))
    nested = MagicMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    nested.__aexit__ = AsyncMock(return_value=False)
    db.begin_nested = MagicMock(return_value=nested)

    result = asyncio.run(
        upsert_sync_document(
            db,
            _lib(),
            "crm",
            SyncDocumentUpsertRequest(external_id="contract-1", title="Contract", text="same text"),
            created_by=USER_ID,
        )
    )

    assert result.operation == "unchanged"
    assert result.document_id == DOC_ID
    assert db.execute.await_count == 3


def test_batch_sync_upsert_preserves_omitted_folder_path(monkeypatch):
    from app.schemas.v02_m4 import SyncDocumentResult

    monkeypatch.setattr(settings, "enable_sync_source_api", True, raising=False)
    db = AsyncMock()
    db.commit = AsyncMock()
    seen_fields = []

    async def fake_upsert(_db, _lib, _source_key, payload, *, created_by):
        seen_fields.append(set(payload.model_fields_set))
        return SyncDocumentResult(external_id=payload.external_id, operation="unchanged", document_id=DOC_ID)

    try:
        with patch("app.deps.load_active_library", new=AsyncMock(return_value=_lib())), patch(
            "app.api.v02_m4.sync_documents_service.upsert_sync_document",
            new=AsyncMock(side_effect=fake_upsert),
        ):
            response = _client_with_db(db).post(
                "/libraries/lib/sync-sources/crm:sync",
                json={"items": [{"action": "upsert", "external_id": "a", "text": "A"}]},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert seen_fields
    assert "folder_path" not in seen_fields[0]


def _ready_doc_revision(*, revision_id=REV_ID, status="ready", normalized_text="before quote after"):
    doc = Document(
        id=DOC_ID,
        library_id=LIB_ID,
        title="Doc",
        content_hash="hash",
        current_revision=2,
        current_revision_id=REV_ID,
        latest_revision_id=revision_id,
        status="ready",
    )
    revision = DocumentRevision(
        id=revision_id,
        document_id=DOC_ID,
        library_id=LIB_ID,
        revision_no=2,
        title="Doc",
        content_hash="hash",
        normalized_text=normalized_text,
        parser_name="legacy",
        parser_version="v0.2",
        chunking_strategy="text",
        chunking_strategy_version="v0.2",
        status=status,
    )
    return doc, revision


def test_evidence_detail_defaults_to_current_ready_revision():
    from app.services.evidence_read import get_evidence_detail

    doc, revision = _ready_doc_revision()
    evidence = EvidenceUnit(
        id=EVIDENCE_ID,
        library_id=LIB_ID,
        document_id=DOC_ID,
        document_revision_id=REV_ID,
        evidence_kind="chunk",
        source_start=7,
        source_end=12,
        text_quote="quote",
        status="active",
    )
    db = AsyncMock()

    async def get_model(model, ident):
        if model is EvidenceUnit and ident == EVIDENCE_ID:
            return evidence
        if model is Document and ident == DOC_ID:
            return doc
        if model is DocumentRevision and ident == REV_ID:
            return revision
        return None

    db.get = AsyncMock(side_effect=get_model)

    detail = asyncio.run(get_evidence_detail(db, _lib(), EVIDENCE_ID))

    assert detail.id == EVIDENCE_ID
    assert detail.document_revision_id == REV_ID
    assert detail.text_window == "before quote after"
    assert detail.source_start == 7
    assert detail.source_end == 12


def test_evidence_detail_requires_explicit_revision_for_historical():
    from app.services.evidence_read import get_evidence_detail

    doc, revision = _ready_doc_revision(revision_id=OLD_REV_ID, status="superseded")
    evidence = EvidenceUnit(
        id=EVIDENCE_ID,
        library_id=LIB_ID,
        document_id=DOC_ID,
        document_revision_id=OLD_REV_ID,
        evidence_kind="chunk",
        source_start=0,
        source_end=5,
        text_quote="old",
        status="active",
    )
    db = AsyncMock()

    async def get_model(model, ident):
        if model is EvidenceUnit and ident == EVIDENCE_ID:
            return evidence
        if model is Document and ident == DOC_ID:
            return doc
        if model is DocumentRevision and ident == OLD_REV_ID:
            return revision
        return None

    db.get = AsyncMock(side_effect=get_model)

    with pytest.raises(LookupError, match="explicit revision"):
        asyncio.run(get_evidence_detail(db, _lib(), EVIDENCE_ID))

    detail = asyncio.run(get_evidence_detail(db, _lib(), EVIDENCE_ID, revision_id=OLD_REV_ID))
    assert detail.document_revision_id == OLD_REV_ID


def test_chunk_source_defaults_to_current_ready_revision():
    from app.services.evidence_read import get_chunk_source

    doc, revision = _ready_doc_revision(normalized_text="alpha beta gamma")
    chunk = Chunk(
        id=CHUNK_ID,
        document_id=DOC_ID,
        library_id=LIB_ID,
        document_revision_id=REV_ID,
        evidence_id=EVIDENCE_ID,
        seq=3,
        chunk_kind="text",
        text="beta",
        token_count=4,
        source_start=6,
        source_end=10,
    )
    evidence = EvidenceUnit(
        id=EVIDENCE_ID,
        library_id=LIB_ID,
        document_id=DOC_ID,
        document_revision_id=REV_ID,
        evidence_kind="chunk",
        source_start=6,
        source_end=10,
        text_quote="beta",
        status="active",
    )
    db = AsyncMock()

    async def get_model(model, ident):
        if model is Chunk and ident == CHUNK_ID:
            return chunk
        if model is EvidenceUnit and ident == EVIDENCE_ID:
            return evidence
        if model is Document and ident == DOC_ID:
            return doc
        if model is DocumentRevision and ident == REV_ID:
            return revision
        return None

    db.get = AsyncMock(side_effect=get_model)

    source = asyncio.run(get_chunk_source(db, _lib(), CHUNK_ID))

    assert source.chunk_id == CHUNK_ID
    assert source.document_revision_id == REV_ID
    assert source.text_window == "alpha beta gamma"
    assert source.source_start == 6
    assert source.source_end == 10


def test_chunk_source_ignores_mismatched_evidence_offsets():
    from app.services.evidence_read import get_chunk_source

    doc, revision = _ready_doc_revision(normalized_text="alpha beta gamma")
    chunk = Chunk(
        id=CHUNK_ID,
        document_id=DOC_ID,
        library_id=LIB_ID,
        document_revision_id=REV_ID,
        evidence_id=EVIDENCE_ID,
        seq=3,
        chunk_kind="text",
        text="beta",
        token_count=4,
        source_start=6,
        source_end=10,
    )
    evidence = EvidenceUnit(
        id=EVIDENCE_ID,
        library_id=LIB_ID,
        document_id=DOC_ID,
        document_revision_id=OLD_REV_ID,
        evidence_kind="chunk",
        source_start=0,
        source_end=5,
        text_quote="alpha",
        status="active",
    )
    db = AsyncMock()

    async def get_model(model, ident):
        if model is Chunk and ident == CHUNK_ID:
            return chunk
        if model is EvidenceUnit and ident == EVIDENCE_ID:
            return evidence
        if model is Document and ident == DOC_ID:
            return doc
        if model is DocumentRevision and ident == REV_ID:
            return revision
        return None

    db.get = AsyncMock(side_effect=get_model)

    source = asyncio.run(get_chunk_source(db, _lib(), CHUNK_ID))

    assert source.source_start == 6
    assert source.source_end == 10
    assert source.text_window == "alpha beta gamma"


@patch("app.deps.has_permission")
def test_evidence_api_requires_read_permission(mock_has_perm):
    mock_has_perm.return_value = False
    db = AsyncMock()

    async def override_user():
        user = _user()
        user.is_superuser = False
        return user

    async def override_db():
        return db

    app.dependency_overrides[current_active_user] = override_user
    app.dependency_overrides[get_db] = override_db
    try:
        with patch("app.deps.load_active_library", new=AsyncMock(return_value=_lib())):
            response = TestClient(app).get(f"/libraries/lib/evidence/{EVIDENCE_ID}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
