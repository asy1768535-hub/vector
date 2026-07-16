from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.auth.backend import current_active_user
from app.config import settings
from app.db import get_db
from app.main import app
from app.models.graph_publication import GraphPublication
from app.models.graph_publication_item import GraphPublicationItem
from app.models.library import Library
from app.models.user import User
from app.services.graph_publication_activation import (
    GraphPublicationActivationError,
    GraphPublicationActivationResult,
)
from app.services.graph_publication_planner import GraphPublicationPlanResult
from app.services.graph_publication_read import PageResult, PublicationItemView


LIBRARY_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
OTHER_LIBRARY_ID = uuid.UUID("10000000-0000-0000-0000-000000000002")
USER_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
ONTOLOGY_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")
PUBLICATION_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")
PARENT_ID = uuid.UUID("40000000-0000-0000-0000-000000000002")
ITEM_ID = uuid.UUID("50000000-0000-0000-0000-000000000001")
ENTITY_ID = uuid.UUID("60000000-0000-0000-0000-000000000001")
JOB_ID = uuid.UUID("70000000-0000-0000-0000-000000000001")


class _Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def scalars(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None


def _library() -> Library:
    return Library(
        id=LIBRARY_ID,
        slug="v05-api",
        name="v0.5 API",
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection="v05_api",
    )


def _user(*, superuser=True) -> User:
    return User(
        id=USER_ID,
        email="v05@example.com",
        is_superuser=superuser,
        is_active=True,
    )


def _publication(*, status="planned", library_id=LIBRARY_ID) -> GraphPublication:
    return GraphPublication(
        id=PUBLICATION_ID,
        library_id=library_id,
        ontology_version_id=ONTOLOGY_ID,
        status=status,
        source_mode="manual_plan",
        manifest_version="v1",
        policy_version="v1",
        policy_snapshot={"secret_policy_input": "must-not-leak"},
        manifest_hash="a" * 64,
        idempotency_key="must-not-leak",
        include_drafts=False,
        plan_options={"internal": "must-not-leak"},
        parent_publication_id=PARENT_ID,
        entity_count=1,
        relation_count=0,
        blocked_counts={"entity_evidence_incomplete": 1},
        blocked_diagnostics={"internal": "must-not-leak"},
        item_hashes_summary={"entity_hashes": ["b" * 64]},
        error_code=None,
        error_message="must-not-leak",
    )


def _item() -> GraphPublicationItem:
    return GraphPublicationItem(
        id=ITEM_ID,
        publication_id=PUBLICATION_ID,
        library_id=LIBRARY_ID,
        ontology_version_id=ONTOLOGY_ID,
        item_kind="entity",
        entity_id=ENTITY_ID,
        item_hash="b" * 64,
        status="active",
        support_evidence_ids=[str(uuid.UUID("80000000-0000-0000-0000-000000000001"))],
        support_counts={"active_mentions": 1},
        fact_snapshot={
            "canonical_name": "must-not-leak",
            "properties": {"secret": "must-not-leak"},
        },
    )


def _plan_result(*, publication=None, dry_run=False, reused=False):
    publication = publication or _publication()
    return GraphPublicationPlanResult(
        publication=publication,
        items=(),
        manifest_hash=publication.manifest_hash,
        policy_snapshot_hash="c" * 64,
        blocked_counts=dict(publication.blocked_counts),
        dry_run=dry_run,
        reused=reused,
    )


def _client(db, *, superuser=True):
    async def override_db():
        return db

    async def override_user():
        return _user(superuser=superuser)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[current_active_user] = override_user
    return TestClient(app)


def _clear_overrides():
    app.dependency_overrides.clear()


def test_v05_publication_routes_are_mounted():
    paths = set(app.openapi()["paths"])
    prefix = "/libraries/{slug}/v05/graph-publications"
    assert {
        f"{prefix}/plan",
        f"{prefix}/",
        f"{prefix}/active",
        f"{prefix}/{{publication_id}}",
        f"{prefix}/{{publication_id}}/items",
        f"{prefix}/{{publication_id}}/activate",
        f"{prefix}/{{publication_id}}/cancel",
        f"{prefix}/{{publication_id}}/rollback",
    }.issubset(paths)


def test_degraded_active_read_is_visible_and_reports_unhealthy_when_feature_disabled(monkeypatch):
    monkeypatch.setattr(settings, "graph_publication_enabled", False)
    db = AsyncMock()
    publication = _publication(status="degraded")
    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch(
                "app.api.v05_graph_publications.graph_publication_read.list_current_graph_publication",
                new=AsyncMock(return_value=publication),
            ),
        ):
            response = _client(db).get("/libraries/v05-api/v05/graph-publications/active")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "degraded"
    assert payload["healthy"] is False
    assert payload["publication_enabled"] is False
    assert "error_message" not in payload
    assert "policy_snapshot" not in payload


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("plan", {}),
        (f"{PUBLICATION_ID}/activate", {}),
        (f"{PUBLICATION_ID}/cancel", {}),
        (f"{PUBLICATION_ID}/rollback", {}),
    ],
)
def test_disabled_flag_blocks_every_mutating_command(monkeypatch, path, body):
    monkeypatch.setattr(settings, "graph_publication_enabled", False)
    db = AsyncMock()
    try:
        with patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())):
            response = _client(db).post(
                f"/libraries/v05-api/v05/graph-publications/{path}",
                json=body,
            )
    finally:
        _clear_overrides()

    assert response.status_code == 503
    assert response.json()["detail"] == "publication_disabled"


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("plan", {"source_mode": "rollback"}),
        ("plan", {"idempotency_key": ""}),
        ("plan", {"unexpected": True}),
        (f"{PUBLICATION_ID}/activate", {"expected_manifest_hash": "A" * 64}),
        (f"{PUBLICATION_ID}/cancel", {"reason_code": "contains-dash"}),
        (f"{PUBLICATION_ID}/rollback", {"unexpected": True}),
    ],
)
def test_command_dtos_reject_invalid_or_extra_fields(monkeypatch, path, body):
    monkeypatch.setattr(settings, "graph_publication_enabled", True)
    db = AsyncMock()
    try:
        with patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())):
            response = _client(db).post(
                f"/libraries/v05-api/v05/graph-publications/{path}",
                json=body,
            )
    finally:
        _clear_overrides()
    assert response.status_code == 422


