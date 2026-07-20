from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, event, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.config import Settings
from app.models.entity import Entity
from app.models.entity_type import EntityType
from app.models.graph_publication import GraphPublication
from app.models.graph_publication_item import GraphPublicationItem
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.schemas.v07_entity_linking import EntityLinkingResolveRequest
from app.services import entity_linking


_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL database for v0.7.",
)


@dataclass(frozen=True, slots=True)
class SeededPublication:
    library_id: uuid.UUID
    ontology_id: uuid.UUID
    publication_id: uuid.UUID
    exact_entity_id: uuid.UUID
    lexical_entity_id: uuid.UUID


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def _seed_publication(Session) -> SeededPublication:
    library_id = uuid.uuid4()
    ontology_id = uuid.uuid4()
    publication_id = uuid.uuid4()
    organization_type_id = uuid.uuid4()
    team_type_id = uuid.uuid4()
    exact_entity_id = uuid.uuid4()
    lexical_entity_id = uuid.uuid4()
    shared_organization_id = uuid.uuid4()
    shared_team_id = uuid.uuid4()
    unpublished_entity_id = uuid.uuid4()
    now = datetime.now(UTC)
    slug = "v07-runtime-" + uuid.uuid4().hex[:12]

    async with Session() as db:
        db.add(
            Library(
                id=library_id,
                slug=slug,
                name=slug,
                embedding_model="bge-m3",
                embedding_dim=1024,
                qdrant_collection=slug,
            )
        )
        await db.flush()
        db.add(
            OntologyVersion(
                id=ontology_id,
                library_id=library_id,
                version_key="default",
                version_no=1,
                status="active",
                published_at=now,
            )
        )
        await db.flush()
        db.add_all(
            [
                EntityType(
                    id=organization_type_id,
                    library_id=library_id,
                    ontology_version_id=ontology_id,
                    key="organization",
                    label="Organization",
                    properties_schema={},
                    status="active",
                ),
                EntityType(
                    id=team_type_id,
                    library_id=library_id,
                    ontology_version_id=ontology_id,
                    key="team",
                    label="Team",
                    properties_schema={},
                    status="active",
                ),
            ]
        )
        await db.flush()
        entities = [
            Entity(
                id=exact_entity_id,
                library_id=library_id,
                ontology_version_id=ontology_id,
                entity_type_id=organization_type_id,
                canonical_name="Alpha Systems",
                normalized_name="alpha systems",
                properties={"private_marker": "must-never-appear"},
                status="active",
                source_type="manual",
            ),
            Entity(
                id=lexical_entity_id,
                library_id=library_id,
                ontology_version_id=ontology_id,
                entity_type_id=organization_type_id,
                canonical_name="Acme Holdings",
                normalized_name="acme holdings",
                properties={},
                status="active",
                source_type="manual",
            ),
            Entity(
                id=shared_organization_id,
                library_id=library_id,
                ontology_version_id=ontology_id,
                entity_type_id=organization_type_id,
                canonical_name="Shared Center",
                normalized_name="shared center",
                properties={},
                status="active",
                source_type="manual",
            ),
            Entity(
                id=shared_team_id,
                library_id=library_id,
                ontology_version_id=ontology_id,
                entity_type_id=team_type_id,
                canonical_name="Shared Center",
                normalized_name="shared center",
                properties={},
                status="active",
                source_type="manual",
            ),
            Entity(
                id=unpublished_entity_id,
                library_id=library_id,
                ontology_version_id=ontology_id,
                entity_type_id=organization_type_id,
                canonical_name="Hidden Scope",
                normalized_name="hidden scope",
                properties={},
                status="active",
                source_type="manual",
            ),
        ]
        db.add_all(entities)
        await db.flush()
        db.add(
            GraphPublication(
                id=publication_id,
                library_id=library_id,
                ontology_version_id=ontology_id,
                status="active",
                source_mode="initial_seed",
                manifest_version="v1",
                policy_version="v1",
                policy_snapshot={},
                manifest_hash=_sha("v07-runtime-manifest"),
                idempotency_key="v07-runtime-publication",
                include_drafts=False,
                plan_options={},
                entity_count=4,
                relation_count=0,
                blocked_counts={},
                blocked_diagnostics={},
                item_hashes_summary={},
                activated_at=now,
                last_reconciled_at=now,
            )
        )
        await db.flush()
        published_entities = entities[:4]
        db.add_all(
            [
                GraphPublicationItem(
                    id=uuid.uuid4(),
                    publication_id=publication_id,
                    library_id=library_id,
                    ontology_version_id=ontology_id,
                    item_kind="entity",
                    entity_id=row.id,
                    item_hash=_sha(f"v07-runtime-item-{index}"),
                    status="active",
                    support_evidence_ids=[],
                    support_counts={},
                    fact_snapshot={},
                )
                for index, row in enumerate(published_entities)
            ]
        )
        await db.commit()
    return SeededPublication(
        library_id=library_id,
        ontology_id=ontology_id,
        publication_id=publication_id,
        exact_entity_id=exact_entity_id,
        lexical_entity_id=lexical_entity_id,
    )


