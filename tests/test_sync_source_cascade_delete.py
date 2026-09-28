"""Tests for sync source cascade deletion of associated documents."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_source(library_id=None, source_key="test-source"):
    source = MagicMock()
    source.id = uuid.uuid4()
    source.library_id = library_id or uuid.uuid4()
    source.source_key = source_key
    source.status = "active"
    source.deleted_at = None
    return source


def _make_library(library_id=None, slug="testlib"):
    lib = MagicMock()
    lib.id = library_id or uuid.uuid4()
    lib.slug = slug
    lib.qdrant_collection = f"col_{slug}"
    return lib


def test_delete_sync_source_cascades_documents():
    """Deleting a sync source should soft-delete all its active documents."""
    from app.services import sync_sources

    library = _make_library()
    source = _make_source(library_id=library.id)
    doc_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]

    # Track which document IDs have been returned from the document query.
    # After the first batch, return empty to exit the loop.
    document_batches = iter([doc_ids, []])

    doc_result_with_data = MagicMock()
    doc_result_with_data.scalars.return_value.all.return_value = doc_ids
    doc_result_empty = MagicMock()
    doc_result_empty.scalars.return_value.all.return_value = []
    generic_result = MagicMock()

    # Determine result by inspecting the statement string.
    async def smart_execute(stmt):
        stmt_str = str(getattr(stmt, "compile", lambda: stmt)())
        # Document ID select queries contain "documents" and use "deleted_at"
        if hasattr(stmt, "whereclause") or "Document" in type(stmt).__name__:
            pass
        # Use a simpler heuristic: track call count per statement type
        return generic_result

    db = AsyncMock()

    # Use side_effect with a stateful callable to handle the various execute calls:
    # 1. FOR UPDATE lock on sync_source → generic
    # 2. SELECT document IDs → batch 1 (3 docs)
    # 3. UPDATE documents → generic
    # 4. UPDATE embedding_jobs → generic
    # 5..7. enqueue_delete_document calls (each does 1+ internal executes)
    # 8. flush
    # 9. SELECT document IDs → batch 2 (empty → break)
    # 10. flush (final)
    #
    # Since enqueue_delete_document is patched out, the actual sequence of
    # db.execute calls from delete_sync_source itself is:
    #   1. select SyncSource FOR UPDATE (lock)
    #   2. select Document.id (batch 1 → doc_ids)
    #   3. update Document (soft-delete)
    #   4. update EmbeddingJob (supersede)
    #   5. select Document.id (batch 2 → empty)
    call_idx = {"n": 0}

    async def fake_execute(stmt):
        call_idx["n"] += 1
        n = call_idx["n"]
        if n == 2:  # first document select
            return doc_result_with_data
        if n == 5:  # second document select
            return doc_result_empty
        return generic_result

    db.execute = fake_execute

    enqueue_calls = []

    async def fake_enqueue(db_, lib_, doc_id):
        enqueue_calls.append(doc_id)

    with patch.object(sync_sources, "require_sync_source", new_callable=AsyncMock, return_value=source):
        with patch("app.services.cleanup.enqueue_delete_document", new=fake_enqueue):
            asyncio.run(sync_sources.delete_sync_source(db, library, "test-source"))

    # Source should be marked deleted
    assert source.status == "deleted"
    assert source.deleted_at is not None

    # All 3 documents should have been enqueued for cleanup
    assert len(enqueue_calls) == 3
    assert set(enqueue_calls) == set(doc_ids)


def test_delete_sync_source_no_documents():
    """Deleting a sync source with no documents should just delete the source."""
    from app.services import sync_sources

    library = _make_library()
    source = _make_source(library_id=library.id)

    doc_result_empty = MagicMock()
    doc_result_empty.scalars.return_value.all.return_value = []

    db = AsyncMock()
    db.execute = AsyncMock(return_value=doc_result_empty)

    with patch.object(sync_sources, "require_sync_source", new_callable=AsyncMock, return_value=source):
        asyncio.run(sync_sources.delete_sync_source(db, library, "test-source"))

    assert source.status == "deleted"
    assert source.deleted_at is not None
