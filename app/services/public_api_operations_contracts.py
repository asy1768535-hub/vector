from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PublicEndpointKey = Literal[
    "libraries.list",
    "scopes.validate",
    "documents.get",
    "entities.get",
    "relations.get",
    "evidence.get",
    "entities.search",
    "relations.search",
    "retrieval.search",
    "answers.create",
    "answers.stream",
]
PublicTerminalOutcome = Literal[
    "completed",
    "failed",
    "cancelled",
    "disconnected",
]
PUBLIC_ENDPOINT_KEYS = tuple(get_args(PublicEndpointKey))

PUBLIC_OPERATION_RECORD_FIELDS = frozenset(
    {
        "request_id",
        "organization_id",
        "user_id",
        "api_key_id",
        "endpoint_key",
        "http_method",
        "library_ids",
        "started_at",
        "finished_at",
        "duration_ms",
        "http_status",
        "error_code",
        "outcome",
        "is_stream",
        "source_count",
        "chunk_count",
        "graph_entity_count",
        "graph_relation_count",
        "answer_model",
        "input_tokens",
        "output_tokens",
    }
)

_REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PublicOperationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    organization_id: uuid.UUID | None = None
    user_id: uuid.UUID
    api_key_id: uuid.UUID | None = None
    endpoint_key: PublicEndpointKey
    http_method: Literal["GET", "POST"]
    library_ids: tuple[uuid.UUID, ...] = Field(default=(), max_length=20)
    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(ge=0)
    http_status: int = Field(ge=100, le=599)
    error_code: str | None = None
    outcome: PublicTerminalOutcome
    is_stream: bool = False
    source_count: int = Field(default=0, ge=0, le=1_000_000)
    chunk_count: int = Field(default=0, ge=0, le=1_000_000)
    graph_entity_count: int = Field(default=0, ge=0, le=1_000_000)
    graph_relation_count: int = Field(default=0, ge=0, le=1_000_000)
    answer_model: str | None = Field(default=None, max_length=160)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        if _REQUEST_ID_RE.fullmatch(value) is None:
            raise ValueError("request_id must be 32 lowercase hexadecimal characters")
        return value

    @field_validator("library_ids")
    @classmethod
    def validate_library_ids(
        cls, value: tuple[uuid.UUID, ...]
    ) -> tuple[uuid.UUID, ...]:
        if len(set(value)) != len(value):
            raise ValueError("library_ids must be unique")
        return value

    @field_validator("started_at", "finished_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @field_validator("error_code")
    @classmethod
    def validate_error_code(cls, value: str | None) -> str | None:
        if value is not None and _ERROR_CODE_RE.fullmatch(value) is None:
            raise ValueError("error_code is invalid")
        return value

    @field_validator("answer_model")
    @classmethod
    def validate_answer_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        result = value.strip()
        if not result or any(ord(character) < 32 for character in result):
            raise ValueError("answer_model is invalid")
        return result

    @model_validator(mode="after")
    def validate_terminal_state(self) -> PublicOperationRecord:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        if self.outcome == "completed" and self.error_code is not None:
            raise ValueError("completed records cannot contain an error_code")
        return self


class PublicAdmissionScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    organization_ids: tuple[uuid.UUID, ...] = Field(min_length=1, max_length=500)
    api_key_id: uuid.UUID | None = None

    @field_validator("organization_ids")
    @classmethod
    def validate_organization_ids(
        cls, value: tuple[uuid.UUID, ...]
    ) -> tuple[uuid.UUID, ...]:
        if len(set(value)) != len(value):
            raise ValueError("organization_ids must be unique")
        return tuple(sorted(value, key=str))


@dataclass(frozen=True, slots=True)
class PublicRateAdmission:
    organization_ids: tuple[uuid.UUID, ...]
    api_key_id: uuid.UUID | None
    window_started_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PublicAnswerLeaseGrant:
    request_id: str
    organization_id: uuid.UUID
    api_key_id: uuid.UUID | None
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PublicOperationsMaintenanceResult:
    request_records_deleted: int = 0
    rate_windows_deleted: int = 0
    answer_leases_deleted: int = 0


@dataclass(slots=True)
class PublicOperationContext:
    request_id: str
    endpoint_key: PublicEndpointKey
    http_method: Literal["GET", "POST"]
    user_id: uuid.UUID
    api_key_id: uuid.UUID | None = None
    is_stream: bool = False
    rollout_capability: Literal["public_api_v1", "mcp_adapter"] = "public_api_v1"
    started_at: datetime = field(default_factory=utc_now)
    started_monotonic: float = field(default_factory=time.monotonic)
    organization_ids: tuple[uuid.UUID, ...] = ()
    library_ids: tuple[uuid.UUID, ...] = ()
    source_count: int = 0
    chunk_count: int = 0
    graph_entity_count: int = 0
    graph_relation_count: int = 0
    answer_model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    answer_lease: PublicAnswerLeaseGrant | None = None
    stream_outcome: PublicTerminalOutcome | None = None
    stream_error_code: str | None = None
    finalized: bool = False

    def bind_scope(
        self,
        *,
        organization_ids: tuple[uuid.UUID, ...],
        library_ids: tuple[uuid.UUID, ...] = (),
    ) -> None:
        scope = PublicAdmissionScope(
            organization_ids=organization_ids,
            api_key_id=self.api_key_id,
        )
        if len(library_ids) > 20 or len(set(library_ids)) != len(library_ids):
            raise ValueError("library_ids must contain at most 20 unique UUIDs")
        self.organization_ids = scope.organization_ids
        self.library_ids = library_ids

    def terminal_record(
        self,
        *,
        http_status: int,
        outcome: PublicTerminalOutcome,
        error_code: str | None = None,
        finished_at: datetime | None = None,
    ) -> PublicOperationRecord:
        finished = finished_at or utc_now()
        elapsed_ms = max(0, round((time.monotonic() - self.started_monotonic) * 1000))
        organization_id = (
            self.organization_ids[0] if len(self.organization_ids) == 1 else None
        )
        return PublicOperationRecord(
            request_id=self.request_id,
            organization_id=organization_id,
            user_id=self.user_id,
            api_key_id=self.api_key_id,
            endpoint_key=self.endpoint_key,
            http_method=self.http_method,
            library_ids=self.library_ids,
            started_at=self.started_at,
            finished_at=finished,
            duration_ms=elapsed_ms,
            http_status=http_status,
            error_code=error_code,
            outcome=outcome,
            is_stream=self.is_stream,
            source_count=self.source_count,
            chunk_count=self.chunk_count,
            graph_entity_count=self.graph_entity_count,
            graph_relation_count=self.graph_relation_count,
            answer_model=self.answer_model,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
        )
