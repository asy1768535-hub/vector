from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select

from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_publication import GraphPublication
from app.models.graph_publication_item import GraphPublicationItem
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
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
_MAX_PARENT_RETRIES = 3
_AUTO_PUBLICATION_CONTRACT_VERSION = "v1"


def _has_publishable_relation(job: GraphExtractionJob) -> bool:
    counts = (job.statistics or {}).get("materialization")
    return (
        isinstance(counts, dict)
        and isinstance(counts.get("publishable_relation_count"), int)
        and counts["publishable_relation_count"] > 0
        and isinstance(counts.get("publishable_relation_evidence_count"), int)
        and counts["publishable_relation_evidence_count"] > 0
    )


def _item_key(item: GraphPublicationItem) -> tuple[str, uuid.UUID] | None:
    if item.item_kind == "entity" and item.entity_id is not None:
        return item.item_kind, item.entity_id
    if item.item_kind == "relation" and item.relation_id is not None:
        return item.item_kind, item.relation_id
    return None


def _publication_diff(
    publication: GraphPublication,
    parent: GraphPublication | None,
    items: list[GraphPublicationItem],
) -> dict[str, dict[str, int]] | None:
    by_publication: dict[uuid.UUID, dict[tuple[str, uuid.UUID], str]] = {}
    for item in items:
        key = _item_key(item)
        if key is not None:
            by_publication.setdefault(item.publication_id, {})[key] = item.item_hash
    current_items = by_publication.get(publication.id, {})
    if len(current_items) != publication.entity_count + publication.relation_count:
        return None
    previous_items: dict[tuple[str, uuid.UUID], str] = {}
    if publication.parent_publication_id is not None:
        if parent is None:
            return None
        previous_items = by_publication.get(parent.id, {})
        if len(previous_items) != parent.entity_count + parent.relation_count:
            return None

    result: dict[str, dict[str, int]] = {}
    for kind in ("entity", "relation"):
        current = {key: value for key, value in current_items.items() if key[0] == kind}
        previous = {
            key: value for key, value in previous_items.items() if key[0] == kind
        }
        current_keys = set(current)
        previous_keys = set(previous)
        common_keys = current_keys & previous_keys
        result[kind] = {
            "added": len(current_keys - previous_keys),
            "retained": sum(current[key] == previous[key] for key in common_keys),
            "changed": sum(current[key] != previous[key] for key in common_keys),
            "removed": len(previous_keys - current_keys),
        }
    return result


