from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.auth.backend import current_active_user
from app.config import Settings, settings
from app.db import get_db
from app.main import app
from app.models.entity import Entity
from app.models.knowledge_relation import KnowledgeRelation
from app.models.library import Library
from app.models.relation_evidence import RelationEvidence
from app.models.user import User


LIB_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("20000000-0000-0000-0000-000000000002")
ONTOLOGY_ID = uuid.UUID("20000000-0000-0000-0000-000000000003")
ENTITY_TYPE_ID = uuid.UUID("20000000-0000-0000-0000-000000000004")
RELATION_TYPE_ID = uuid.UUID("20000000-0000-0000-0000-000000000005")
ENTITY_ID = uuid.UUID("20000000-0000-0000-0000-000000000006")
RELATION_ID = uuid.UUID("20000000-0000-0000-0000-000000000007")
EVIDENCE_ID = uuid.UUID("20000000-0000-0000-0000-000000000008")
DOC_ID = uuid.UUID("20000000-0000-0000-0000-000000000009")
REVISION_ID = uuid.UUID("20000000-0000-0000-0000-000000000010")


def _lib() -> Library:
    return Library(
        id=LIB_ID,
        slug="lib",
        name="Library",
        qdrant_collection="lib_col",
        embedding_model="m",
        embedding_dim=8,
        chunk_size=1000,
        chunk_overlap=120,
        lifecycle_mode="managed",
        index_state="ready",
    )


def _user() -> User:
    return User(id=USER_ID, email="m6@example.com", is_superuser=True, is_active=True)


def _client_with_db(db):
    async def override_db():
        return db

    async def override_user():
        return _user()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[current_active_user] = override_user
    return TestClient(app)


def test_graph_v03_feature_flag_defaults_off():
    assert Settings().graph_v03_enabled is False


def test_graph_v03_routes_are_mounted():
    paths = set(app.openapi()["paths"])

    assert "/libraries/{slug}/v03/entities" in paths
    assert "/libraries/{slug}/v03/relations" in paths
    assert "/libraries/{slug}/v03/relations/{relation_id}" in paths
    assert "/libraries/{slug}/v03/relations/{relation_id}/evidence" in paths


