from __future__ import annotations

import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from app.models.document_revision import DocumentRevision
from app.models.extraction_raw_output_attempt import ExtractionRawOutputAttempt
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.schemas.graph_extraction import GraphExtractionPayload


def normalize_cached_graph_extraction_payload(
    value: Any,
) -> GraphExtractionPayload | None:
    if not isinstance(value, dict):
        return None
    payload = {key: item for key, item in value.items() if key != "batch_key"}
    try:
        return GraphExtractionPayload.model_validate(payload)
    except ValidationError:
        return None


async def load_cached_graph_extraction_payload(
    db,
    *,
    cache_key: str,
    library_id: uuid.UUID,
    security_level: str | None,
    visibility_scope: str | None,
    current_unit_id: uuid.UUID,
) -> GraphExtractionPayload | None:
    conditions = [
        ExtractionRawOutputAttempt.request_payload_hash == cache_key,
        ExtractionRawOutputAttempt.request_status == "succeeded",
        ExtractionRawOutputAttempt.parse_status == "valid",
        ExtractionRawOutputAttempt.parsed_response.is_not(None),
        ExtractionRawOutputAttempt.purged_at.is_(None),
        ExtractionRawOutputAttempt.extraction_unit_id != current_unit_id,
        GraphExtractionJob.library_id == library_id,
        GraphExtractionJob.sensitive_payload_purged_at.is_(None),
    ]
    conditions.append(
        DocumentRevision.security_level == security_level
        if security_level is not None
        else DocumentRevision.security_level.is_(None)
    )
    conditions.append(
        DocumentRevision.visibility_scope == visibility_scope
        if visibility_scope is not None
        else DocumentRevision.visibility_scope.is_(None)
    )
    result = await db.execute(
        select(ExtractionRawOutputAttempt.parsed_response)
        .join(
            GraphExtractionUnit,
            GraphExtractionUnit.id == ExtractionRawOutputAttempt.extraction_unit_id,
        )
        .join(GraphExtractionJob, GraphExtractionJob.id == GraphExtractionUnit.job_id)
        .join(
            DocumentRevision,
            DocumentRevision.id == GraphExtractionUnit.document_revision_id,
        )
        .where(*conditions)
        .order_by(ExtractionRawOutputAttempt.created_at.desc())
        .limit(20)
    )
    for value in result.scalars().all():
        payload = normalize_cached_graph_extraction_payload(value)
        if payload is not None:
            return payload
    return None


async def load_replay_graph_extraction_payload(
    db,
    *,
    unit_id: uuid.UUID,
    context_snapshot_id: uuid.UUID,
) -> GraphExtractionPayload | None:
    result = await db.execute(
        select(ExtractionRawOutputAttempt.parsed_response)
        .where(
            ExtractionRawOutputAttempt.extraction_unit_id == unit_id,
            ExtractionRawOutputAttempt.context_snapshot_id == context_snapshot_id,
            ExtractionRawOutputAttempt.request_status == "succeeded",
            ExtractionRawOutputAttempt.parse_status == "valid",
            ExtractionRawOutputAttempt.parsed_response.is_not(None),
            ExtractionRawOutputAttempt.purged_at.is_(None),
        )
        .order_by(ExtractionRawOutputAttempt.attempt_no.asc())
    )
    for value in result.scalars().all():
        payload = normalize_cached_graph_extraction_payload(value)
        if payload is not None:
            return payload
    return None
