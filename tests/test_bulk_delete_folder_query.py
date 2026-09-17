from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.api.documents import bulk_delete_documents, query_library
from app.models.document import Document
from app.models.folder import Folder
from app.models.library import Library
from app.schemas.documents import BulkDeleteDocumentsResponse, QueryRequest, QueryResponse
from app.services.qdrant import document_id_any_filter


LIBRARY_ID = uuid.uuid4()


def _library() -> Library:
    return Library(
        id=LIBRARY_ID,
        slug="library",
        name="Library",
        qdrant_collection="library_collection",
        embedding_model="model",
        embedding_dim=8,
        chunk_size=1000,
        chunk_overlap=100,
        lifecycle_mode="managed",
        index_state="ready",
    )


def _result(*, rows=None, scalar_rows=None):
    result = MagicMock()
    result.scalars.return_value.all.return_value = list(rows or [])
    result.scalars.return_value.first.return_value = (
        scalar_rows[0] if scalar_rows else None
    )
    return result


def test_query_request_folder_id_is_optional_and_qdrant_filter_matches_any_ids():
    request = QueryRequest(query="term")
    assert request.folder_id is None
    assert document_id_any_filter([uuid.UUID(int=1), "doc-2"]) == {
        "must": [{"key": "document_id", "match": {"any": [str(uuid.UUID(int=1)), "doc-2"]}}]
    }


def test_bulk_delete_soft_deletes_active_documents_and_enqueues_cleanup():
    library = _library()
    documents = [
        Document(id=uuid.uuid4(), library_id=library.id, content_hash="a", status="ready"),
        Document(id=uuid.uuid4(), library_id=library.id, content_hash="b", status="processing"),
    ]
    document_ids = [document.id for document in documents]
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result(rows=document_ids),
        _result(),
        _result(),
        _result(rows=[]),
    ])
    db.commit = AsyncMock()
    cleanup = AsyncMock()

    async def run():
        with patch("app.api.documents._lock_writable", new=AsyncMock(return_value=library)), patch(
            "app.api.documents.cleanup_service.enqueue_delete_document", new=cleanup
        ):
            return await bulk_delete_documents(lib=library, db=db)

    response = asyncio.run(run())

    assert isinstance(response, BulkDeleteDocumentsResponse)
    assert response.deleted_count == 2
    assert response.cleanup_task_count == 2
    assert cleanup.await_count == 2
    assert [call.args[2] for call in cleanup.await_args_list] == [doc.id for doc in documents]
    db.commit.assert_awaited_once()


def test_bulk_delete_is_idempotent_when_no_active_documents():
    library = _library()
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result())
    db.commit = AsyncMock()
    cleanup = AsyncMock()

    async def run():
        with patch("app.api.documents._lock_writable", new=AsyncMock(return_value=library)), patch(
            "app.api.documents.cleanup_service.enqueue_delete_document", new=cleanup
        ):
            return await bulk_delete_documents(lib=library, db=db)

    response = asyncio.run(run())

    assert response == BulkDeleteDocumentsResponse(deleted_count=0, cleanup_task_count=0)
    cleanup.assert_not_awaited()
    db.commit.assert_awaited_once()


def test_folder_query_pushes_descendant_document_ids_before_recall():
    library = _library()
    parent = Folder(id=uuid.uuid4(), library_id=library.id, name="root", path="/root")
    child = Folder(
        id=uuid.uuid4(),
        library_id=library.id,
        parent_id=parent.id,
        name="child",
        path="/root/child",
    )
    document_ids = [uuid.uuid4(), uuid.uuid4()]
    db = AsyncMock()
    db.get = AsyncMock(return_value=parent)
    db.execute = AsyncMock(side_effect=[
        _result(rows=[parent.id, child.id]),
        _result(rows=document_ids),
    ])
    embedding = AsyncMock(return_value=[0.1] * library.embedding_dim)
    recall = AsyncMock(return_value=[])
    enrichment = SimpleNamespace(enabled=False, parsed={"extra_columns": []}, texts=[], rows=[])
    rerank = AsyncMock(return_value=([], {}))

    async def run():
        with patch("app.api.documents.embedding.embed_one", new=embedding), patch(
            "app.api.documents.retrieval_svc._recall_visible", new=recall
        ), patch(
            "app.api.documents.source_enrichment.enrich_payloads", new=AsyncMock(return_value=enrichment)
        ), patch("app.api.documents.rerank_svc.rank_candidates", new=rerank):
            return await query_library(
                body=QueryRequest(query="term", folder_id=parent.id),
                lib=library,
                db=db,
            )

    response = asyncio.run(run())

    assert isinstance(response, QueryResponse)
    assert response.results == []
    embedding.assert_awaited_once()
    payload_filter = recall.await_args.kwargs["payload_filter"]
    assert payload_filter == document_id_any_filter(document_ids)


def test_empty_folder_query_returns_before_embedding_or_recall():
    library = _library()
    folder = Folder(id=uuid.uuid4(), library_id=library.id, name="empty", path="/empty")
    db = AsyncMock()
    db.get = AsyncMock(return_value=folder)
    db.execute = AsyncMock(side_effect=[_result(rows=[folder.id]), _result(rows=[])])
    embedding = AsyncMock()
    recall = AsyncMock()

    async def run():
        with patch("app.api.documents.embedding.embed_one", new=embedding), patch(
            "app.api.documents.retrieval_svc._recall_visible", new=recall
        ):
            return await query_library(
                body=QueryRequest(query="term", folder_id=folder.id),
                lib=library,
                db=db,
            )

    response = asyncio.run(run())

    assert response == QueryResponse(results=[])
    embedding.assert_not_awaited()
    recall.assert_not_awaited()


def test_cross_library_folder_query_is_rejected():
    library = _library()
    folder = Folder(id=uuid.uuid4(), library_id=uuid.uuid4(), name="other", path="/other")
    db = AsyncMock()
    db.get = AsyncMock(return_value=folder)

    async def run():
        return await query_library(
            body=QueryRequest(query="term", folder_id=folder.id),
            lib=library,
            db=db,
        )

    with patch("app.api.documents.embedding.embed_one", new=AsyncMock()):
        try:
            asyncio.run(run())
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 404
        else:
            raise AssertionError("cross-library folder must be rejected")
