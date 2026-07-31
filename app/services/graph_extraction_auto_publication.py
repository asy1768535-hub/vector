from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select

from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_publication import GraphPublication
from app.models.library import Library
from app.services.graph_publication_activation import (
    GraphPublicationActivationError,
    activate_graph_publication,
)
from app.services.graph_publication_planner import (
    GraphPublicationPlanError,
    plan_graph_publication,
)


class GraphExtractionAutoPublicationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class GraphExtractionAutoPublicationResult:
    outcome: Literal["activated", "already_published", "skipped"]
    publication_id: uuid.UUID | None = None


_PUBLISHED_STATUSES = {"active", "degraded", "superseded"}
_ACTIVATABLE_STATUSES = {"planned", "activating"}
_MATERIALIZATION_COUNT_KEYS = (
    "entity_count",
    "entity_mention_count",
    "relation_count",
    "relation_evidence_count",
)
_MAX_PARENT_RETRIES = 3
_AUTO_PUBLICATION_CONTRACT_VERSION = "v1"


def _has_materialized_effect(job: GraphExtractionJob) -> bool:
    counts = (job.statistics or {}).get("materialization")
    return isinstance(counts, dict) and any(
        isinstance(counts.get(key), int) and counts[key] > 0
        for key in _MATERIALIZATION_COUNT_KEYS
    )


async def auto_publish_graph_extraction_job(
    session_factory,
    *,
    job_id: uuid.UUID,
) -> GraphExtractionAutoPublicationResult:
    idempotency_key = (
        f"graph-extraction:auto-publish:{job_id}:"
        f"{_AUTO_PUBLICATION_CONTRACT_VERSION}"
    )
    for attempt in range(_MAX_PARENT_RETRIES):
        async with session_factory() as db:
            job = await db.get(GraphExtractionJob, job_id)
            if job is None:
                raise GraphExtractionAutoPublicationError(
                    "job_not_found", "graph extraction Job was not found"
                )
            if (
                job.execution_mode != "production"
                or job.status not in {"succeeded", "partially_succeeded"}
                or not _has_materialized_effect(job)
            ):
                return GraphExtractionAutoPublicationResult("skipped")

            existing = (
                (
                    await db.execute(
                        select(GraphPublication)
                        .where(
                            GraphPublication.library_id == job.library_id,
                            GraphPublication.ontology_version_id
                            == job.ontology_version_id,
                            GraphPublication.idempotency_key == idempotency_key,
                        )
                        .order_by(GraphPublication.created_at.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            if existing is not None and existing.status in _PUBLISHED_STATUSES:
                return GraphExtractionAutoPublicationResult(
                    "already_published", existing.id
                )
            if existing is not None and existing.status in _ACTIVATABLE_STATUSES:
                publication = existing
                manifest_hash = existing.manifest_hash
            else:
                library = await db.get(Library, job.library_id)
                if library is None or library.deleted_at is not None:
                    raise GraphExtractionAutoPublicationError(
                        "library_not_found", "graph extraction Library was not found"
                    )
                try:
                    plan = await plan_graph_publication(
                        db,
                        library,
                        ontology_version_id=job.ontology_version_id,
                        include_drafts=True,
                        idempotency_key=idempotency_key,
                        requested_by_user_id=job.requested_by,
                    )
                    await db.commit()
                except GraphPublicationPlanError as exc:
                    await db.rollback()
                    raise GraphExtractionAutoPublicationError(exc.code, str(exc)) from exc
                publication = plan.publication
                manifest_hash = plan.manifest_hash
            actor_id = job.requested_by

        async with session_factory() as activation_db:
            try:
                activated = await activate_graph_publication(
                    activation_db,
                    publication.id,
                    activated_by_user_id=actor_id,
                    expected_manifest_hash=manifest_hash,
                    command_idempotency_key=f"{idempotency_key}:activate",
                )
                return GraphExtractionAutoPublicationResult(
                    "activated", activated.publication.id
                )
            except GraphPublicationActivationError as exc:
                if (
                    exc.code == "publication_parent_changed"
                    and attempt + 1 < _MAX_PARENT_RETRIES
                ):
                    continue
                raise GraphExtractionAutoPublicationError(exc.code, str(exc)) from exc

    raise AssertionError("auto publication retry loop exhausted")