def test_plan_dry_run_passes_fence_rolls_back_and_returns_sanitized_response(monkeypatch):
    monkeypatch.setattr(settings, "graph_publication_enabled", True)
    db = AsyncMock()
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    result = _plan_result(dry_run=True)
    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch(
                "app.api.v05_graph_publications.plan_graph_publication",
                new=AsyncMock(return_value=result),
            ) as planner,
        ):
            response = _client(db).post(
                "/libraries/v05-api/v05/graph-publications/plan",
                json={
                    "dry_run": True,
                    "idempotency_key": "plan-key",
                    "expected_parent_publication_id": str(PARENT_ID),
                },
            )
    finally:
        _clear_overrides()

    assert response.status_code == 201
    planner.assert_awaited_once()
    assert planner.await_args.kwargs["expected_parent_publication_id"] == PARENT_ID
    assert planner.await_args.kwargs["dry_run"] is True
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
    payload = response.json()
    assert payload["dry_run"] is True
    assert payload["policy_snapshot_hash"] == "c" * 64
    for forbidden in (
        "policy_snapshot",
        "plan_options",
        "blocked_diagnostics",
        "item_hashes_summary",
        "idempotency_key",
        "error_message",
    ):
        assert forbidden not in payload


def test_read_permission_does_not_grant_plan_or_admin_commands(monkeypatch):
    monkeypatch.setattr(settings, "graph_publication_enabled", True)
    db = AsyncMock()

    def read_only(_user_id, _slug, action):
        return action == "read"

    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch("app.deps.has_permission", side_effect=read_only),
            patch(
                "app.api.v05_graph_publications.graph_publication_read.get_graph_publication",
                new=AsyncMock(return_value=_publication()),
            ),
        ):
            client = _client(db, superuser=False)
            read_response = client.get(
                f"/libraries/v05-api/v05/graph-publications/{PUBLICATION_ID}"
            )
            plan_response = client.post(
                "/libraries/v05-api/v05/graph-publications/plan",
                json={},
            )
            activate_response = client.post(
                f"/libraries/v05-api/v05/graph-publications/{PUBLICATION_ID}/activate",
                json={},
            )
            rollback_response = client.post(
                f"/libraries/v05-api/v05/graph-publications/{PUBLICATION_ID}/rollback",
                json={},
            )
    finally:
        _clear_overrides()

    assert read_response.status_code == 200
    assert plan_response.status_code == 403
    assert activate_response.status_code == 403
    assert rollback_response.status_code == 403


