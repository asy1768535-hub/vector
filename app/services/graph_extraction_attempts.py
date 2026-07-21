from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import exists, func, select, update

from app.models.extraction_context_snapshot import ExtractionContextSnapshot
from app.models.extraction_raw_output_attempt import ExtractionRawOutputAttempt
from app.models.graph_extraction_unit import GraphExtractionUnit


RequestTerminalStatus = Literal[
    "succeeded",
    "timeout",
    "network_error",
    "http_error",
]
ParseStatus = Literal["valid", "invalid_json", "invalid_schema"]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_STATUSES = {"succeeded", "timeout", "network_error", "http_error"}
_PARSE_STATUSES = {"valid", "invalid_json", "invalid_schema"}


class AttemptStateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AttemptCompletion:
    request_status: RequestTerminalStatus
    latency_ms: int
    parse_status: ParseStatus | None = None
    provider_request_id: str | None = None
    raw_response: str | None = None
    parsed_response: dict[str, Any] | None = None
    parse_error: str | None = None
    input_token_count: int | None = None
    output_token_count: int | None = None
    finish_reason: str | None = None


def _sanitize_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", str(value))
    return sanitized[:limit]


def _validate_nonnegative_int(value: int | None, field: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError(f"{field} must be a non-negative integer")


def _completion_values(completion: AttemptCompletion) -> dict[str, Any]:
    if completion.request_status not in _TERMINAL_STATUSES:
        raise ValueError("unsupported terminal request_status")
    if completion.parse_status is not None and completion.parse_status not in _PARSE_STATUSES:
        raise ValueError("unsupported parse_status")
    if completion.request_status == "succeeded" and completion.parse_status is None:
        raise ValueError("succeeded Attempt requires parse_status")
    if completion.request_status != "succeeded" and completion.parse_status is not None:
        raise ValueError("failed Provider request cannot have parse_status")
    _validate_nonnegative_int(completion.latency_ms, "latency_ms")
    _validate_nonnegative_int(completion.input_token_count, "input_token_count")
    _validate_nonnegative_int(completion.output_token_count, "output_token_count")

    parse_error = _sanitize_text(completion.parse_error, 512)
    if completion.parse_status in {"invalid_json", "invalid_schema"} and not parse_error:
        raise ValueError("invalid parsed response requires parse_error")
    if completion.parse_status == "valid" and completion.parsed_response is None:
        raise ValueError("valid parsed response requires parsed_response")
    if completion.request_status != "succeeded" and completion.parsed_response is not None:
        raise ValueError("failed Provider request cannot have parsed_response")

    return {
        "request_status": completion.request_status,
        "parse_status": completion.parse_status,
        "provider_request_id": _sanitize_text(completion.provider_request_id, 255),
        "raw_response": _sanitize_text(completion.raw_response, 1_000_000),
        "parsed_response": completion.parsed_response,
        "parse_error": parse_error,
        "input_token_count": completion.input_token_count,
        "output_token_count": completion.output_token_count,
        "latency_ms": completion.latency_ms,
        "finish_reason": _sanitize_text(completion.finish_reason, 64),
        "updated_at": func.now(),
    }


async def create_pending_attempt(
    db,
    *,
    unit_id: uuid.UUID,
    context_snapshot_id: uuid.UUID,
    claim_token: uuid.UUID,
    request_payload_hash: str,
    max_attempts: int,
) -> ExtractionRawOutputAttempt:
    if not _SHA256.fullmatch(request_payload_hash):
        raise ValueError("request_payload_hash must be a lowercase SHA-256 hex digest")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")

    async with db.begin():
        unit_result = await db.execute(
            select(GraphExtractionUnit)
            .where(
                GraphExtractionUnit.id == unit_id,
                GraphExtractionUnit.status == "processing",
                GraphExtractionUnit.claim_token == claim_token,
                GraphExtractionUnit.lease_expires_at > func.now(),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        unit = unit_result.scalars().first()
        if unit is None:
            raise AttemptStateError("Unit does not have the requested live claim")
        if unit.model_attempt_count >= max_attempts:
            raise AttemptStateError("Unit model attempt budget is exhausted")

        context_result = await db.execute(
            select(ExtractionContextSnapshot.id).where(
                ExtractionContextSnapshot.id == context_snapshot_id,
                ExtractionContextSnapshot.extraction_unit_id == unit_id,
                ExtractionContextSnapshot.purged_at.is_(None),
            )
        )
        if context_result.scalar_one_or_none() is None:
            raise AttemptStateError("Context Snapshot is missing, purged, or belongs to another Unit")

        number_result = await db.execute(
            select(func.max(ExtractionRawOutputAttempt.attempt_no)).where(
                ExtractionRawOutputAttempt.extraction_unit_id == unit_id
            )
        )
        previous_number = number_result.scalar_one_or_none() or 0
        attempt = ExtractionRawOutputAttempt(
            extraction_unit_id=unit_id,
            context_snapshot_id=context_snapshot_id,
            attempt_no=previous_number + 1,
            claim_token=claim_token,
            request_status="pending",
            parse_status=None,
            request_payload_hash=request_payload_hash,
        )
        db.add(attempt)
        unit.model_attempt_count += 1
        await db.flush()
        return attempt


async def finalize_attempt(
    db,
    *,
    attempt_id: uuid.UUID,
    claim_token: uuid.UUID,
    completion: AttemptCompletion,
) -> bool:
    values = _completion_values(completion)
    live_claim = exists(
        select(1).where(
            GraphExtractionUnit.id == ExtractionRawOutputAttempt.extraction_unit_id,
            GraphExtractionUnit.status == "processing",
            GraphExtractionUnit.claim_token == claim_token,
            GraphExtractionUnit.lease_expires_at > func.now(),
        )
    )
    statement = (
        update(ExtractionRawOutputAttempt)
        .where(
            ExtractionRawOutputAttempt.id == attempt_id,
            ExtractionRawOutputAttempt.request_status == "pending",
            ExtractionRawOutputAttempt.claim_token == claim_token,
            live_claim,
        )
        .values(**values)
    )
    async with db.begin():
        result = await db.execute(statement)
        return result.rowcount == 1
