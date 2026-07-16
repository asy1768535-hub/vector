from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


GRAPH_PUBLICATION_ITEM_KIND_ENTITY = "entity"
GRAPH_PUBLICATION_ITEM_KIND_RELATION = "relation"

GRAPH_PUBLICATION_ITEM_STATUS_PLANNED = "planned"
GRAPH_PUBLICATION_ITEM_STATUS_ACTIVE = "active"
GRAPH_PUBLICATION_ITEM_STATUS_STALE = "stale"
GRAPH_PUBLICATION_ITEM_STATUS_DEGRADED = "degraded"
GRAPH_PUBLICATION_ITEM_STATUS_SUPERSEDED = "superseded"


class GraphPublicationItem(Base):
    __tablename__ = "graph_publication_items"
    __table_args__ = (
        CheckConstraint(
            "item_kind IN ('entity','relation')",
            name="ck_graph_publication_items_kind",
        ),
        CheckConstraint(
            "status IN ('planned','active','stale','degraded','superseded')",
            name="ck_graph_publication_items_status",
        ),
        CheckConstraint(
            "((item_kind = 'entity' AND entity_id IS NOT NULL AND relation_id IS NULL) OR "
            "(item_kind = 'relation' AND relation_id IS NOT NULL AND entity_id IS NULL))",
            name="ck_graph_publication_items_exact_target",
        ),
        Index(
            "uq_graph_publication_items_publication_entity",
            "publication_id",
            "item_kind",
            "entity_id",
            unique=True,
            postgresql_where=text("entity_id IS NOT NULL"),
        ),
        Index(
            "uq_graph_publication_items_publication_relation",
            "publication_id",
            "item_kind",
            "relation_id",
            unique=True,
            postgresql_where=text("relation_id IS NOT NULL"),
        ),
        Index(
            "uq_graph_publication_items_publication_hash",
            "publication_id",
            "item_hash",
            unique=True,
        ),
        Index(
            "ix_graph_publication_items_scope_kind_status",
            "library_id",
            "ontology_version_id",
            "item_kind",
            "status",
        ),
        Index("ix_graph_publication_items_entity", "entity_id"),
        Index("ix_graph_publication_items_relation", "relation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    publication_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "graph_publications.id",
            ondelete="CASCADE",
            name="fk_graph_publication_items_publication",
        ),
        nullable=False,
    )
    library_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("sys_libraries.id", ondelete="CASCADE", name="fk_graph_publication_items_library"),
        nullable=False,
    )
    ontology_version_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "ontology_versions.id",
            ondelete="RESTRICT",
            name="fk_graph_publication_items_ontology",
        ),
        nullable=False,
    )
    item_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    entity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("entities.id", ondelete="RESTRICT", name="fk_graph_publication_items_entity"),
        nullable=True,
    )
    relation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey(
            "knowledge_relations.id",
            ondelete="RESTRICT",
            name="fk_graph_publication_items_relation",
        ),
        nullable=True,
    )
    item_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planned", server_default="planned")
    support_evidence_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    support_counts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    fact_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
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
