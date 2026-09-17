"""#7 Cleanup 服务/worker 纯逻辑单测：execute_event 派发 + 指数退避。"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.models.cleanup_outbox import (
    EVENT_DELETE_COLLECTION,
    EVENT_DELETE_DOCUMENT_ALL,
    EVENT_DELETE_DOCUMENT_BEFORE_REVISION,
    EVENT_DELETE_FILE_RESOURCES,
)
from app.services import cleanup as cleanup_svc
from app.workers import cleanup as cleanup_worker


def _row(event_type, **kw):
    base = dict(collection_name="lib_c", document_id=uuid.uuid4(), target_revision=None)
    base.update(kw)
    return NS(event_type=event_type, **base)


def test_execute_event_dispatch_delete_all():
    row = _row(EVENT_DELETE_DOCUMENT_ALL)
    with patch("app.services.qdrant.delete_points_by_document_id", new_callable=AsyncMock) as f, \
         patch("app.services.qdrant.delete_points_before_revision", new_callable=AsyncMock) as g, \
         patch("app.services.qdrant.delete_collection", new_callable=AsyncMock) as h:
        asyncio.run(cleanup_svc.execute_event(row))
        f.assert_awaited_once_with("lib_c", str(row.document_id))
        g.assert_not_called()
        h.assert_not_called()


def test_execute_event_dispatch_before_revision():
    row = _row(EVENT_DELETE_DOCUMENT_BEFORE_REVISION, target_revision=5)
    with patch("app.services.qdrant.delete_points_before_revision", new_callable=AsyncMock) as g:
        asyncio.run(cleanup_svc.execute_event(row))
        g.assert_awaited_once_with("lib_c", str(row.document_id), 5)


def test_execute_event_dispatch_delete_collection():
    row = _row(EVENT_DELETE_COLLECTION)
    with patch("app.services.qdrant.delete_collection", new_callable=AsyncMock) as h:
        asyncio.run(cleanup_svc.execute_event(row))
        h.assert_awaited_once_with("lib_c")


def test_execute_event_deletes_file_resource_through_its_recorded_provider():
    resource_id = uuid.uuid4()
    resource = NS(id=resource_id, storage_provider="local", storage_status="available")

    class _Rows:
        def scalars(self):
            return self

        def all(self):
            return [resource]

    db = NS(execute=AsyncMock(return_value=_Rows()))
    adapter = object()
    row = _row(
        EVENT_DELETE_FILE_RESOURCES,
        payload={"document_id": str(uuid.uuid4())},
    )
    with (
        patch("app.services.cleanup.build_object_storage_adapter", return_value=adapter) as build,
        patch(
            "app.services.cleanup.delete_file_resource_object",
            new_callable=AsyncMock,
        ) as delete,
    ):
        asyncio.run(cleanup_svc.execute_event(row, db=db))

    build.assert_called_once_with(provider="local")
    delete.assert_awaited_once_with(adapter=adapter, resource=resource, db=db)


def test_execute_event_deletes_one_storage_only_resource_by_its_own_id():
    resource_id = uuid.uuid4()
    library_id = uuid.uuid4()
    resource = NS(id=resource_id, storage_provider="local", storage_status="deleting")

    class _Rows:
        def scalars(self):
            return self

        def all(self):
            return [resource]

    db = NS(execute=AsyncMock(return_value=_Rows()))
    adapter = object()
    row = _row(
        EVENT_DELETE_FILE_RESOURCES,
        library_id=library_id,
        payload={"file_resource_id": str(resource_id)},
    )
    with (
        patch("app.services.cleanup.build_object_storage_adapter", return_value=adapter) as build,
        patch(
            "app.services.cleanup.delete_file_resource_object",
            new_callable=AsyncMock,
        ) as delete,
    ):
        asyncio.run(cleanup_svc.execute_event(row, db=db))

    build.assert_called_once_with(provider="local")
    delete.assert_awaited_once_with(adapter=adapter, resource=resource, db=db)


def test_document_delete_enqueues_file_resource_cleanup():
    library = NS(id=uuid.uuid4(), qdrant_collection="lib_c")
    document_id = uuid.uuid4()
    db = NS()
    with (
        patch(
            "app.services.graph_evidence.mark_document_graph_evidence_stale",
            new_callable=AsyncMock,
            return_value=NS(
                stale_entity_mentions=0,
                stale_relation_evidence=0,
                stale_relations=0,
            ),
        ),
        patch(
            "app.services.graph_extraction_purge.purge_document_graph_extraction_payloads",
            new_callable=AsyncMock,
        ),
        patch("app.services.cleanup._enqueue", new_callable=AsyncMock) as enqueue,
    ):
        asyncio.run(cleanup_svc.enqueue_delete_document(db, library, document_id))

    assert any(
        call.kwargs["event_type"] == EVENT_DELETE_FILE_RESOURCES
        and call.kwargs["document_id"] == document_id
        for call in enqueue.await_args_list
    )


def test_document_delete_queues_one_delayed_graph_refresh_per_degraded_publication():
    library = NS(id=uuid.uuid4(), qdrant_collection="lib_c")
    document_id = uuid.uuid4()
    publication_id = uuid.uuid4()

    class _Rows:
        def scalars(self):
            return self

        def all(self):
            return [publication_id]

    db = NS(execute=AsyncMock(return_value=_Rows()))
    lifecycle = NS(
        stale_entity_mentions=1,
        stale_relation_evidence=0,
        stale_relations=0,
    )
    with (
        patch(
            "app.services.graph_evidence.mark_document_graph_evidence_stale",
            new_callable=AsyncMock,
            return_value=lifecycle,
        ),
        patch(
            "app.services.graph_extraction_purge.purge_document_graph_extraction_payloads",
            new_callable=AsyncMock,
        ),
        patch("app.services.cleanup._enqueue", new_callable=AsyncMock) as enqueue,
    ):
        asyncio.run(cleanup_svc.enqueue_delete_document(db, library, document_id))

    refreshes = [
        call.kwargs
        for call in enqueue.await_args_list
        if call.kwargs["event_type"] == "refresh_graph_publication"
    ]
    assert len(refreshes) == 1
    refresh = refreshes[0]
    assert refresh["payload"] == {"publication_id": str(publication_id)}
    assert refresh["idempotency_key"] == f"refresh-graph-publication:{publication_id}"
    assert refresh["available_at"] > datetime.now(timezone.utc)


def test_graph_refresh_republishes_from_graph_records_without_reextracting_documents():
    library_id = uuid.uuid4()
    source_publication_id = uuid.uuid4()
    replacement_publication_id = uuid.uuid4()
    library = NS(id=library_id)
    source = NS(
        id=source_publication_id,
        library_id=library_id,
        ontology_version_id=uuid.uuid4(),
        status="degraded",
        include_drafts=False,
    )
    row = _row(
        "refresh_graph_publication",
        id=uuid.uuid4(),
        library_id=library_id,
        payload={"publication_id": str(source_publication_id)},
        idempotency_key=f"refresh-graph-publication:{source_publication_id}",
    )
    db = NS(get=AsyncMock(side_effect=[library, source]), commit=AsyncMock())
    planned = NS(
        publication=NS(id=replacement_publication_id),
        manifest_hash="a" * 64,
    )
    with (
        patch(
            "app.services.graph_publication_planner.plan_graph_publication",
            new_callable=AsyncMock,
            return_value=planned,
        ) as plan,
        patch(
            "app.services.graph_publication_activation.activate_graph_publication",
            new_callable=AsyncMock,
        ) as activate,
    ):
        asyncio.run(cleanup_svc.execute_event(row, db=db))

    plan.assert_awaited_once_with(
        db,
        library,
        ontology_version_id=source.ontology_version_id,
        source_mode="manual_plan",
        include_drafts=False,
        idempotency_key=f"{row.idempotency_key}:plan",
        expected_parent_publication_id=source_publication_id,
        allow_explicit_disabled=True,
        plan_options={"refresh_reason": "source_document_deleted"},
    )
    db.commit.assert_awaited_once()
    activate.assert_awaited_once_with(
        db,
        replacement_publication_id,
        expected_manifest_hash="a" * 64,
        command_idempotency_key=f"{row.idempotency_key}:activate",
    )


def test_backoff_increases_and_caps(monkeypatch):
    monkeypatch.setattr(settings, "cleanup_backoff_base_seconds", 5.0)
    monkeypatch.setattr(settings, "cleanup_backoff_max_seconds", 60.0)
    # 退避（不含 jitter）随 attempt 递增并封顶：base*2^(n-1)，n 大时 == cap
    lows = [cleanup_worker._backoff_seconds(n) - 0 for n in (1, 2, 3)]
    # 去掉 jitter 的下界：base*2^(n-1)
    assert lows[0] >= 5.0 and lows[1] >= 10.0 and lows[2] >= 20.0
    big = cleanup_worker._backoff_seconds(20)
    assert big <= 60.0 + 5.0          # cap + 最多一个 base 的 jitter
