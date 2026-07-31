from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import async_session_factory
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.services.graph_extraction_jobs import (
    GraphExtractionJobError,
    create_graph_extraction_job,
)


class GraphExtractionTriggerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def graph_extraction_auto_trigger_available() -> bool:
    return (
        settings.graph_extraction_enabled
        and settings.graph_extraction_auto_trigger_enabled
        and bool(settings.graph_extraction_api_key.get_secret_value().strip())
    )


def graph_extraction_upload_request_available() -> bool:
    return (
        settings.graph_extraction_enabled
        and bool(settings.graph_extraction_api_key.get_secret_value().strip())
    )


def _library_build_mode(library: Library) -> str:
    value = getattr(library, "graph_extraction_build_mode", "standard")
    return value if value in {"fast", "standard", "deep"} else "standard"


async def graph_extraction_upload_configuration(db, library: Library) -> dict:
    reasons = []
    if not settings.graph_extraction_enabled:
        reasons.append("runtime_disabled")
    if not settings.graph_extraction_api_key.get_secret_value().strip():
        reasons.append("provider_unconfigured")
    if not library.graph_extraction_enabled:
        reasons.append("library_disabled")
    if not library.external_llm_enabled:
        reasons.append("external_model_disabled")
    levels = (
        library.graph_extraction_allowed_security_levels
        if isinstance(library.graph_extraction_allowed_security_levels, list)
        else []
    )
    if not levels:
        reasons.append("security_levels_missing")
    active_ontology = (
        await db.execute(
            select(OntologyVersion.id).where(
                OntologyVersion.library_id == library.id,
                OntologyVersion.status == "active",
            ).limit(1)
        )
    ).scalar_one_or_none()
    if active_ontology is None:
        reasons.append("active_ontology_missing")
    return {
        "available": not reasons,
        "default_requested": bool(library.graph_extraction_enabled),
        "default_build_mode": _library_build_mode(library),
        "allowed_security_levels": levels,
        "reasons": reasons,
    }


async def enqueue_ready_revision_graph_extraction(
    *,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
    force: bool = False,
    session_factory=async_session_factory,
):
    if force:
        if not graph_extraction_upload_request_available():
            return None
    elif not graph_extraction_auto_trigger_available():
        return None
    try:
        async with session_factory() as db:
            async with db.begin():
                library = await db.get(Library, library_id)
                document = await db.get(Document, document_id)
                revision = await db.get(DocumentRevision, revision_id)
                if library is None or document is None or revision is None:
                    raise GraphExtractionTriggerError(
                        "publication_scope_missing",
                        "published revision scope is missing",
                    )
                return await create_graph_extraction_job(
                    db,
                    library=library,
                    document=document,
                    revision=revision,
                    trigger_type="revision_published",
                    execution_mode="production",
                    requested_by=revision.created_by,
                    idempotency_key=None,
                    build_mode=_library_build_mode(library),
                )
    except GraphExtractionJobError as exc:
        raise GraphExtractionTriggerError(exc.code, str(exc)) from exc
    except IntegrityError as exc:
        raise GraphExtractionTriggerError(
            "trigger_race",
            "graph extraction trigger raced with another creator",
        ) from exc


async def compensate_ready_graph_extractions(
    *,
    limit: int = 100,
    session_factory=async_session_factory,
) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 100:
        raise ValueError("compensation limit must be between 1 and 100")
    if not graph_extraction_auto_trigger_available():
        return 0
    ensured = 0
    async with session_factory() as db:
        async with db.begin():
            result = await db.execute(
                select(Library, Document, DocumentRevision)
                .join(Document, Document.library_id == Library.id)
                .join(
                    DocumentRevision,
                    DocumentRevision.id == Document.current_revision_id,
                )
                .where(
                    Library.deleted_at.is_(None),
                    Library.graph_extraction_enabled.is_(True),
                    Library.external_llm_enabled.is_(True),
                    Document.deleted_at.is_(None),
                    Document.status == "ready",
                    DocumentRevision.status == "ready",
                    DocumentRevision.security_level.is_not(None),
                )
                .order_by(
                    DocumentRevision.published_at.asc().nulls_last(),
                    DocumentRevision.id.asc(),
                )
                .limit(limit)
            )
            for library, document, revision in result.all():
                try:
                    async with db.begin_nested():
                        await create_graph_extraction_job(
                            db,
                            library=library,
                            document=document,
                            revision=revision,
                            trigger_type="revision_published",
                            execution_mode="production",
                            requested_by=revision.created_by,
                            idempotency_key=None,
                            build_mode=_library_build_mode(library),
                        )
                    ensured += 1
                except (GraphExtractionJobError, IntegrityError):
                    continue
    return ensured
