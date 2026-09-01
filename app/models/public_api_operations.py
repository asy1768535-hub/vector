from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PublicAPIRequestRecord(Base):
    __tablename__ = "public_api_request_records"
    __table_args__ = (
        CheckConstraint(
            "request_id ~ '^[0-9a-f]{32}$'",
            name="ck_public_api_request_records_request_id",
        ),
        CheckConstraint(
            "endpoint_key ~ '^[a-z][a-z0-9_.-]{0,63}$' "
            "AND http_method IN ('GET','POST')",
            name="ck_public_api_request_records_endpoint",
        ),
        CheckConstraint(
            "cardinality(library_ids) <= 20 "
            "AND array_position(library_ids, NULL) IS NULL",
            name="ck_public_api_request_records_libraries",
        ),
        CheckConstraint(
            "finished_at >= started_at AND duration_ms >= 0 "
            "AND http_status BETWEEN 100 AND 599",
            name="ck_public_api_request_records_timing",
        ),
        CheckConstraint(
            "outcome IN ('completed','failed','cancelled','disconnected') "
            "AND (error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,63}$')",
            name="ck_public_api_request_records_outcome",
        ),
        CheckConstraint(
            "source_count >= 0 AND chunk_count >= 0 "
            "AND graph_entity_count >= 0 AND graph_relation_count >= 0 "
            "AND (input_tokens IS NULL OR input_tokens >= 0) "
            "AND (output_tokens IS NULL OR output_tokens >= 0)",
            name="ck_public_api_request_records_counts",
        ),
        UniqueConstraint(
            "request_id", name="uq_public_api_request_records_request_id"
        ),
        Index("ix_public_api_request_records_retention", "finished_at", "id"),
        Index(
            "ix_public_api_request_records_organization_finished",
            "organization_id",
            text("finished_at DESC"),
        ),
        Index(
            "ix_public_api_request_records_api_key_finished",
            "api_key_id",
            text("finished_at DESC"),
            postgresql_where=text("api_key_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    request_id: Mapped[str] = mapped_column(String(32), nullable=False)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_organizations.id",
            ondelete="SET NULL",
            name="fk_public_api_request_records_organization",
        ),
        nullable=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_users.id",
            ondelete="SET NULL",
            name="fk_public_api_request_records_user",
        ),
        nullable=True,
    )
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_api_keys.id",
            ondelete="SET NULL",
            name="fk_public_api_request_records_api_key",
        ),
        nullable=True,
    )
    endpoint_key: Mapped[str] = mapped_column(String(64), nullable=False)
    http_method: Mapped[str] = mapped_column(String(8), nullable=False)
    library_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PgUUID(as_uuid=True)),
        nullable=False,
        default=list,
        server_default=text("'{}'::uuid[]"),
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    http_status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    is_stream: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    source_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    graph_entity_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    graph_relation_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    answer_model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class PublicAPIRateWindow(Base):
    __tablename__ = "public_api_rate_windows"
    __table_args__ = (
        CheckConstraint(
            "scope_kind IN ('organization','api_key') AND request_count > 0",
            name="ck_public_api_rate_windows_values",
        ),
        CheckConstraint(
            "date_trunc('minute', window_started_at) = window_started_at",
            name="ck_public_api_rate_windows_boundary",
        ),
        Index("ix_public_api_rate_windows_started", "window_started_at"),
    )

    scope_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    scope_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    request_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        onupdate=text("now()"),
    )


class PublicAPIAnswerLease(Base):
    __tablename__ = "public_api_answer_leases"
    __table_args__ = (
        CheckConstraint(
            "request_id ~ '^[0-9a-f]{32}$' "
            "AND endpoint_key IN ('answers.create','answers.stream')",
            name="ck_public_api_answer_leases_identity",
        ),
        CheckConstraint(
            "expires_at > acquired_at",
            name="ck_public_api_answer_leases_expiry",
        ),
        UniqueConstraint(
            "request_id", name="uq_public_api_answer_leases_request_id"
        ),
        Index(
            "ix_public_api_answer_leases_organization_expiry",
            "organization_id",
            "expires_at",
        ),
        Index(
            "ix_public_api_answer_leases_api_key_expiry",
            "api_key_id",
            "expires_at",
            postgresql_where=text("api_key_id IS NOT NULL"),
        ),
        Index("ix_public_api_answer_leases_expiry", "expires_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    request_id: Mapped[str] = mapped_column(String(32), nullable=False)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_organizations.id",
            ondelete="CASCADE",
            name="fk_public_api_answer_leases_organization",
        ),
        nullable=False,
    )
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_api_keys.id",
            ondelete="CASCADE",
            name="fk_public_api_answer_leases_api_key",
        ),
        nullable=True,
    )
    endpoint_key: Mapped[str] = mapped_column(String(64), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
