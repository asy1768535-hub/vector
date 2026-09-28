"""Cleanup outbox 服务（#7 §4.4 / §8）：入队（幂等）+ 单条执行（幂等删 Qdrant）。

入队在删除/更新的同一事务内调用（不 commit，由调用方提交）。执行由 Cleanup Worker 调用。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cleanup_outbox import (
    EVENT_DELETE_COLLECTION,
    EVENT_DELETE_DOCUMENT_ALL,
    EVENT_DELETE_DOCUMENT_BEFORE_REVISION,
    EVENT_DELETE_DOCUMENT_REVISION,
    EVENT_DELETE_FILE_RESOURCES,
    EVENT_DELETE_UNPUBLISHED_REVISION_POINTS,
    EVENT_REFRESH_GRAPH_PUBLICATION,
    CleanupOutbox,
)
from app.models.document_import_job import DocumentImportJob
from app.models.file_resource import FileResource
from app.models.graph_publication import (
    GRAPH_PUBLICATION_SOURCE_MANUAL_PLAN,
    GRAPH_PUBLICATION_STATUS_DEGRADED,
    GraphPublication,
)
from app.models.library import Library
from app.services.file_resources import delete_file_resource_object
from app.services.object_storage import build_object_storage_adapter

log = logging.getLogger(__name__)

GRAPH_PUBLICATION_REFRESH_DELAY = timedelta(minutes=2)


async def _enqueue(db: AsyncSession, *, event_type, library_id, collection_name,
                   idempotency_key, document_id=None, target_revision=None,
                   payload=None, available_at: datetime | None = None) -> None:
    """插一条 outbox（ON CONFLICT(idempotency_key) DO NOTHING → 幂等，重复删除不产生重复任务）。

    不提交：与触发它的删除/更新同事务，由调用方一起 commit（事务性 outbox）。
    """
    values = {
        "id": uuid.uuid4(),
        "event_type": event_type,
        "library_id": library_id,
        "document_id": document_id,
        "collection_name": collection_name,
        "target_revision": target_revision,
        "payload": payload,
        "idempotency_key": idempotency_key,
        "status": "pending",
    }
    if available_at is not None:
        values["available_at"] = available_at
    stmt = pg_insert(CleanupOutbox).values(**values).on_conflict_do_nothing(
        index_elements=["idempotency_key"]
    )
    await db.execute(stmt)


async def enqueue_delete_document(db, library, document_id) -> None:
    from app.services import graph_evidence, graph_extraction_purge

    lifecycle = await graph_evidence.mark_document_graph_evidence_stale(
        db, library, document_id=document_id
    )
    await graph_extraction_purge.purge_document_graph_extraction_payloads(
        db,
        library_id=library.id,
        document_id=document_id,
    )
    if (
        lifecycle.stale_entity_mentions
        or lifecycle.stale_relation_evidence
        or lifecycle.stale_relations
    ):
        publications = list(
            (
                await db.execute(
                    select(GraphPublication.id).where(
                        GraphPublication.library_id == library.id,
                        GraphPublication.status == GRAPH_PUBLICATION_STATUS_DEGRADED,
                    )
                )
            ).scalars().all()
        )
        for publication_id in publications:
            await _enqueue(
                db,
                event_type=EVENT_REFRESH_GRAPH_PUBLICATION,
                library_id=library.id,
                collection_name=library.qdrant_collection,
                payload={"publication_id": str(publication_id)},
                idempotency_key=f"refresh-graph-publication:{publication_id}",
                available_at=datetime.now(timezone.utc) + GRAPH_PUBLICATION_REFRESH_DELAY,
            )
    await _enqueue(
        db, event_type=EVENT_DELETE_DOCUMENT_ALL, library_id=library.id,
        collection_name=library.qdrant_collection, document_id=document_id,
        idempotency_key=f"delete-document:{document_id}",
    )
    await _enqueue(
        db,
        event_type=EVENT_DELETE_FILE_RESOURCES,
        library_id=library.id,
        collection_name=library.qdrant_collection,
        document_id=document_id,
        payload={"document_id": str(document_id)},
        idempotency_key=f"delete-file-resources:{document_id}",
    )


async def enqueue_delete_file_resource(db, library, file_resource_id) -> None:
    """Queue one storage-only original for provider deletion after its DB fence."""
    await _enqueue(
        db,
        event_type=EVENT_DELETE_FILE_RESOURCES,
        library_id=library.id,
        collection_name=library.qdrant_collection,
        payload={"file_resource_id": str(file_resource_id)},
        idempotency_key=f"delete-file-resource:{file_resource_id}",
    )


async def enqueue_delete_before_revision(db, library, document_id, target_revision) -> None:
    await _enqueue(
        db, event_type=EVENT_DELETE_DOCUMENT_BEFORE_REVISION, library_id=library.id,
        collection_name=library.qdrant_collection, document_id=document_id,
        target_revision=target_revision,
        idempotency_key=f"delete-before-revision:{document_id}:{target_revision}",
    )


async def enqueue_delete_document_revision(db, library, document_id, document_revision_id) -> None:
    from app.services import graph_evidence, graph_extraction_purge

    await graph_evidence.mark_document_revision_graph_evidence_stale(
        db, library, document_revision_id=document_revision_id
    )
    await graph_extraction_purge.purge_revision_graph_extraction_payloads(
        db,
        library_id=library.id,
        document_revision_id=document_revision_id,
    )
    await _enqueue(
        db, event_type=EVENT_DELETE_DOCUMENT_REVISION, library_id=library.id,
        collection_name=library.qdrant_collection, document_id=document_id,
        payload={"document_revision_id": str(document_revision_id)},
        idempotency_key=f"delete-document-revision:{document_id}:{document_revision_id}",
    )


async def enqueue_delete_unpublished_revision_points(db, library, document_id, document_revision_id) -> None:
    await _enqueue(
        db, event_type=EVENT_DELETE_UNPUBLISHED_REVISION_POINTS, library_id=library.id,
        collection_name=library.qdrant_collection, document_id=document_id,
        payload={"document_revision_id": str(document_revision_id)},
        idempotency_key=f"delete-unpublished-revision:{document_id}:{document_revision_id}",
    )


async def enqueue_delete_collection(db, library) -> None:
    await _enqueue(
        db, event_type=EVENT_DELETE_COLLECTION, library_id=library.id,
        collection_name=library.qdrant_collection,
        idempotency_key=f"delete-collection:{library.qdrant_collection}",
    )


async def enqueue_delete_library_file_resources(db, library) -> None:
    """Queue cleanup of all file resources belonging to a deleted library."""
    await _enqueue(
        db,
        event_type=EVENT_DELETE_FILE_RESOURCES,
        library_id=library.id,
        collection_name=library.qdrant_collection,
        payload={"library_id": str(library.id)},
        idempotency_key=f"delete-library-file-resources:{library.id}",
    )


async def execute_event(row: CleanupOutbox, *, db: AsyncSession | None = None) -> None:
    """Execute one idempotent cleanup event."""
    from app.services import qdrant
    if row.event_type == EVENT_REFRESH_GRAPH_PUBLICATION:
        if db is None:
            raise ValueError("graph publication refresh requires a database session")
        try:
            publication_id = uuid.UUID(str((row.payload or {}).get("publication_id")))
        except (TypeError, ValueError, AttributeError):
            raise ValueError(f"graph refresh event missing publication_id: {row.id}") from None
        library = await db.get(Library, row.library_id)
        publication = await db.get(GraphPublication, publication_id)
        if (
            library is None
            or getattr(library, "deleted_at", None) is not None
            or publication is None
            or publication.library_id != row.library_id
            or publication.status != GRAPH_PUBLICATION_STATUS_DEGRADED
        ):
            return
        from app.services.graph_publication_activation import activate_graph_publication
        from app.services.graph_publication_planner import plan_graph_publication

        planned = await plan_graph_publication(
            db,
            library,
            ontology_version_id=publication.ontology_version_id,
            source_mode=GRAPH_PUBLICATION_SOURCE_MANUAL_PLAN,
            include_drafts=publication.include_drafts,
            idempotency_key=f"{row.idempotency_key}:plan",
            expected_parent_publication_id=publication.id,
            allow_explicit_disabled=True,
            plan_options={"refresh_reason": "source_document_deleted"},
        )
        # 激活要求空闲会话：先提交仅包含图数据库事实的展示快照；失败会由 outbox 重试。
        await db.commit()
        await activate_graph_publication(
            db,
            planned.publication.id,
            expected_manifest_hash=planned.manifest_hash,
            command_idempotency_key=f"{row.idempotency_key}:activate",
        )
    elif row.event_type == EVENT_DELETE_FILE_RESOURCES:
        if db is None:
            raise ValueError("file resource cleanup requires a database session")
        payload = row.payload or {}
        file_resource_id = payload.get("file_resource_id")
        if file_resource_id is not None:
            try:
                resource_id = uuid.UUID(str(file_resource_id))
            except (TypeError, ValueError, AttributeError):
                raise ValueError(f"file resource event missing file_resource_id: {row.id}") from None
            resources = list(
                (
                    await db.execute(
                        select(FileResource)
                        .where(
                            FileResource.id == resource_id,
                            FileResource.library_id == row.library_id,
                            FileResource.storage_status != "deleted",
                        )
                        .with_for_update()
                    )
                ).scalars().all()
            )
        elif payload.get("document_id") is not None:
            try:
                document_id = uuid.UUID(str(payload["document_id"]))
            except (TypeError, ValueError, AttributeError):
                raise ValueError(f"file resource event missing document_id: {row.id}") from None
            resources = list(
                (
                    await db.execute(
                        select(FileResource)
                        .join(
                            DocumentImportJob,
                            DocumentImportJob.file_resource_id == FileResource.id,
                        )
                        .where(
                            DocumentImportJob.document_id == document_id,
                            FileResource.storage_status != "deleted",
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
        else:
            # Library-level bulk file resource cleanup (triggered by library deletion).
            # Loop in batches to handle libraries with many resources without
            # holding a single huge lock or risking the outbox event completing
            # before all resources are processed.
            while True:
                batch = list(
                    (
                        await db.execute(
                            select(FileResource)
                            .where(
                                FileResource.library_id == row.library_id,
                                FileResource.storage_status.notin_(("deleted", "storing")),
                            )
                            .with_for_update(skip_locked=True)
                            .limit(500)
                        )
                    ).scalars().all()
                )
                if not batch:
                    break
                for resource in batch:
                    adapter = build_object_storage_adapter(provider=resource.storage_provider)
                    await delete_file_resource_object(adapter=adapter, resource=resource, db=db)
            resources = []  # already handled above; skip the shared loop
        for resource in resources:
            adapter = build_object_storage_adapter(provider=resource.storage_provider)
            await delete_file_resource_object(adapter=adapter, resource=resource, db=db)
    elif row.event_type == EVENT_DELETE_DOCUMENT_ALL:
        await qdrant.delete_points_by_document_id(row.collection_name, str(row.document_id))
    elif row.event_type == EVENT_DELETE_DOCUMENT_BEFORE_REVISION:
        await qdrant.delete_points_before_revision(
            row.collection_name, str(row.document_id), int(row.target_revision))
    elif row.event_type in (EVENT_DELETE_DOCUMENT_REVISION, EVENT_DELETE_UNPUBLISHED_REVISION_POINTS):
        document_revision_id = (row.payload or {}).get("document_revision_id")
        if not document_revision_id:
            raise ValueError(f"cleanup event missing document_revision_id: {row.id}")
        await qdrant.delete_points_by_document_revision_id(row.collection_name, str(document_revision_id))
    elif row.event_type == EVENT_DELETE_COLLECTION:
        await qdrant.delete_collection(row.collection_name)
    else:
        raise ValueError(f"unknown cleanup event_type: {row.event_type}")
