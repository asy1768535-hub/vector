from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.api import chat_graph_context as api_module
from app.config import settings
from app.db import get_db
from app.main import app
from app.models.library import Library
from app.models.user import User
from app.schemas.chat_graph_context import ChatGraphContextResponse
from app.services import chat_graph_context
from app.services.organization_authorization import OrganizationAuthorizationError


PUBLICATION_A = uuid.UUID("71000000-0000-0000-0000-000000000001")
PUBLICATION_B = uuid.UUID("71000000-0000-0000-0000-000000000002")
ONTOLOGY = uuid.UUID("71000000-0000-0000-0000-000000000003")
ENTITY_A = uuid.UUID("71000000-0000-0000-0000-000000000004")
ENTITY_B = uuid.UUID("71000000-0000-0000-0000-000000000005")
ENTITY_C = uuid.UUID("71000000-0000-0000-0000-000000000006")
RELATION = uuid.UUID("71000000-0000-0000-0000-000000000007")
CHUNK = uuid.UUID("71000000-0000-0000-0000-000000000008")


def _row(
    publication_id: uuid.UUID,
    *,
    item_kind: str,
    entity_id: uuid.UUID | None = None,
    relation_id: uuid.UUID | None = None,
    source_entity_id: uuid.UUID | None = None,
    target_entity_id: uuid.UUID | None = None,
    activated_at: datetime | None = None,
    publication_status: str = "active",
):
    return SimpleNamespace(
        publication_id=publication_id,
        ontology_version_id=ONTOLOGY,
        publication_status=publication_status,
        activated_at=activated_at or datetime(2026, 7, 27, tzinfo=UTC),
        item_kind=item_kind,
        entity_id=entity_id,
        relation_id=relation_id,
        source_entity_id=source_entity_id,
        target_entity_id=target_entity_id,
    )


def test_candidate_selection_prefers_more_exact_facts_and_orders_seeds():
    rows = [
        _row(PUBLICATION_A, item_kind="entity", entity_id=ENTITY_C),
        _row(PUBLICATION_B, item_kind="entity", entity_id=ENTITY_B),
        _row(
            PUBLICATION_B,
            item_kind="relation",
            relation_id=RELATION,
            source_entity_id=ENTITY_A,
            target_entity_id=ENTITY_C,
        ),
    ]

    selected = chat_graph_context._select_candidate(rows)

    assert selected is not None
    assert selected.publication_id == PUBLICATION_B
    assert selected.direct_entity_ids == {ENTITY_B}
    assert selected.endpoint_entity_ids == {ENTITY_A, ENTITY_C}
    assert len(selected.fact_ids) == 2


def test_candidate_selection_rejects_invalid_published_relation_shape():
    rows = [_row(PUBLICATION_A, item_kind="relation", relation_id=RELATION)]

    try:
        chat_graph_context._select_candidate(rows)
    except chat_graph_context.ChatGraphContextServiceError as exc:
        assert exc.code == "graph_publication_invariant_failed"
    else:
        raise AssertionError("invalid relation shape must fail closed")


def test_evidence_identity_selection_is_deterministic_and_bounded():
    values = [uuid.UUID(int=index + 1) for index in range(8)]

    selected = chat_graph_context._bounded_evidence_ids(
        values[4:],
        values[:5],
        (values[2], None),
    )

    assert selected == tuple(sorted(values, key=str))

    overflow = [uuid.UUID(int=index + 1) for index in range(201)]
    try:
        chat_graph_context._bounded_evidence_ids(overflow)
    except chat_graph_context.ChatGraphContextServiceError as exc:
        assert exc.code == "graph_retrieval_limit_exceeded"
    else:
        raise AssertionError("oversized evidence identity sets must fail closed")


def _library() -> Library:
    return Library(
        id=uuid.UUID("71000000-0000-0000-0000-000000000010"),
        slug="demo",
        name="Demo",
        qdrant_collection="demo",
        embedding_model="model",
        embedding_dim=3,
        vector_distance="cosine",
    )


def _user() -> User:
    return User(
        id=uuid.UUID("71000000-0000-0000-0000-000000000011"),
        email="reader@example.com",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        is_verified=True,
    )


def _client(db: AsyncMock) -> TestClient:
    from app.auth.backend import current_active_user

    app.dependency_overrides[current_active_user] = _user
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def _clear_overrides() -> None:
    app.dependency_overrides.clear()


def test_chat_graph_context_route_returns_successful_empty_response(monkeypatch):
    monkeypatch.setattr(settings, "graph_retrieval_enabled", True)
    monkeypatch.setattr(settings, "organization_authorization_enabled", True)
    db = AsyncMock()
    service = AsyncMock(
        return_value=ChatGraphContextResponse(
            contract_version="v1",
            chunk_id=CHUNK,
            exact_fact_count=0,
            exact_seed_count=0,
            exact_seeds_truncated=False,
            graph=None,
        )
    )
    try:
        with (
            patch("app.deps.authorize_library", new=AsyncMock(return_value=_library())),
            patch.object(api_module.chat_graph_context, "load_chat_graph_context", service),
        ):
            response = _client(db).get(
                "/libraries/demo/chat/graph-context",
                params={"chunk_id": str(CHUNK)},
            )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json()["graph"] is None
    assert response.json()["exact_fact_count"] == 0
    service.assert_awaited_once()


def test_chat_graph_context_route_maps_hidden_chunk_and_rolls_back(monkeypatch):
    monkeypatch.setattr(settings, "graph_retrieval_enabled", True)
    monkeypatch.setattr(settings, "organization_authorization_enabled", True)
    db = AsyncMock()
    db.rollback = AsyncMock()
    service = AsyncMock(
        side_effect=chat_graph_context.ChatGraphContextServiceError(
            "citation_chunk_not_found"
        )
    )
    try:
        with (
            patch("app.deps.authorize_library", new=AsyncMock(return_value=_library())),
            patch.object(api_module.chat_graph_context, "load_chat_graph_context", service),
        ):
            response = _client(db).get(
                "/libraries/demo/chat/graph-context",
                params={"chunk_id": str(CHUNK)},
            )
    finally:
        _clear_overrides()

    assert response.status_code == 404
    assert response.json() == {"detail": "citation_chunk_not_found"}
    db.rollback.assert_awaited_once()


def test_chat_graph_context_route_requires_library_read(monkeypatch):
    monkeypatch.setattr(settings, "graph_retrieval_enabled", True)
    monkeypatch.setattr(settings, "organization_authorization_enabled", True)
    db = AsyncMock()
    try:
        with patch(
            "app.deps.authorize_library",
            new=AsyncMock(side_effect=OrganizationAuthorizationError("forbidden")),
        ):
            response = _client(db).get(
                "/libraries/demo/chat/graph-context",
                params={"chunk_id": str(CHUNK)},
            )
    finally:
        _clear_overrides()

    assert response.status_code == 403
    assert response.json() == {"detail": "forbidden"}
