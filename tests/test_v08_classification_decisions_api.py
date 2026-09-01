from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import deps as deps_module
from app.api import classification_decisions as api
from app.auth.backend import current_active_user, current_cookie_user
from app.config import settings
from app.db import get_db
from app.main import app
from app.models.classification_decision import (
    DocumentClassificationDecision,
    DocumentClassificationDecisionSet,
    DocumentClassificationProposal,
    DocumentClassificationRun,
)
from app.models.classification_taxonomy import ClassificationLabel
from app.models.classification_taxonomy import ClassificationTaxonomy
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.library import Library
from app.models.organization import Organization
from app.models.user import User
from app.services.classification_decision_contracts import ClassificationDecisionError
from app.services.classification_decisions import (
    ClassificationReviewItem,
    ClassificationReviewPage,
    ClassificationRunResult,
    EffectiveClassification,
)
from app.services.organization_authorization import OrganizationAuthorizationError


NOW = datetime(2026, 7, 22, 19, 0, tzinfo=timezone.utc)
HASH = "a" * 64


def _user(*, superuser: bool = False) -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@example.com",
        hashed_password="hash",
        is_active=True,
        is_superuser=superuser,
        is_verified=True,
        created_at=NOW,
    )


def _organization() -> Organization:
    return Organization(
        id=uuid.uuid4(),
        slug="example",
        name="Example Organization",
        deployment_profile="hosted",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )


def _library(organization: Organization) -> Library:
    return Library(
        id=uuid.uuid4(),
        organization_id=organization.id,
        slug="legal",
        name="Legal Library",
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection="legal",
        index_state="ready",
        created_at=NOW,
    )


def _effective(
    library: Library,
    *,
    source: str = "manual",
) -> EffectiveClassification:
    document_id, revision_id, taxonomy_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    document = Document(
        id=document_id,
        library_id=library.id,
        title="Confidential investigation title",
        content_hash=HASH,
        current_revision=1,
        current_revision_id=revision_id,
        latest_revision_id=revision_id,
        status="ready",
        created_at=NOW,
        updated_at=NOW,
    )
    revision = DocumentRevision(
        id=revision_id,
        document_id=document_id,
        library_id=library.id,
        revision_no=1,
        title=document.title,
        content_hash=HASH,
        parser_name="test",
        parser_version="1",
        chunking_strategy="fixed",
        chunking_strategy_version="1",
        status="ready",
        created_at=NOW,
        updated_at=NOW,
        finished_at=NOW,
    )
    label = ClassificationLabel(
        id=uuid.uuid4(),
        taxonomy_version_id=taxonomy_id,
        key="legal",
        label="Legal",
        status="active",
        sort_order=0,
        created_at=NOW,
        updated_at=NOW,
    )
    decision_set = DocumentClassificationDecisionSet(
        id=uuid.uuid4(),
        library_id=library.id,
        document_id=document_id,
        document_revision_id=revision_id,
        taxonomy_version_id=taxonomy_id,
        source=source,
        lifecycle="effective",
        generation_no=2,
        reviewed_at=NOW if source == "manual" else None,
        created_at=NOW,
        updated_at=NOW,
    )
    decision = DocumentClassificationDecision(
        id=uuid.uuid4(),
        decision_set_id=decision_set.id,
        label_id=label.id,
        role="primary",
        ordinal=0,
        confidence_micros=None,
        created_at=NOW,
    )
    return EffectiveClassification(
        document=document,
        revision=revision,
        state="classified",
        decision_set=decision_set,
        decisions=((decision, label),),
        latest_run=None,
    )


def _run(library: Library, effective: EffectiveClassification) -> DocumentClassificationRun:
    return DocumentClassificationRun(
        id=uuid.uuid4(),
        library_id=library.id,
        document_id=effective.document.id,
        document_revision_id=effective.revision.id,
        revision_content_hash=HASH,
        taxonomy_version_id=effective.decision_set.taxonomy_version_id,
        enabled_label_set_hash="b" * 64,
        policy_version="classification-policy-v1",
        min_confidence_micros=900_000,
        min_margin_micros=150_000,
        max_secondary_labels=8,
        classifier_version="classifier-v1",
        model_provider="local",
        model_name="model-v1",
        model_config_hash="c" * 64,
        prompt_version="prompt-v1",
        input_fingerprint="d" * 64,
        idempotency_key="e" * 64,
        generation_no=1,
        retry_generation=0,
        trigger_type="revision_ready",
        status="pending_review",
        reason_codes=["primary_confidence_below_threshold"],
        created_at=NOW,
        updated_at=NOW,
        finished_at=NOW,
    )