async def _load_publication_diff(db, publication: GraphPublication):
    parent = None
    publication_ids = [publication.id]
    if publication.parent_publication_id is not None:
        parent = await db.get(GraphPublication, publication.parent_publication_id)
        publication_ids.append(publication.parent_publication_id)
    items = list(
        (
            await db.execute(
                select(GraphPublicationItem).where(
                    GraphPublicationItem.publication_id.in_(publication_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    return _publication_diff(publication, parent, items)


def _set_publication_statistics(
    job: GraphExtractionJob,
    *,
    outcome: str,
    failure_reason: str | None = None,
    publication_id: uuid.UUID | None = None,
    publication_diff: dict[str, dict[str, int]] | None = None,
    current_graph_unchanged: bool = False,
) -> None:
    statistics = dict(job.statistics or {})
    materialization = statistics.get("materialization")
    materialization = materialization if isinstance(materialization, dict) else {}
    statistics["publication"] = {
        "outcome": outcome,
        "entity_count": int(materialization.get("publishable_entity_candidate_count") or 0),
        "relation_count": int(materialization.get("publishable_relation_count") or 0),
        "relation_evidence_count": int(
            materialization.get("publishable_relation_evidence_count") or 0
        ),
        "failure_reason": failure_reason,
        "publication_id": str(publication_id) if publication_id is not None else None,
        "diff": publication_diff,
        "current_graph_unchanged": current_graph_unchanged,
    }
    job.statistics = statistics


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
            ):
                _set_publication_statistics(
                    job,
                    outcome="skipped",
                    failure_reason="job_not_publishable",
                    current_graph_unchanged=True,
                )
                await db.commit()
                return GraphExtractionAutoPublicationResult("skipped")
            has_publishable_relation = _has_publishable_relation(job)

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
                publication_diff = await _load_publication_diff(db, existing)
                _set_publication_statistics(
                    job,
                    outcome="already_published",
                    publication_id=existing.id,
                    publication_diff=publication_diff,
                )
                await db.commit()
                return GraphExtractionAutoPublicationResult(
                    "already_published", existing.id
                )
            if existing is not None and existing.status in _ACTIVATABLE_STATUSES:
                publication = existing
                manifest_hash = existing.manifest_hash
            else:
                if not has_publishable_relation:
                    degraded = (
                        (
                            await db.execute(
                                select(GraphPublication.id)
                                .where(
                                    GraphPublication.library_id == job.library_id,
                                    GraphPublication.ontology_version_id
                                    == job.ontology_version_id,
                                    GraphPublication.status == "degraded",
                                )
                                .limit(1)
                            )
                        )
                        .scalars()
                        .first()
                    )
                    if degraded is None:
                        _set_publication_statistics(
                            job,
                            outcome="skipped",
                            failure_reason="no_valid_relation",
                            current_graph_unchanged=True,
                        )
                        await db.commit()
                        return GraphExtractionAutoPublicationResult("skipped")
                library = await db.get(Library, job.library_id)
                if library is None or library.deleted_at is not None:
                    raise GraphExtractionAutoPublicationError(
                        "library_not_found", "graph extraction Library was not found"
                    )
                ontology = await db.get(OntologyVersion, job.ontology_version_id)
                explicit_ai_draft = bool(
                    ontology is not None
                    and getattr(job, "schema_discovery_run_id", None) is not None
                    and ontology.status == "draft"
                    and (job.ontology_snapshot or {}).get("schema_state") == "ai_draft"
                    and (job.ontology_snapshot or {}).get("confirmed") is False
                    and (job.ontology_snapshot or {}).get("ontology_version_id") == str(job.ontology_version_id)
                )
                include_drafts = explicit_ai_draft
                try:
                    plan = await plan_graph_publication(
                        db,
                        library,
                        ontology_version_id=job.ontology_version_id,
                        # v0.4 materialization intentionally creates draft facts.
                        # Auto-publication may include them only when the job has
                        # passed the materializer's relation/evidence gates.
                        include_drafts=include_drafts or has_publishable_relation,
                        allow_explicit_draft=explicit_ai_draft,
                        plan_options=(
                            {
                                "explicit_ai_draft": True,
                                "schema_discovery_run_id": str(getattr(job, "schema_discovery_run_id")),
                            }
                            if explicit_ai_draft
                            else None
                        ),
                        idempotency_key=idempotency_key,
                        requested_by_user_id=job.requested_by,
                    )
                except GraphPublicationPlanError as exc:
                    await db.rollback()
                    _set_publication_statistics(
                        job,
                        outcome="failed",
                        failure_reason=exc.code,
                        current_graph_unchanged=True,
                    )
                    await db.commit()
                    raise GraphExtractionAutoPublicationError(exc.code, str(exc)) from exc
                publication = plan.publication
                manifest_hash = plan.manifest_hash
            if publication.relation_count < 1:
                await db.rollback()
                skipped_job = await db.get(GraphExtractionJob, job_id)
                if skipped_job is not None:
                    _set_publication_statistics(
                        skipped_job,
                        outcome="skipped",
                        failure_reason="no_valid_relation",
                        current_graph_unchanged=True,
                    )
                    await db.commit()
                return GraphExtractionAutoPublicationResult("skipped")
            await db.commit()
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
                activated_job = await activation_db.get(GraphExtractionJob, job_id)
                if activated_job is not None:
                    publication_diff = await _load_publication_diff(
                        activation_db, activated.publication
                    )
                    _set_publication_statistics(
                        activated_job,
                        outcome="activated",
                        publication_id=activated.publication.id,
                        publication_diff=publication_diff,
                    )
                    await activation_db.commit()
                return GraphExtractionAutoPublicationResult(
                    "activated", activated.publication.id
                )
            except GraphPublicationActivationError as exc:
                if (
                    exc.code == "publication_parent_changed"
                    and attempt + 1 < _MAX_PARENT_RETRIES
                ):
                    continue
                await activation_db.rollback()
                failed_job = await activation_db.get(GraphExtractionJob, job_id)
                if failed_job is not None:
                    _set_publication_statistics(
                        failed_job,
                        outcome="failed",
                        failure_reason=exc.code,
                        current_graph_unchanged=True,
                    )
                    await activation_db.commit()
                raise GraphExtractionAutoPublicationError(exc.code, str(exc)) from exc

    raise AssertionError("auto publication retry loop exhausted")
