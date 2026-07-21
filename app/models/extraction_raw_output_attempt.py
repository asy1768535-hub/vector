from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ExtractionRawOutputAttempt(Base):
    __tablename__ = "extraction_raw_output_attempts"
    __table_args__ = (
        UniqueConstraint(
            "extraction_unit_id",
            "attempt_no",
            name="uq_extraction_raw_attempts_unit_no",
        ),
        CheckConstraint("attempt_no >= 1", name="ck_extraction_raw_attempts_attempt_no"),
        CheckConstraint(
            "request_status IN ('pending','succeeded','abandoned','timeout','network_error','http_error')",
            name="ck_extraction_raw_attempts_request_status",
        ),
        CheckConstraint(
            "parse_status IS NULL OR parse_status IN ('valid','invalid_json','invalid_schema')",
            name="ck_extraction_raw_attempts_parse_status",
        ),
        CheckConstraint(
            "(request_status IN ('pending','abandoned') AND latency_ms IS NULL) OR "
            "(request_status IN ('succeeded','timeout','network_error','http_error') "
            "AND latency_ms IS NOT NULL)",
            name="ck_extraction_raw_attempts_latency",
        ),
        CheckConstraint(
            "(request_status = 'succeeded' AND parse_status IS NOT NULL) OR "
            "(request_status <> 'succeeded' AND parse_status IS NULL)",
            name="ck_extraction_raw_attempts_parse_by_request",
        ),
        CheckConstraint(
            "(request_status = 'abandoned' AND abandoned_at IS NOT NULL "
            "AND abandoned_reason IS NOT NULL) OR "
            "(request_status <> 'abandoned' AND abandoned_at IS NULL "
            "AND abandoned_reason IS NULL)",
            name="ck_extraction_raw_attempts_abandoned_fields",
        ),
        CheckConstraint(
            "abandoned_reason IS NULL OR abandoned_reason IN "
            "('lease_expired','claim_replaced','unit_cancelled')",
            name="ck_extraction_raw_attempts_abandoned_reason",
        ),
        CheckConstraint(
            "purged_at IS NULL OR (raw_response IS NULL AND parsed_response IS NULL "
            "AND parse_error IS NULL)",
            name="ck_extraction_raw_attempts_payload_or_purged",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    extraction_unit_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_extraction_units.id",
            ondelete="CASCADE",
            name="fk_extraction_raw_attempts_unit",
        ),
        nullable=False,
    )
    context_snapshot_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "extraction_context_snapshots.id",
            ondelete="CASCADE",
            name="fk_extraction_raw_attempts_context",
        ),
        nullable=False,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    claim_token: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    request_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )
    parse_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_request_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    raw_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    parsed_response: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    parse_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    input_token_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output_token_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    finish_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    abandoned_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    abandoned_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    purged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


Index(
    "ix_extraction_raw_attempts_unit_no",
    ExtractionRawOutputAttempt.extraction_unit_id,
    ExtractionRawOutputAttempt.attempt_no.desc(),
)
Index(
    "ix_extraction_raw_attempts_pending_claim",
    ExtractionRawOutputAttempt.claim_token,
    postgresql_where=text("request_status = 'pending'"),
)
Index(
    "ix_extraction_raw_attempts_purge",
    ExtractionRawOutputAttempt.purged_at,
    ExtractionRawOutputAttempt.created_at,
)