def test_insert_permission_grants_plan_to_non_admin(monkeypatch):
    monkeypatch.setattr(settings, "graph_publication_enabled", True)
    db = AsyncMock()
    db.rollback = AsyncMock()

    def insert_only(_user_id, _slug, action):
        return action == "insert"

    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch("app.deps.has_permission", side_effect=insert_only),
            patch(
                "app.api.v05_graph_publications.plan_graph_publication",
                new=AsyncMock(return_value=_plan_result(dry_run=True)),
            ),
        ):
            response = _client(db, superuser=False).post(
                "/libraries/v05-api/v05/graph-publications/plan",
                json={"dry_run": True},
            )
    finally:
        _clear_overrides()

    assert response.status_code == 201


def test_insert_permission_grants_cancel_to_non_admin(monkeypatch):
    monkeypatch.setattr(settings, "graph_publication_enabled", True)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_Result(), _Result([_publication()])])
    db.commit = AsyncMock()

    def insert_only(_user_id, _slug, action):
        return action == "insert"

    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch("app.deps.has_permission", side_effect=insert_only),
            patch("app.api.v05_graph_publications.audit_log.record", new=AsyncMock()),
        ):
            response = _client(db, superuser=False).post(
                f"/libraries/v05-api/v05/graph-publications/{PUBLICATION_ID}/cancel",
                json={},
            )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_activate_preflights_scope_ends_read_transaction_and_forwards_manifest_fence(monkeypatch):
    monkeypatch.setattr(settings, "graph_publication_enabled", True)
    db = AsyncMock()
    db.rollback = AsyncMock()
    publication = _publication(status="active")
    expected_hash = publication.manifest_hash

    async def activate(*args, **kwargs):
        db.rollback.assert_awaited_once()
        assert kwargs["expected_manifest_hash"] == expected_hash
        assert kwargs["command_idempotency_key"] == "activate-retry"
        return GraphPublicationActivationResult(
            publication=publication,
            previous_publication_id=PARENT_ID,
            idempotent=True,
        )

    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch(
                "app.api.v05_graph_publications.graph_publication_read.get_graph_publication",
                new=AsyncMock(return_value=publication),
            ),
            patch("app.api.v05_graph_publications.activate_graph_publication", new=activate),
        ):
            response = _client(db).post(
                f"/libraries/v05-api/v05/graph-publications/{PUBLICATION_ID}/activate",
                json={
                    "idempotency_key": "activate-retry",
                    "expected_manifest_hash": expected_hash,
                },
            )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json()["status"] == "active"
    assert response.json()["healthy"] is True


def test_activate_cannot_cross_library_scope(monkeypatch):
    monkeypatch.setattr(settings, "graph_publication_enabled", True)
    db = AsyncMock()
    activate = AsyncMock()
    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch(
                "app.api.v05_graph_publications.graph_publication_read.get_graph_publication",
                new=AsyncMock(return_value=None),
            ),
            patch("app.api.v05_graph_publications.activate_graph_publication", new=activate),
        ):
            response = _client(db).post(
                f"/libraries/v05-api/v05/graph-publications/{PUBLICATION_ID}/activate",
                json={},
            )
    finally:
        _clear_overrides()

    assert response.status_code == 404
    activate.assert_not_awaited()


def test_activation_error_is_sanitized_to_code_only(monkeypatch):
    monkeypatch.setattr(settings, "graph_publication_enabled", True)
    db = AsyncMock()
    db.rollback = AsyncMock()
    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch(
                "app.api.v05_graph_publications.graph_publication_read.get_graph_publication",
                new=AsyncMock(return_value=_publication()),
            ),
            patch(
                "app.api.v05_graph_publications.activate_graph_publication",
                new=AsyncMock(
                    side_effect=GraphPublicationActivationError(
                        "expected_manifest_mismatch",
                        "internal details must not leak",
                    )
                ),
            ),
        ):
            response = _client(db).post(
                f"/libraries/v05-api/v05/graph-publications/{PUBLICATION_ID}/activate",
                json={},
            )
    finally:
        _clear_overrides()

    assert response.status_code == 409
    assert response.json() == {"detail": "expected_manifest_mismatch"}


