"""Optional PostgreSQL checks for P1.2 transaction and FK behavior.

The tests are intentionally skipped unless an explicit disposable test DSN is
provided.  They never use the application's configured database settings.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.db import Base
from app.models.canonical_entity import CanonicalEntity
from app.models.entity_resolution_decision import EntityResolutionDecision
from app.models.library import Library
from app.models.organization import (
    DEFAULT_ORGANIZATION_ID,
    DEFAULT_ORGANIZATION_SLUG,
    Organization,
)
from app.services.canonical_entity_resolution import EntityResolutionInput, resolve_canonical_entity
from app.services.graph_normalization import normalize_graph_name_v1


_PG_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _PG_DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL service",
)


def _request(library_id: uuid.UUID) -> EntityResolutionInput:
    return EntityResolutionInput(
        library_id=library_id,
        observed_name="Concurrent Project A",
        observed_normalized_name=normalize_graph_name_v1("Concurrent Project A"),
        source_fingerprint="concurrent-source-1",
        evidence_refs=({"document_revision_id": "revision-1", "mention_id": "mention-1"},),
    )


async def _acceptance() -> None:
    engine = create_async_engine(_PG_DSN, pool_size=2, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    library_id = uuid.uuid4()
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(
                insert(Organization)
                .values(
                    id=DEFAULT_ORGANIZATION_ID,
                    slug=DEFAULT_ORGANIZATION_SLUG,
                    name="Default Organization",
                    deployment_profile="private",
                    status="active",
                )
                .on_conflict_do_nothing(index_elements=[Organization.id])
            )
            async with connection.begin_nested():
                await connection.execute(
                    insert(Library)
                    .values(
                        id=library_id,
                        organization_id=DEFAULT_ORGANIZATION_ID,
                        slug="p12-concurrency-" + uuid.uuid4().hex[:12],
                        name="P1.2 concurrency",
                        embedding_model="fixture",
                        embedding_dim=3,
                        qdrant_collection="p12-" + uuid.uuid4().hex[:12],
                        lifecycle_mode="managed",
                        index_state="ready",
                    )
                    .on_conflict_do_nothing(index_elements=[Library.id])
                )

        async def resolve_once():
            async with sessions() as db:
                result = await resolve_canonical_entity(db, _request(library_id))
                await db.commit()
                return result

        first, second = await asyncio.gather(resolve_once(), resolve_once())
        assert first.decision.id == second.decision.id
        assert first.canonical_entity is not None
        assert second.canonical_entity is not None

        async with sessions() as db:
            canonical_count = await db.scalar(
                select(func.count(CanonicalEntity.id)).where(CanonicalEntity.library_id == library_id)
            )
            decision_count = await db.scalar(
                select(func.count(EntityResolutionDecision.id)).where(
                    EntityResolutionDecision.library_id == library_id
                )
            )
            assert canonical_count == 1
            assert decision_count == 1

            on_delete = await db.scalar(
                text(
                    """
                    SELECT confdeltype
                    FROM pg_constraint
                    WHERE conname = 'fk_entity_resolution_decisions_candidate'
                    """
                )
            )
            assert on_delete == "n"
    finally:
        await engine.dispose()


def test_concurrent_create_new_is_one_canonical_and_one_decision():
    asyncio.run(_acceptance())