def test_routes_are_default_off_before_library_scope_probe():
    user = _user()
    db = AsyncMock()
    app.dependency_overrides[current_cookie_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    try:
        with (
            patch.object(settings, "classification_decision_enabled", False),
            patch.object(deps_module, "load_active_library", new=AsyncMock()) as load,
        ):
            response = TestClient(app).get("/libraries/secret/classifications/reviews")
        assert response.status_code == 404
        assert response.json() == {"detail": "not found"}
        load.assert_not_awaited()
    finally:
        app.dependency_overrides.clear()


def test_platform_superuser_without_customer_authority_is_forbidden():
    user, organization = _user(superuser=True), _organization()
    library = _library(organization)
    app.dependency_overrides[current_cookie_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with (
            patch.object(settings, "classification_decision_enabled", True),
            patch.object(
                deps_module,
                "load_active_library",
                new=AsyncMock(return_value=library),
            ),
            patch.object(
                api,
                "authorize_library_management",
                new=AsyncMock(
                    side_effect=OrganizationAuthorizationError(
                        "organization_library_forbidden"
                    )
                ),
            ),
            patch.object(api, "list_classification_review_runs", new=AsyncMock()) as listing,
        ):
            response = TestClient(app).get(
                f"/libraries/{library.slug}/classifications/reviews"
            )
        assert response.status_code == 403
        assert response.json() == {"detail": "forbidden"}
        listing.assert_not_awaited()
        assert organization.name not in response.text
    finally:
        app.dependency_overrides.clear()


def test_bearer_api_key_cannot_enter_cookie_review_route():
    authorization_scheme = "".join(("Bea", "rer"))
    with patch.object(settings, "classification_decision_enabled", True):
        response = TestClient(app).get(
            "/libraries/legal/classifications/reviews",
            headers={
                "Authorization": f"{authorization_scheme} vk_not_a_cookie_session"
            },
        )
    assert response.status_code == 401


def test_review_list_returns_document_fence_effective_labels_and_choices():
    user, organization = _user(), _organization()
    library = _library(organization)
    effective = _effective(library)
    run = _run(library, effective)
    proposal = DocumentClassificationProposal(
        id=uuid.uuid4(),
        run_id=run.id,
        label_id=effective.decisions[0][1].id,
        role="primary",
        rank=0,
        confidence_micros=850_000,
        status="pending_review",
        reason_codes=["primary_confidence_below_threshold"],
        created_at=NOW,
        updated_at=NOW,
    )
    taxonomy = ClassificationTaxonomy(
        id=run.taxonomy_version_id,
        organization_id=organization.id,
        version_key="document-category",
        version_no=1,
        status="active",
        activated_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    page = ClassificationReviewPage(
        items=(
            ClassificationReviewItem(
                run=run,
                proposals=((proposal, effective.decisions[0][1]),),
                document=effective.document,
                effective_decision_set=effective.decision_set,
                effective_decisions=effective.decisions,
            ),
        ),
        total=1,
        taxonomy=taxonomy,
        available_labels=(effective.decisions[0][1],),
    )
    context = api.ClassificationManagementContext(user, library)
    app.dependency_overrides[api.require_classification_management] = lambda: context
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with patch.object(
            api,
            "list_classification_review_runs",
            new=AsyncMock(return_value=page),
        ):
            response = TestClient(app).get(
                f"/libraries/{library.slug}/classifications/reviews?limit=20&offset=0"
            )
        assert response.status_code == 200
        body = response.json()
        assert body["taxonomy_version_id"] == str(taxonomy.id)
        assert body["available_labels"][0]["label"] == "Legal"
        assert body["items"][0]["document_title"] == effective.document.title
        assert body["items"][0]["effective_decision_set_id"] == str(
            effective.decision_set.id
        )
        assert body["items"][0]["effective_source"] == "manual"
        assert body["items"][0]["effective_decisions"][0]["label"] == "Legal"
        for forbidden in ("model_config_hash", "input_fingerprint", "hashed_password"):
            assert forbidden not in response.text.lower()
    finally:
        app.dependency_overrides.clear()


def test_review_commits_constructs_fenced_command_and_returns_bounded_projection():
    user, organization = _user(), _organization()
    library = _library(organization)
    effective = _effective(library)
    run = _run(library, effective)
    context = api.ClassificationManagementContext(user, library)
    db = AsyncMock()
    app.dependency_overrides[api.require_classification_management] = lambda: context
    app.dependency_overrides[get_db] = lambda: db
    try:
        with (
            patch.object(
                api,
                "review_classification_run",
                new=AsyncMock(
                    return_value=ClassificationRunResult(run, (), None, ())
                ),
            ) as review,
            patch.object(
                api,
                "get_effective_classification",
                new=AsyncMock(return_value=effective),
            ),
        ):
            response = TestClient(app).post(
                f"/libraries/{library.slug}/classifications/runs/{run.id}/review",
                json={
                    "expected_run_status": "pending_review",
                    "expected_effective_decision_set_id": None,
                    "action": "change",
                    "primary_label_id": str(effective.decisions[0][1].id),
                    "secondary_label_ids": [],
                },
            )
        assert response.status_code == 200
        assert response.json()["state"] == "classified"
        assert response.json()["source"] == "manual"
        command = review.await_args.args[1]
        assert command.library_id == library.id and command.actor_user_id == user.id
        assert command.expected_effective_decision_set_id is None
        assert command.selection.primary_label_id == effective.decisions[0][1].id
        db.commit.assert_awaited_once()
        for forbidden in (
            "confidential investigation title",
            "model_config_hash",
            "input_fingerprint",
            "hashed_password",
            "organization_name",
        ):
            assert forbidden not in response.text.lower()
    finally:
        app.dependency_overrides.clear()


def test_review_state_change_rolls_back_with_stable_error():
    user, organization = _user(), _organization()
    library = _library(organization)
    context = api.ClassificationManagementContext(user, library)
    db = AsyncMock()
    app.dependency_overrides[api.require_classification_management] = lambda: context
    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.object(
            api,
            "review_classification_run",
            new=AsyncMock(
                side_effect=ClassificationDecisionError(
                    "classification_run_state_changed",
                    "raw provider secret must not escape",
                )
            ),
        ):
            response = TestClient(app).post(
                f"/libraries/{library.slug}/classifications/runs/{uuid.uuid4()}/review",
                json={
                    "expected_run_status": "pending_review",
                    "expected_effective_decision_set_id": None,
                    "action": "reject",
                    "primary_label_id": None,
                    "secondary_label_ids": [],
                },
            )
        assert response.status_code == 409
        assert response.json() == {"detail": "classification_run_state_changed"}
        assert "raw provider secret" not in response.text
        db.rollback.assert_awaited_once()
        db.commit.assert_not_awaited()
    finally:
        app.dependency_overrides.clear()


def test_manual_set_rejects_extra_fields_before_mutation():
    user, organization = _user(), _organization()
    library = _library(organization)
    context = api.ClassificationManagementContext(user, library)
    app.dependency_overrides[api.require_classification_management] = lambda: context
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with patch.object(api, "set_manual_classification", new=AsyncMock()) as setting:
            response = TestClient(app).put(
                f"/libraries/{library.slug}/classifications/documents/{uuid.uuid4()}",
                json={
                    "expected_effective_decision_set_id": None,
                    "primary_label_id": str(uuid.uuid4()),
                    "secondary_label_ids": [],
                    "source_text": "must not pass",
                },
            )
        assert response.status_code == 422
        setting.assert_not_awaited()
    finally:
        app.dependency_overrides.clear()


def test_normal_library_reader_can_read_effective_projection():
    user, organization = _user(), _organization()
    library = _library(organization)
    effective = _effective(library)
    db = AsyncMock()
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    try:
        with (
            patch.object(settings, "classification_decision_enabled", True),
            patch.object(settings, "organization_authorization_enabled", False),
            patch.object(
                deps_module,
                "load_active_library",
                new=AsyncMock(return_value=library),
            ),
            patch.object(deps_module, "has_permission", return_value=True),
            patch.object(
                api,
                "get_effective_classification",
                new=AsyncMock(return_value=effective),
            ) as get_effective,
        ):
            response = TestClient(app).get(
                f"/libraries/{library.slug}/classifications/documents/{effective.document.id}"
            )
        assert response.status_code == 200
        assert response.json()["document_id"] == str(effective.document.id)
        get_effective.assert_awaited_once_with(
            db,
            library_id=library.id,
            document_id=effective.document.id,
        )
    finally:
        app.dependency_overrides.clear()


def test_cross_library_document_failure_is_not_found_without_names_or_counts():
    user, organization = _user(), _organization()
    library = _library(organization)
    context = api.ClassificationManagementContext(user, library)
    db = AsyncMock()
    app.dependency_overrides[api.require_classification_management] = lambda: context
    app.dependency_overrides[get_db] = lambda: db
    try:
        with patch.object(
            api,
            "set_manual_classification",
            new=AsyncMock(
                side_effect=ClassificationDecisionError(
                    "classification_document_not_found",
                    "Other Library / secret.pdf / 42 results",
                )
            ),
        ):
            response = TestClient(app).put(
                f"/libraries/{library.slug}/classifications/documents/{uuid.uuid4()}",
                json={
                    "expected_effective_decision_set_id": None,
                    "primary_label_id": str(uuid.uuid4()),
                    "secondary_label_ids": [],
                },
            )
        assert response.status_code == 404
        assert response.json() == {"detail": "not found"}
        assert "secret.pdf" not in response.text and "42 results" not in response.text
    finally:
        app.dependency_overrides.clear()


def test_review_projection_hides_cross_taxonomy_label_identity():
    organization = _organization()
    library = _library(organization)
    effective = _effective(library)
    run = _run(library, effective)
    foreign_label = ClassificationLabel(
        id=uuid.uuid4(),
        taxonomy_version_id=uuid.uuid4(),
        key="foreign-secret",
        label="Foreign Secret",
        status="active",
        sort_order=0,
        created_at=NOW,
        updated_at=NOW,
    )
    proposal = DocumentClassificationProposal(
        id=uuid.uuid4(),
        run_id=run.id,
        label_id=foreign_label.id,
        role="primary",
        rank=0,
        confidence_micros=950_000,
        status="pending_review",
        reason_codes=["label_not_enabled"],
        created_at=NOW,
        updated_at=NOW,
    )
    projected = api._run_read(
        ClassificationReviewItem(run, ((proposal, foreign_label),))
    )
    assert projected.proposals[0].label_id is None
    assert projected.proposals[0].label_key is None
    assert projected.proposals[0].label is None
    assert "foreign-secret" not in projected.model_dump_json()
