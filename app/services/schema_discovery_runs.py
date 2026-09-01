"""Batch-level coordination for AI Schema discovery.

This module owns the boundary between ready imported revisions and the single
draft that all graph jobs in an import batch consume.  It never creates work
from a read projection; callers invoke it only from upload/embedding write
paths or from the graph worker coordinator.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_import_job import DocumentImportJob
from app.models.document_revision import DocumentRevision
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.schema_discovery_run import SchemaDiscoveryRun
from app.services.graph_candidate_aggregation import canonical_graph_value_hash_v1
from app.services.graph_schema_discovery import (
    DiscoveryText,
    discover_business_schema,
    persist_business_schema_draft,
)
from app.services.schema_lifecycle_read import resolve_current_ontology


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sample_discovery_texts(
    texts: Sequence[DiscoveryText],
    *,
    max_texts: int,
) -> list[DiscoveryText]:
    if max_texts < 1:
        raise ValueError("max_texts must be positive")
    if len(texts) <= max_texts:
        return list(texts)
    return [texts[index * len(texts) // max_texts] for index in range(max_texts)]


def discovery_placeholder_snapshot(*, ontology_version_id: uuid.UUID, source_hash: str) -> dict[str, Any]:
    return {
        "ontology_version_id": str(ontology_version_id),
        "schema_state": "ai_discovery_pending",
        "confirmed": False,
        "origin": "ai_discovery",
        "source_hash": source_hash,
        "entity_types": [],
        "relation_types": [],
        "relation_constraints": [],
    }


async def _ready_batch_revisions(
    db: AsyncSession,
    *,
    library_id: uuid.UUID,
    batch_id: uuid.UUID,
) -> list[tuple[Document, DocumentRevision]]:
    imports = list(
        (
            await db.execute(
                select(DocumentImportJob)
                .where(
                    DocumentImportJob.library_id == library_id,
                    DocumentImportJob.batch_id == batch_id,
                    DocumentImportJob.graph_extraction_requested.is_(True),
                    DocumentImportJob.status.notin_(("cancelled", "failed")),
                )
                .order_by(DocumentImportJob.id)
            )
        )
        .scalars()
        .all()
    )
    if not imports or any(row.document_id is None or row.document_revision_id is None for row in imports):
        return []
    pairs: list[tuple[Document, DocumentRevision]] = []
    for import_job in imports:
        result = await db.execute(
            select(Document, DocumentRevision)
            .join(DocumentRevision, DocumentRevision.id == import_job.document_revision_id)
            .where(
                Document.id == import_job.document_id,
                Document.library_id == library_id,
                Document.deleted_at.is_(None),
                Document.status == "ready",
                Document.current_revision_id == DocumentRevision.id,
                DocumentRevision.id == import_job.document_revision_id,
                DocumentRevision.status == "ready",
            )
        )
        pair = result.first()
        if pair is None:
            return []
        pairs.append(pair)
    return pairs


def _revision_source_hash(pairs: list[tuple[Document, DocumentRevision]]) -> str:
    return canonical_graph_value_hash_v1(
        {
            "revisions": [
                {"id": str(revision.id), "content_hash": revision.content_hash}
                for _document, revision in sorted(pairs, key=lambda item: str(item[1].id))
            ]
        }
    )


async def _create_draft_ontology(db: AsyncSession, library: Library, source_hash: str) -> OntologyVersion:
    version_key = "ai-discovery"
    current = await resolve_current_ontology(
        db,
        library=library,
        required=False,
    )
    version_no = (
        await db.execute(
            select(func.max(OntologyVersion.version_no)).where(
                OntologyVersion.library_id == library.id,
                OntologyVersion.version_key == version_key,
            )
        )
    ).scalar_one()
    ontology = OntologyVersion(
        library_id=library.id,
        version_key=version_key,
        version_no=int(version_no or 0) + 1,
        status="draft",
        description=f"AI Schema discovery draft for source set {source_hash[:12]}",
        origin="ai_discovery",
        confirmed=False,
        parent_version_id=current.id if current is not None else None,
    )
    db.add(ontology)
    await db.flush()
    return ontology


async def ensure_schema_discovery_run_for_batch(
    db: AsyncSession,
    *,
    library: Library,
    batch_id: uuid.UUID,
    requested_by: Any = None,
    build_mode: str | None = None,
) -> tuple[SchemaDiscoveryRun, list[GraphExtractionJob]] | None:
    """Create/reuse one run once every graph-requested batch file is ready."""

    # Library lock serializes source-set creation before the unique constraint
    # is consulted.  It also prevents two final embedder callbacks from each
    # creating a different draft ontology.
    locked_library = (
        await db.execute(
            select(Library).where(Library.id == library.id).with_for_update()
        )
    ).scalar_one_or_none()
    if locked_library is None:
        return None
    pairs = await _ready_batch_revisions(db, library_id=library.id, batch_id=batch_id)
    if not pairs:
        return None
    source_hash = _revision_source_hash(pairs)
    source_set_key = str(batch_id)
    run = (
        await db.execute(
            select(SchemaDiscoveryRun)
            .where(
                SchemaDiscoveryRun.library_id == library.id,
                SchemaDiscoveryRun.source_set_key == source_set_key,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if run is None:
        ontology_version = await _create_draft_ontology(db, locked_library, source_hash)
        run = SchemaDiscoveryRun(
            library_id=library.id,
            source_set_key=source_set_key,
            source_revision_ids=[str(revision.id) for _document, revision in pairs],
            source_hash=source_hash,
            status="queued",
            confirmation_policy=getattr(locked_library, "schema_confirmation_policy", "required"),
            ontology_version_id=ontology_version.id,
        )
        db.add(run)
        await db.flush()
    if run.source_hash != source_hash or sorted(run.source_revision_ids) != sorted(
        str(revision.id) for _document, revision in pairs
    ):
        # A replacement changes the source set.  Keep the old run immutable;
        # the caller must use a new batch id or explicit retry workflow.
        raise ValueError("schema discovery source set changed after run creation")

    ontology_version = await db.get(OntologyVersion, run.ontology_version_id)
    if ontology_version is None:
        raise ValueError("schema discovery ontology is missing")
    placeholder = discovery_placeholder_snapshot(
        ontology_version_id=ontology_version.id,
        source_hash=run.source_hash,
    )
    placeholder_hash = canonical_graph_value_hash_v1(placeholder)
    from app.services.graph_extraction_jobs import create_graph_extraction_job

    jobs: list[GraphExtractionJob] = []
    for document, revision in pairs:
        existing = (
            await db.execute(
                select(GraphExtractionJob)
                .where(
                    GraphExtractionJob.schema_discovery_run_id == run.id,
                    GraphExtractionJob.document_revision_id == revision.id,
                )
                .order_by(GraphExtractionJob.created_at)
                .limit(1)
            )
        ).scalars().first()
        if existing is not None:
            jobs.append(existing)
            continue
        jobs.append(
            await create_graph_extraction_job(
                db,
                library=library,
                document=document,
                revision=revision,
                trigger_type="revision_published",
                execution_mode="production",
                requested_by=requested_by,
                idempotency_key=None,
                build_mode=build_mode,
                ontology_version=ontology_version,
                ontology_snapshot=placeholder,
                ontology_snapshot_hash=placeholder_hash,
                schema_discovery_run_id=run.id,
                waiting_schema=True,
            )
        )
    return run, jobs


async def ensure_schema_discovery_run_for_revision(
    db: AsyncSession,
    *,
    library: Library,
    document: Document,
    revision: DocumentRevision,
    requested_by: Any = None,
    build_mode: str | None = None,
) -> tuple[SchemaDiscoveryRun, GraphExtractionJob]:
    """Create one explicit discovery run for a manually submitted revision.

    Upload batches use ``ensure_schema_discovery_run_for_batch``.  The manual
    endpoint has no import batch, so it gets a one-revision source set with the
    same immutable-run and waiting-job semantics.
    """

    locked_library = (
        await db.execute(
            select(Library).where(Library.id == library.id).with_for_update()
        )
    ).scalar_one_or_none()
    if locked_library is None:
        raise ValueError("library_not_found")
    if (
        document.library_id != library.id
        or document.deleted_at is not None
        or document.status != "ready"
        or document.current_revision_id != revision.id
        or revision.library_id != library.id
        or revision.document_id != document.id
        or revision.status != "ready"
    ):
        raise ValueError("revision_not_ready")

    source_hash = _revision_source_hash([(document, revision)])
    source_set_key = f"revision:{revision.id}"
    run = (
        await db.execute(
            select(SchemaDiscoveryRun)
            .where(
                SchemaDiscoveryRun.library_id == library.id,
                SchemaDiscoveryRun.source_set_key == source_set_key,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if run is None:
        ontology_version = await _create_draft_ontology(db, locked_library, source_hash)
        run = SchemaDiscoveryRun(
            library_id=library.id,
            source_set_key=source_set_key,
            source_revision_ids=[str(revision.id)],
            source_hash=source_hash,
            status="queued",
            confirmation_policy=getattr(locked_library, "schema_confirmation_policy", "required"),
            ontology_version_id=ontology_version.id,
        )
        db.add(run)
        await db.flush()
    if run.source_hash != source_hash or run.source_revision_ids != [str(revision.id)]:
        raise ValueError("schema_discovery_source_set_changed")

    ontology_version = await db.get(OntologyVersion, run.ontology_version_id)
    if ontology_version is None:
        raise ValueError("schema_discovery_ontology_missing")
    snapshot = run.ontology_snapshot or discovery_placeholder_snapshot(
        ontology_version_id=ontology_version.id,
        source_hash=run.source_hash,
    )
    snapshot_hash = run.ontology_snapshot_hash or canonical_graph_value_hash_v1(snapshot)
    from app.services.graph_extraction_jobs import create_graph_extraction_job

    existing = (
        await db.execute(
            select(GraphExtractionJob)
            .where(
                GraphExtractionJob.schema_discovery_run_id == run.id,
                GraphExtractionJob.document_revision_id == revision.id,
            )
            .order_by(GraphExtractionJob.created_at)
            .limit(1)
        )
    ).scalars().first()
    if existing is not None:
        return run, existing
    job = await create_graph_extraction_job(
        db,
        library=library,
        document=document,
        revision=revision,
        trigger_type="manual",
        execution_mode="production",
        requested_by=requested_by,
        idempotency_key=None,
        build_mode=build_mode,
        ontology_version=ontology_version,
        ontology_snapshot=snapshot,
        ontology_snapshot_hash=snapshot_hash,
        schema_discovery_run_id=run.id,
        waiting_schema=run.status != "succeeded",
    )
    return run, job


async def _current_run_texts(
    db: AsyncSession,
    run: SchemaDiscoveryRun,
) -> tuple[list[DiscoveryText], str]:
    revision_ids = [uuid.UUID(value) for value in run.source_revision_ids]
    result = await db.execute(
        select(Chunk.id, Chunk.text)
        .join(Document, Document.id == Chunk.document_id)
        .join(DocumentRevision, DocumentRevision.id == Chunk.document_revision_id)
        .where(
            Chunk.library_id == run.library_id,
            Chunk.document_revision_id.in_(revision_ids),
            Document.library_id == run.library_id,
            Document.deleted_at.is_(None),
            Document.status == "ready",
            Document.current_revision_id == DocumentRevision.id,
            DocumentRevision.status == "ready",
        )
        .order_by(Chunk.document_id, Chunk.seq, Chunk.id)
    )
    texts = [DiscoveryText(str(chunk_id), text) for chunk_id, text in result.all() if isinstance(text, str) and text.strip()]
    texts = _sample_discovery_texts(
        texts,
        max_texts=settings.graph_schema_discovery_max_source_chunks,
    )
    revision_result = await db.execute(
        select(DocumentRevision.id, DocumentRevision.content_hash)
        .join(Document, Document.id == DocumentRevision.document_id)
        .where(
            DocumentRevision.id.in_(revision_ids),
            DocumentRevision.library_id == run.library_id,
            Document.deleted_at.is_(None),
            Document.status == "ready",
            Document.current_revision_id == DocumentRevision.id,
            DocumentRevision.status == "ready",
        )
    )
    source_hash = canonical_graph_value_hash_v1(
        {
            "revisions": [
                {"id": str(row_id), "content_hash": content_hash}
                for row_id, content_hash in sorted(revision_result.all(), key=lambda row: str(row[0]))
            ]
        }
    )
    return texts, source_hash


async def _fail_run(session_factory, run_id: uuid.UUID, code: str, message: str) -> SchemaDiscoveryRun | None:
    async with session_factory() as db:
        async with db.begin():
            run = await db.get(SchemaDiscoveryRun, run_id, with_for_update=True)
            if run is None:
                return None
            run.status = "failed"
            run.error_code = code
            run.error_message = message[:4000]
            run.finished_at = _now()
            jobs = (
                await db.execute(
                    select(GraphExtractionJob).where(GraphExtractionJob.schema_discovery_run_id == run.id)
                )
            ).scalars().all()
            for job in jobs:
                job.status = "failed"
                job.current_stage = None
                job.error_code = code
                job.error_message = message[:4000]
                job.finished_at = _now()
            return run


async def resume_schema_discovery_run_after_confirmation(
    db: AsyncSession,
    *,
    library: Library,
    ontology_version_id: uuid.UUID,
) -> int:
    """Bind the exact activated version to a waiting discovery batch."""

    from app.services.graph_extraction_jobs import build_ontology_rule_snapshot

    runs = (
        await db.execute(
            select(SchemaDiscoveryRun)
            .where(
                SchemaDiscoveryRun.library_id == library.id,
                SchemaDiscoveryRun.ontology_version_id == ontology_version_id,
                SchemaDiscoveryRun.status == "waiting_confirmation",
            )
            .with_for_update()
        )
    ).scalars().all()
    if not runs:
        return 0
    snapshot, snapshot_hash = await build_ontology_rule_snapshot(
        db,
        library=library,
        ontology_version_id=ontology_version_id,
    )
    snapshot["schema_state"] = "confirmed"
    snapshot["confirmed"] = True
    snapshot_hash = canonical_graph_value_hash_v1(snapshot)
    resumed = 0
    for run in runs:
        run.ontology_snapshot = snapshot
        run.ontology_snapshot_hash = snapshot_hash
        run.status = "succeeded"
        run.finished_at = _now()
        jobs = (
            await db.execute(
                select(GraphExtractionJob)
                .where(GraphExtractionJob.schema_discovery_run_id == run.id)
                .with_for_update()
            )
        ).scalars().all()
        for job in jobs:
            job.ontology_version_id = ontology_version_id
            job.ontology_snapshot = snapshot
            job.ontology_snapshot_hash = snapshot_hash
            model_snapshot = dict(job.model_config_snapshot or {})
            model_snapshot["schema_discovery"] = "completed"
            model_snapshot["schema_state"] = "confirmed"
            job.model_config_snapshot = model_snapshot
            job.model_config_hash = canonical_graph_value_hash_v1(model_snapshot)
            if job.status == "waiting_schema":
                job.status = "queued"
                job.current_stage = "preparing"
                resumed += 1
    return resumed


async def process_next_schema_discovery_run(*, session_factory, provider=None) -> SchemaDiscoveryRun | None:
    """Claim one queued run, call the model once, then bind every waiting job."""

    async with session_factory() as db:
        async with db.begin():
            run = (
                await db.execute(
                    select(SchemaDiscoveryRun)
                    .where(SchemaDiscoveryRun.status == "queued")
                    .order_by(SchemaDiscoveryRun.created_at, SchemaDiscoveryRun.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
            ).scalars().first()
            if run is None:
                return None
            run.status = "discovering"
            run.started_at = _now()
            texts, source_hash = await _current_run_texts(db, run)
            if source_hash != run.source_hash:
                run.status = "failed"
                run.error_code = "source_set_changed"
                run.error_message = "current ready revisions no longer match the discovery source set"
                run.finished_at = _now()
                jobs = (
                    await db.execute(
                        select(GraphExtractionJob).where(
                            GraphExtractionJob.schema_discovery_run_id == run.id
                        )
                    )
                ).scalars().all()
                for job in jobs:
                    job.status = "failed"
                    job.error_code = "source_set_changed"
                    job.error_message = run.error_message
                    job.finished_at = _now()
                return run
            if not texts:
                run.status = "failed"
                run.error_code = "schema_discovery_empty"
                run.error_message = "no current ready revision chunks are available"
                run.finished_at = _now()
                jobs = (
                    await db.execute(
                        select(GraphExtractionJob).where(
                            GraphExtractionJob.schema_discovery_run_id == run.id
                        )
                    )
                ).scalars().all()
                for job in jobs:
                    job.status = "failed"
                    job.error_code = "schema_discovery_empty"
                    job.error_message = run.error_message
                    job.finished_at = _now()
                return run
            library = await db.get(Library, run.library_id)
    output_budget = (
        settings.graph_schema_discovery_max_output_tokens
        if settings.graph_extraction_output_budget_enabled
        else None
    )
    if provider is None:
        from app.services.graph_extraction_provider import OpenAICompatibleGraphExtractor

        provider = OpenAICompatibleGraphExtractor(
            base_url=settings.graph_extraction_base_url,
            model=settings.graph_extraction_model,
            api_key=settings.graph_extraction_api_key.get_secret_value(),
            timeout_seconds=settings.graph_schema_discovery_timeout_seconds,
            max_output_tokens=output_budget,
        )
    try:
        draft = await discover_business_schema(
            texts,
            provider=provider,
            source_hash=run.source_hash,
            context_window_tokens=settings.graph_schema_discovery_context_window_tokens,
            max_output_tokens=output_budget,
            unbounded_output=output_budget is None,
            concept_inventory_enabled=(
                settings.graph_schema_discovery_concept_inventory_enabled
            ),
        )
        if not draft.entity_types:
            raise ValueError("AI Schema discovery returned no entity types")
    except Exception as exc:  # noqa: BLE001 - run records the stable failure
        return await _fail_run(session_factory, run.id, "schema_discovery_failed", str(exc))

    async with session_factory() as db:
        async with db.begin():
            locked_run = await db.get(SchemaDiscoveryRun, run.id, with_for_update=True)
            if locked_run is None or locked_run.status != "discovering":
                return locked_run
            texts_now, source_hash_now = await _current_run_texts(db, locked_run)
            if source_hash_now != locked_run.source_hash or not texts_now:
                locked_run.status = "failed"
                locked_run.error_code = "source_set_changed"
                locked_run.error_message = "source revisions changed during Schema discovery"
                locked_run.finished_at = _now()
                jobs = (
                    await db.execute(
                        select(GraphExtractionJob).where(
                            GraphExtractionJob.schema_discovery_run_id == locked_run.id
                        )
                    )
                ).scalars().all()
                for job in jobs:
                    job.status = "failed"
                    job.error_code = "source_set_changed"
                    job.error_message = locked_run.error_message
                    job.finished_at = _now()
                return locked_run
            ontology_version = await db.get(OntologyVersion, locked_run.ontology_version_id, with_for_update=True)
            library = await db.get(Library, locked_run.library_id)
            if ontology_version is None or library is None or ontology_version.status != "draft":
                locked_run.status = "failed"
                locked_run.error_code = "schema_discovery_state_changed"
                locked_run.error_message = "AI draft is no longer writable"
                locked_run.finished_at = _now()
                return locked_run
            snapshot = await persist_business_schema_draft(
                db,
                library=library,
                ontology_version=ontology_version,
                draft=draft,
            )
            snapshot_hash = canonical_graph_value_hash_v1(snapshot)
            locked_run.ontology_snapshot = snapshot
            locked_run.ontology_snapshot_hash = snapshot_hash
            locked_run.concept_inventory = [
                item.model_dump(mode="json")
                for item in getattr(draft, "concept_inventory", ())
            ]
            locked_run.discovery_trace = dict(getattr(draft, "trace", {}) or {})
            locked_run.confirmation_policy = getattr(
                library, "schema_confirmation_policy", "required"
            )
            locked_run.status = (
                "succeeded"
                if locked_run.confirmation_policy == "automatic"
                else "waiting_confirmation"
            )
            locked_run.finished_at = _now()
            jobs = (
                await db.execute(
                    select(GraphExtractionJob)
                    .where(GraphExtractionJob.schema_discovery_run_id == locked_run.id)
                    .with_for_update()
                )
            ).scalars().all()
            if locked_run.confirmation_policy == "automatic":
                from app.services.graph_extraction_jobs import build_ontology_rule_snapshot
                from app.services.schema_lifecycle_actions import activate_schema_version
                from app.services.schema_lifecycle_contracts import SchemaLifecycleCommand
                from app.services.schema_lifecycle_read import (
                    load_schema_version_bundle,
                    schema_version_state_hash,
                )

                draft_bundle = await load_schema_version_bundle(
                    db, library, ontology_version.id, for_update=True
                )
                current = await resolve_current_ontology(
                    db,
                    library=library,
                    required=False,
                )
                active_id = current.id if current is not None else None
                command = SchemaLifecycleCommand(
                    library_id=library.id,
                    ontology_version_id=ontology_version.id,
                    actor_user_id=getattr(library, "created_by", None),
                    action_kind="activate_version",
                    target_kind="ontology_version",
                    target_id=ontology_version.id,
                    expected_state_hash=schema_version_state_hash(draft_bundle),
                    idempotency_key=f"ai-discovery:{locked_run.id}",
                    payload={
                        "confirmation": "activate_schema_version",
                        "expected_active_version_id": str(active_id) if active_id else None,
                    },
                )
                await activate_schema_version(db, library, command)
                snapshot, snapshot_hash = await build_ontology_rule_snapshot(
                    db,
                    library=library,
                    ontology_version_id=ontology_version.id,
                )
                snapshot["schema_state"] = "confirmed"
                snapshot["confirmed"] = True
                snapshot_hash = canonical_graph_value_hash_v1(snapshot)
                locked_run.ontology_snapshot = snapshot
                locked_run.ontology_snapshot_hash = snapshot_hash
            for job in jobs:
                job.ontology_version_id = ontology_version.id
                job.ontology_snapshot = locked_run.ontology_snapshot
                job.ontology_snapshot_hash = locked_run.ontology_snapshot_hash
                model_snapshot = dict(job.model_config_snapshot or {})
                model_snapshot["schema_discovery"] = "completed"
                model_snapshot["schema_state"] = (
                    "confirmed" if locked_run.confirmation_policy == "automatic" else "ai_draft"
                )
                job.model_config_snapshot = model_snapshot
                job.model_config_hash = canonical_graph_value_hash_v1(model_snapshot)
                if locked_run.confirmation_policy == "automatic":
                    job.status = "queued"
                    job.current_stage = "preparing"
    return locked_run
