from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


TAXONOMY_STATUSES = ("draft", "active", "disabled")
CLASSIFICATION_LABEL_STATUSES = ("active", "disabled")


class ClassificationTaxonomy(Base):
    __tablename__ = "classification_taxonomies"
    __table_args__ = (
        CheckConstraint(
            "version_key ~ '^[a-z](?:[a-z0-9_-]{0,62}[a-z0-9])?$'",
            name="ck_classification_taxonomies_version_key",
        ),
        CheckConstraint(
            "version_no > 0",
            name="ck_classification_taxonomies_version_no",
        ),
        CheckConstraint(
            "status IN ('draft','active','disabled')",
            name="ck_classification_taxonomies_status",
        ),
        CheckConstraint(
            "description IS NULL OR char_length(description) BETWEEN 1 AND 1000",
            name="ck_classification_taxonomies_description",
        ),
        CheckConstraint(
            "parent_version_id IS NULL OR parent_version_id <> id",
            name="ck_classification_taxonomies_parent_not_self",
        ),
        CheckConstraint(
            "((status = 'draft' AND activated_at IS NULL AND activated_by_user_id IS NULL) OR "
            "(status IN ('active','disabled') AND activated_at IS NOT NULL))",
            name="ck_classification_taxonomies_activation_shape",
        ),
        UniqueConstraint(
            "organization_id",
            "version_key",
            "version_no",
            name="uq_classification_taxonomies_org_version",
        ),
        Index(
            "uq_classification_taxonomies_one_active_org",
            "organization_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ix_classification_taxonomies_org_status_created",
            "organization_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_organizations.id",
            ondelete="RESTRICT",
            name="fk_classification_taxonomies_organization",
        ),
        nullable=False,
    )
    version_key: Mapped[str] = mapped_column(String(64), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="draft", server_default="draft"
    )
    parent_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "classification_taxonomies.id",
            ondelete="RESTRICT",
            name="fk_classification_taxonomies_parent_version",
        ),
        nullable=True,
    )
    description: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_users.id",
            ondelete="SET NULL",
            name="fk_classification_taxonomies_created_by",
        ),
        nullable=True,
    )
    activated_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_users.id",
            ondelete="SET NULL",
            name="fk_classification_taxonomies_activated_by",
        ),
        nullable=True,
    )
    activated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ClassificationLabel(Base):
    __tablename__ = "classification_labels"
    __table_args__ = (
        CheckConstraint(
            "key ~ '^[a-z](?:[a-z0-9_-]{0,62}[a-z0-9])?$'",
            name="ck_classification_labels_key",
        ),
        CheckConstraint(
            "btrim(label) <> ''",
            name="ck_classification_labels_label",
        ),
        CheckConstraint(
            "description IS NULL OR char_length(description) BETWEEN 1 AND 1000",
            name="ck_classification_labels_description",
        ),
        CheckConstraint(
            "parent_label_id IS NULL OR parent_label_id <> id",
            name="ck_classification_labels_parent_not_self",
        ),
        CheckConstraint(
            "sort_order BETWEEN 0 AND 1000000",
            name="ck_classification_labels_sort_order",
        ),
        CheckConstraint(
            "status IN ('active','disabled')",
            name="ck_classification_labels_status",
        ),
        UniqueConstraint(
            "taxonomy_version_id",
            "key",
            name="uq_classification_labels_taxonomy_key",
        ),
        Index(
            "ix_classification_labels_taxonomy_status_order",
            "taxonomy_version_id",
            "status",
            "sort_order",
            "key",
        ),
        Index("ix_classification_labels_parent", "parent_label_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    taxonomy_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "classification_taxonomies.id",
            ondelete="CASCADE",
            name="fk_classification_labels_taxonomy",
        ),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    parent_label_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "classification_labels.id",
            ondelete="RESTRICT",
            name="fk_classification_labels_parent",
        ),
        nullable=True,
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class LibraryClassificationLabel(Base):
    __tablename__ = "library_classification_labels"
    __table_args__ = (
        CheckConstraint(
            "ordinal BETWEEN 0 AND 499",
            name="ck_library_classification_labels_ordinal",
        ),
        UniqueConstraint(
            "library_id",
            "label_id",
            name="uq_library_classification_labels_library_label",
        ),
        UniqueConstraint(
            "library_id",
            "ordinal",
            name="uq_library_classification_labels_library_ordinal",
        ),
        Index(
            "ix_library_classification_labels_library_taxonomy_order",
            "library_id",
            "taxonomy_version_id",
            "ordinal",
        ),
        Index("ix_library_classification_labels_label", "label_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "sys_libraries.id",
            ondelete="CASCADE",
            name="fk_library_classification_labels_library",
        ),
        nullable=False,
    )
    taxonomy_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "classification_taxonomies.id",
            ondelete="RESTRICT",
            name="fk_library_classification_labels_taxonomy",
        ),
        nullable=False,
    )
    label_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "classification_labels.id",
            ondelete="RESTRICT",
            name="fk_library_classification_labels_label",
        ),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