def test_graph_v03_disabled_returns_404(monkeypatch):
    monkeypatch.setattr(settings, "graph_v03_enabled", False, raising=False)
    db = AsyncMock()

    try:
        with patch("app.deps.load_active_library", new=AsyncMock(return_value=_lib())):
            response = _client_with_db(db).post(
                "/libraries/lib/v03/entities",
                json={
                    "ontology_version_id": str(ONTOLOGY_ID),
                    "entity_type_id": str(ENTITY_TYPE_ID),
                    "canonical_name": "Alice",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404


def test_create_entity_api_returns_created_entity(monkeypatch):
    monkeypatch.setattr(settings, "graph_v03_enabled", True, raising=False)
    db = AsyncMock()
    db.commit = AsyncMock()
    entity = Entity(
        id=ENTITY_ID,
        library_id=LIB_ID,
        ontology_version_id=ONTOLOGY_ID,
        entity_type_id=ENTITY_TYPE_ID,
        canonical_name="Alice",
        normalized_name="alice",
        properties={},
        status="active",
        source_type="manual",
    )

    async def fake_create_entity(_db, library, body):
        assert library.id == LIB_ID
        assert body.canonical_name == "Alice"
        return entity

    try:
        with patch("app.deps.load_active_library", new=AsyncMock(return_value=_lib())):
            monkeypatch.setattr("app.api.v03_graph.graph_entities.create_entity", fake_create_entity)
            response = _client_with_db(db).post(
                "/libraries/lib/v03/entities",
                json={
                    "ontology_version_id": str(ONTOLOGY_ID),
                    "entity_type_id": str(ENTITY_TYPE_ID),
                    "canonical_name": "Alice",
                    "status": "active",
                    "source_type": "manual",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    assert response.json()["id"] == str(ENTITY_ID)
    assert response.json()["normalized_name"] == "alice"
    db.commit.assert_awaited_once()


def test_create_relation_api_returns_clear_409_for_invalid_relation(monkeypatch):
    monkeypatch.setattr(settings, "graph_v03_enabled", True, raising=False)
    db = AsyncMock()

    async def fake_create_relation(_db, _library, _body):
        raise ValueError("active relation_type_constraint not found")

    try:
        with patch("app.deps.load_active_library", new=AsyncMock(return_value=_lib())):
            monkeypatch.setattr("app.api.v03_graph.graph_relations.create_relation", fake_create_relation)
            response = _client_with_db(db).post(
                "/libraries/lib/v03/relations",
                json={
                    "relation_type_id": str(RELATION_TYPE_ID),
                    "source_entity_id": str(ENTITY_ID),
                    "target_entity_id": str(uuid.uuid4()),
                    "status": "pending_review",
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["detail"] == "active relation_type_constraint not found"
    db.commit.assert_not_awaited()


def test_bind_relation_evidence_api_returns_v02_snapshot_fields(monkeypatch):
    monkeypatch.setattr(settings, "graph_v03_enabled", True, raising=False)
    db = AsyncMock()
    db.commit = AsyncMock()
    row = RelationEvidence(
        id=uuid.uuid4(),
        library_id=LIB_ID,
        relation_id=RELATION_ID,
        evidence_id=EVIDENCE_ID,
        document_id=DOC_ID,
        document_revision_id=REVISION_ID,
        support_type="supports",
        quote_text="Alice belongs to Engineering.",
        evidence_text_snapshot="Alice belongs to Engineering.",
        confidence=0.91,
        status="active",
    )

    async def fake_bind_relation_evidence(_db, library, relation_id, body):
        assert library.id == LIB_ID
        assert relation_id == RELATION_ID
        assert body.evidence_id == EVIDENCE_ID
        return row

    try:
        with patch("app.deps.load_active_library", new=AsyncMock(return_value=_lib())):
            monkeypatch.setattr(
                "app.api.v03_graph.graph_relations.bind_relation_evidence",
                fake_bind_relation_evidence,
            )
            response = _client_with_db(db).post(
                f"/libraries/lib/v03/relations/{RELATION_ID}/evidence",
                json={"evidence_id": str(EVIDENCE_ID), "support_type": "supports", "confidence": 0.91},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    payload = response.json()
    assert payload["relation_id"] == str(RELATION_ID)
    assert payload["evidence_id"] == str(EVIDENCE_ID)
    assert payload["document_id"] == str(DOC_ID)
    assert payload["document_revision_id"] == str(REVISION_ID)
    assert payload["evidence_text_snapshot"] == "Alice belongs to Engineering."
    db.commit.assert_awaited_once()


def test_get_relation_api_returns_relation_without_evidence_or_status_transition_side_effects(monkeypatch):
    monkeypatch.setattr(settings, "graph_v03_enabled", True, raising=False)
    db = AsyncMock()
    relation = KnowledgeRelation(
        id=RELATION_ID,
        library_id=LIB_ID,
        ontology_version_id=ONTOLOGY_ID,
        relation_type_id=RELATION_TYPE_ID,
        source_entity_id=ENTITY_ID,
        target_entity_id=uuid.uuid4(),
        properties={},
        status="pending_review",
        review_status="pending_review",
        source_type="manual",
    )

    async def fake_get_relation(_db, library, relation_id):
        assert library.id == LIB_ID
        assert relation_id == RELATION_ID
        return relation

    try:
        with patch("app.deps.load_active_library", new=AsyncMock(return_value=_lib())):
            monkeypatch.setattr("app.api.v03_graph.graph_relations.get_relation", fake_get_relation)
            response = _client_with_db(db).get(f"/libraries/lib/v03/relations/{RELATION_ID}")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["id"] == str(RELATION_ID)
    assert response.json()["status"] == "pending_review"
    db.commit.assert_not_awaited()