async def _cleanup(Session, library_id: uuid.UUID) -> None:
    async with Session() as db:
        await db.execute(delete(Library).where(Library.id == library_id))
        await db.commit()


async def _run_acceptance(monkeypatch) -> None:
    engine = create_async_engine(_DSN, pool_size=4, max_overflow=2)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    seed: SeededPublication | None = None
    try:
        async with engine.connect() as connection:
            assert (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one() == "0023"
        seed = await _seed_publication(Session)
        config = Settings(_env_file=None, entity_linking_enabled=False)
        async with Session() as db:
            library = await db.get(Library, seed.library_id)
            assert library is not None

            wrong_fence = EntityLinkingResolveRequest(
                ontology_version_id=seed.ontology_id,
                expected_publication_id=uuid.uuid4(),
                mentions=[{"text": "Alpha Systems"}],
            )
            with pytest.raises(entity_linking.EntityLinkingServiceError) as exc_info:
                await entity_linking.execute_entity_linking(
                    db,
                    library,
                    wrong_fence,
                    config=config,
                )
            assert exc_info.value.code == "publication_changed"
            await db.rollback()
            library = await db.get(Library, seed.library_id)
            assert library is not None

            statements: list[str] = []

            def capture_statement(_conn, _cursor, statement, _parameters, _context, _many):
                statements.append(statement.strip().split(None, 1)[0].upper())

            event.listen(engine.sync_engine, "before_cursor_execute", capture_statement)
            try:
                request = EntityLinkingResolveRequest(
                    ontology_version_id=seed.ontology_id,
                    expected_publication_id=seed.publication_id,
                    mentions=[
                        {"text": "Alpha Systems", "entity_type_key": "organization"},
                        {"text": "Acme Holding", "entity_type_key": "organization"},
                        {"text": "Shared Center"},
                        {"text": "Hidden Scope", "entity_type_key": "organization"},
                    ],
                    max_candidates_per_mention=5,
                )
                response = await entity_linking.execute_entity_linking(
                    db,
                    library,
                    request,
                    config=config,
                )
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", capture_statement)

            assert statements == ["SELECT"] * 8
            assert [row.status for row in response.results] == [
                "linked",
                "linked",
                "ambiguous",
                "not_found",
            ]
            assert [row.method for row in response.results] == [
                "exact_canonical",
                "lexical_v1",
                None,
                None,
            ]
            assert response.results[0].selected.entity_id == seed.exact_entity_id
            assert response.results[1].selected.entity_id == seed.lexical_entity_id
            assert [row.entity_type_key for row in response.results[2].candidates] == [
                "organization",
                "team",
            ]
            assert response.counts.model_dump() == {
                "mentions": 4,
                "linked_exact": 1,
                "linked_lexical": 1,
                "ambiguous": 1,
                "not_found": 1,
                "candidates": 2,
            }
            assert "must-never-appear" not in str(response.model_dump(mode="json"))
            assert not db.new and not db.dirty and not db.deleted
            await db.rollback()

        original_load = entity_linking.load_published_entity_candidates

        async def supersede_after_projection(db, snapshot, schema):
            candidates = await original_load(db, snapshot, schema)
            async with Session() as other:
                await other.execute(
                    update(GraphPublication)
                    .where(GraphPublication.id == seed.publication_id)
                    .values(status="superseded", superseded_at=datetime.now(UTC))
                )
                await other.commit()
            return candidates

        monkeypatch.setattr(
            entity_linking,
            "load_published_entity_candidates",
            supersede_after_projection,
        )
        async with Session() as db:
            library = await db.get(Library, seed.library_id)
            fence_request = EntityLinkingResolveRequest(
                ontology_version_id=seed.ontology_id,
                expected_publication_id=seed.publication_id,
                mentions=[{"text": "Alpha Systems"}],
            )
            with pytest.raises(entity_linking.EntityLinkingServiceError) as exc_info:
                await entity_linking.execute_entity_linking(
                    db,
                    library,
                    fence_request,
                    config=config,
                )
            assert exc_info.value.code == "publication_changed"
            await db.rollback()
    finally:
        if seed is not None:
            await _cleanup(Session, seed.library_id)
        await engine.dispose()


def test_v07_real_postgres_publication_linking_and_fences(monkeypatch):
    asyncio.run(_run_acceptance(monkeypatch))
