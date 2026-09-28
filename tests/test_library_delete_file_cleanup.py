"""Tests for library-level file resource cleanup on library deletion."""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.cleanup_outbox import EVENT_DELETE_FILE_RESOURCES, CleanupOutbox
from app.services import cleanup as cleanup_service


def test_enqueue_delete_library_file_resources_creates_outbox_row():
    """enqueue_delete_library_file_resources should insert a row with library_id payload."""
    captured = []

    async def fake_execute(stmt):
        captured.append(stmt)

    db = AsyncMock()
    db.execute = fake_execute

    library = MagicMock()
    library.id = uuid.uuid4()
    library.qdrant_collection = "test_collection"

    asyncio.run(cleanup_service.enqueue_delete_library_file_resources(db, library))

    assert len(captured) == 1


def test_execute_event_library_level_file_resources():
    """execute_event should handle library-level file resource cleanup."""
    library_id = uuid.uuid4()
    row = CleanupOutbox(
        id=uuid.uuid4(),
        event_type=EVENT_DELETE_FILE_RESOURCES,
        library_id=library_id,
        collection_name="test_collection",
        payload={"library_id": str(library_id)},
        idempotency_key=f"delete-library-file-resources:{library_id}",
        status="processing",
    )

    mock_resource = MagicMock()
    mock_resource.storage_provider = "local"

    # First call returns one resource; second call returns empty → loop exits.
    result_with_resource = MagicMock()
    result_with_resource.scalars.return_value.all.return_value = [mock_resource]
    result_empty = MagicMock()
    result_empty.scalars.return_value.all.return_value = []

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[result_with_resource, result_empty])

    with patch(
        "app.services.cleanup.build_object_storage_adapter"
    ) as mock_build, patch(
        "app.services.cleanup.delete_file_resource_object", new_callable=AsyncMock
    ) as mock_delete:
        mock_adapter = MagicMock()
        mock_build.return_value = mock_adapter
        asyncio.run(cleanup_service.execute_event(row, db=db))

    mock_delete.assert_awaited_once_with(
        adapter=mock_adapter, resource=mock_resource, db=db
    )