def test_cancel_only_planned_publication_and_records_sanitized_audit(monkeypatch):
    monkeypatch.setattr(settings, "graph_publication_enabled", True)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_Result(), _Result([_publication()])])
    db.commit = AsyncMock()
    audit = AsyncMock()
    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch("app.api.v05_graph_publications.audit_log.record", new=audit),
        ):
            response = _client(db).post(
                f"/libraries/v05-api/v05/graph-publications/{PUBLICATION_ID}/cancel",
                json={"idempotency_key": "cancel-key", "reason_code": "operator_cancelled"},
            )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    db.commit.assert_awaited_once()
    audit.assert_awaited_once()
    target = audit.await_args.args[3]
    assert target["reason_code"] == "operator_cancelled"
    assert "idempotency_key" not in target
    assert len(target["command_idempotency_hash"]) == 64


def test_cancel_rejects_non_planned_publication(monkeypatch):
    monkeypatch.setattr(settings, "graph_publication_enabled", True)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_Result(), _Result([_publication(status="active")])])
    db.commit = AsyncMock()
    try:
        with patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())):
            response = _client(db).post(
                f"/libraries/v05-api/v05/graph-publications/{PUBLICATION_ID}/cancel",
                json={},
            )
    finally:
        _clear_overrides()

    assert response.status_code == 409
    assert response.json()["detail"] == "publication_not_planned"
    db.commit.assert_not_awaited()


def test_rollback_dry_run_uses_admin_scope_and_persists_nothing(monkeypatch):
    monkeypatch.setattr(settings, "graph_publication_enabled", True)
    db = AsyncMock()
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    result = _plan_result(dry_run=True)
    rollback = AsyncMock(return_value=result)
    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch(
                "app.api.v05_graph_publications.graph_publication_read.get_graph_publication",
                new=AsyncMock(return_value=_publication(status="superseded")),
            ),
            patch("app.api.v05_graph_publications.plan_graph_publication_rollback", new=rollback),
        ):
            response = _client(db).post(
                f"/libraries/v05-api/v05/graph-publications/{PUBLICATION_ID}/rollback",
                json={"idempotency_key": "rollback-key", "dry_run": True},
            )
    finally:
        _clear_overrides()

    assert response.status_code == 201
    assert rollback.await_args.kwargs["dry_run"] is True
    assert rollback.await_args.kwargs["idempotency_key"] == "rollback-key"
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


def test_item_response_contains_safe_job_ids_but_no_fact_or_evidence_payload(monkeypatch):
    db = AsyncMock()
    view = PublicationItemView(item=_item(), source_job_ids=(JOB_ID,))
    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch(
                "app.api.v05_graph_publications.graph_publication_read.list_graph_publication_items",
                new=AsyncMock(return_value=PageResult(rows=(view,), total=1)),
            ),
        ):
            response = _client(db).get(
                f"/libraries/v05-api/v05/graph-publications/{PUBLICATION_ID}/items",
                params={"page": 1, "page_size": 100},
            )
    finally:
        _clear_overrides()

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["source_job_ids"] == [str(JOB_ID)]
    assert item["entity_id"] == str(ENTITY_ID)
    for forbidden in (
        "fact_snapshot",
        "canonical_name",
        "properties",
        "quote_text",
        "evidence_text_snapshot",
        "source_span",
    ):
        assert forbidden not in item


def test_page_size_is_capped_at_500_before_query(monkeypatch):
    db = AsyncMock()
    reader = AsyncMock()
    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch(
                "app.api.v05_graph_publications.graph_publication_read.list_graph_publication_items",
                new=reader,
            ),
        ):
            response = _client(db).get(
                f"/libraries/v05-api/v05/graph-publications/{PUBLICATION_ID}/items",
                params={"page_size": 501},
            )
    finally:
        _clear_overrides()

    assert response.status_code == 422
    reader.assert_not_awaited()
