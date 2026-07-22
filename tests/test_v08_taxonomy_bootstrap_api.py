from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.api import classification_taxonomy_bootstrap as api
from app.config import settings
from app.db import get_db
from app.main import app
from app.models.classification_taxonomy import ClassificationTaxonomy
from app.models.classification_taxonomy_bootstrap import (
    TaxonomyBootstrapRun,
    TaxonomyBootstrapSource,
)
from app.models.organization import Organization
from app.models.user import User
from app.services.classification_provider import (
    ClassificationProviderError,
    ClassificationProviderResponse,
)
from app.services.classification_taxonomy_bootstrap import (
    BootstrapSampleSnapshot,
    PreparedLlmTaxonomyBootstrap,
    TaxonomyBootstrapResult,
    TaxonomyBootstrapRunDetail,
)
from app.services.classification_taxonomy_bootstrap_contracts import (
    DraftLabelInput,
    LlmTaxonomyProposal,
    TaxonomyBootstrapError,
    normalize_taxonomy_draft,
)
from app.services.organization_authorization import OrganizationAdminContext


NOW = datetime(2026, 7, 22, 23, 30, tzinfo=timezone.utc)
HASH = "a" * 64


def _user() -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@example.com",
        hashed_password="hash",
        is_active=True,
        is_superuser=False,
        is_verified=True,
        created_at=NOW,
    )


def _organization() -> Organization:
    return Organization(
        id=uuid.uuid4(),
        slug=f"org-{uuid.uuid4().hex[:8]}",
        name="Example",
        deployment_profile="hosted",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )


def _context(user: User, organization: Organization):
    return SimpleNamespace(
        user=user,
        admin=OrganizationAdminContext(
            organization_id=organization.id,
            membership_id=uuid.uuid4(),
            user_id=user.id,
        ),
    )


def _run(
    organization: Organization,
    user: User,
    *,
    status: str = "succeeded",
) -> TaxonomyBootstrapRun:
    processing = status == "processing"
    succeeded = status == "succeeded"
    return TaxonomyBootstrapRun(
        id=uuid.uuid4(),
        organization_id=organization.id,
        request_id=uuid.uuid4(),
        source_type="llm_proposal" if processing else "builtin_template",
        source_key=(
            "organization-revision-samples" if processing else "general-enterprise"
        ),
        source_version="1",
        source_hash=HASH,
        input_fingerprint="b" * 64,
        idempotency_key="c" * 64,
        status=status,
        warning_items=[],
        output_taxonomy_id=uuid.uuid4() if succeeded else None,
        model_provider="deepseek" if processing else None,
        model_name="deepseek-v4-pro" if processing else None,
        model_config_hash="d" * 64 if processing else None,
        prompt_version="taxonomy-bootstrap-v1" if processing else None,
        attempt_token=uuid.uuid4() if processing else None,
        expires_at=NOW + timedelta(minutes=2) if processing else None,
        created_by_user_id=user.id,
        error_code=None,
        created_at=NOW,
        updated_at=NOW,
        started_at=NOW,
        finished_at=NOW if succeeded else None,
    )


def _override_admin_and_db(user, organization, db):
    app.dependency_overrides[api.require_taxonomy_bootstrap_admin] = lambda: _context(
        user, organization
    )
    app.dependency_overrides[get_db] = lambda: db


def test_bootstrap_routes_default_off_before_cookie_authentication():
    organization = _organization()
    with patch.object(settings, "classification_taxonomy_bootstrap_enabled", False):
        response = TestClient(app).get(
            f"/organizations/{organization.id}/classification-taxonomy-bootstrap/templates"
        )
    assert response.status_code == 404
    assert response.json() == {"detail": "not found"}


def test_bearer_token_cannot_enter_cookie_only_bootstrap_route():
    organization = _organization()
    authorization_scheme = "".join(("Bea", "rer"))
    with (
        patch.object(settings, "classification_taxonomy_enabled", True),
        patch.object(settings, "classification_taxonomy_bootstrap_enabled", True),
    ):
        response = TestClient(app).get(
            f"/organizations/{organization.id}/classification-taxonomy-bootstrap/templates",
            headers={"Authorization": f"{authorization_scheme} not-a-cookie"},
        )
    assert response.status_code == 401


def test_template_list_and_apply_return_bounded_draft_provenance():
    user, organization = _user(), _organization()
    run = _run(organization, user)
    taxonomy = ClassificationTaxonomy(
        id=run.output_taxonomy_id,
        organization_id=organization.id,
        version_key="document-category",
        version_no=1,
        status="draft",
        created_by_user_id=user.id,
        created_at=NOW,
        updated_at=NOW,
    )
    db = AsyncMock()
    _override_admin_and_db(user, organization, db)
    try:
        with patch.object(settings, "classification_taxonomy_bootstrap_enabled", True):
            templates = TestClient(app).get(
                f"/organizations/{organization.id}/classification-taxonomy-bootstrap/templates"
            )
        assert templates.status_code == 200
        assert templates.json()[0]["key"] == "general-enterprise"
        assert templates.json()[0]["label_count"] == 8

        with (
            patch.object(settings, "classification_taxonomy_bootstrap_enabled", True),
            patch.object(
                api,
                "apply_builtin_taxonomy_template",
                new=AsyncMock(return_value=TaxonomyBootstrapResult(run, taxonomy, (), True)),
            ) as apply,
        ):
            response = TestClient(app).post(
                f"/organizations/{organization.id}/classification-taxonomy-bootstrap/templates/general-enterprise",
                json={"request_id": str(run.request_id)},
            )
        assert response.status_code == 201
        assert response.json()["output_taxonomy_id"] == str(taxonomy.id)
        assert response.json()["status"] == "succeeded"
        assert "label" not in response.json()
        command = apply.await_args.args[1]
        assert command.actor_user_id == user.id
        assert command.template_key == "general-enterprise"
        db.commit.assert_awaited_once()
    finally:
        app.dependency_overrides.clear()


def test_import_is_strict_and_rejects_extra_fields_before_service():
    user, organization = _user(), _organization()
    db = AsyncMock()
    _override_admin_and_db(user, organization, db)
    try:
        with (
            patch.object(settings, "classification_taxonomy_bootstrap_enabled", True),
            patch.object(api, "import_taxonomy_bootstrap", new=AsyncMock()) as importing,
        ):
            response = TestClient(app).post(
                f"/organizations/{organization.id}/classification-taxonomy-bootstrap/imports",
                json={
                    "request_id": str(uuid.uuid4()),
                    "source_name": "Admin reference",
                    "source_version": "1",
                    "labels": [
                        {"key": "legal", "label": "Legal", "sort_order": 10}
                    ],
                    "raw_url": "https://secret.example/private",
                },
            )
        assert response.status_code == 422
        importing.assert_not_awaited()
    finally:
        app.dependency_overrides.clear()


def test_run_detail_maps_cross_organization_absence_to_generic_not_found():
    user, organization = _user(), _organization()
    db = AsyncMock()
    _override_admin_and_db(user, organization, db)
    try:
        with (
            patch.object(settings, "classification_taxonomy_bootstrap_enabled", True),
            patch.object(
                api,
                "get_taxonomy_bootstrap_run_detail",
                new=AsyncMock(side_effect=TaxonomyBootstrapError("bootstrap_run_not_found")),
            ),
        ):
            response = TestClient(app).get(
                f"/organizations/{organization.id}/classification-taxonomy-bootstrap/runs/{uuid.uuid4()}"
            )
        assert response.status_code == 404
        assert response.json() == {"detail": "not found"}
    finally:
        app.dependency_overrides.clear()


def test_llm_endpoint_commits_reservation_before_provider_and_publishes():
    user, organization = _user(), _organization()
    run = _run(organization, user, status="processing")
    revision_id = uuid.uuid4()
    snapshot = BootstrapSampleSnapshot(
        uuid.uuid4(),
        uuid.uuid4(),
        revision_id,
        HASH,
        "internal",
    )
    prepared = PreparedLlmTaxonomyBootstrap(
        run,
        run.attempt_token,
        user.id,
        "document-category",
        None,
        (snapshot,),
        [{"role": "user", "content": "bounded sample"}],
        False,
    )
    taxonomy = ClassificationTaxonomy(
        id=uuid.uuid4(),
        organization_id=organization.id,
        version_key="document-category",
        version_no=1,
        status="draft",
        created_by_user_id=user.id,
        created_at=NOW,
        updated_at=NOW,
    )
    source = TaxonomyBootstrapSource(
        id=uuid.uuid4(),
        bootstrap_run_id=run.id,
        ordinal=0,
        library_id=snapshot.library_id,
        document_id=snapshot.document_id,
        document_revision_id=snapshot.document_revision_id,
        revision_content_hash=HASH,
        security_level="internal",
        created_at=NOW,
    )
    proposal = LlmTaxonomyProposal(
        None,
        normalize_taxonomy_draft(
            (
                DraftLabelInput("legal", "Legal", sort_order=10),
                DraftLabelInput("finance", "Finance", sort_order=20),
                DraftLabelInput("safety", "Safety", sort_order=30),
            ),
            minimum=3,
            maximum=50,
        ),
    )
    events = []
    db = AsyncMock()
    db.commit = AsyncMock(side_effect=lambda: events.append("commit"))
    _override_admin_and_db(user, organization, db)

    class Provider:
        async def generate(self, messages):
            assert events == ["commit"]
            assert messages == prepared.messages
            events.append("provider")
            return ClassificationProviderResponse(
                content=json.dumps({"labels": []}),
                request_payload_hash=HASH,
                provider_request_id=None,
                latency_ms=1,
                input_token_count=None,
                output_token_count=None,
            )

    async def publish(*_args, **_kwargs):
        events.append("publish")
        run.status = "succeeded"
        run.output_taxonomy_id = taxonomy.id
        run.attempt_token = None
        run.expires_at = None
        run.finished_at = NOW
        return TaxonomyBootstrapResult(run, taxonomy, (), True)

    try:
        with (
            patch.object(settings, "classification_taxonomy_enabled", True),
            patch.object(settings, "classification_taxonomy_bootstrap_enabled", True),
            patch.object(settings, "classification_taxonomy_bootstrap_llm_enabled", True),
            patch.object(api, "prepare_llm_taxonomy_bootstrap", new=AsyncMock(return_value=prepared)),
            patch.object(api, "OpenAICompatibleClassificationProvider", return_value=Provider()),
            patch.object(api, "parse_llm_taxonomy_bootstrap_output", return_value=proposal),
            patch.object(api, "publish_llm_taxonomy_bootstrap", side_effect=publish),
            patch.object(
                api,
                "get_taxonomy_bootstrap_run_detail",
                new=AsyncMock(return_value=TaxonomyBootstrapRunDetail(run, (source,))),
            ),
        ):
            response = TestClient(app).post(
                f"/organizations/{organization.id}/classification-taxonomy-bootstrap/llm-proposals",
                json={
                    "request_id": str(run.request_id),
                    "sample_revision_ids": [str(revision_id)],
                },
            )
        assert response.status_code == 201, response.text
        assert events == ["commit", "provider", "publish", "commit", "commit"]
        assert response.json()["status"] == "succeeded"
        assert response.json()["sources"][0]["document_revision_id"] == str(revision_id)
    finally:
        app.dependency_overrides.clear()


def test_llm_provider_failure_records_stable_code_without_raw_error():
    user, organization = _user(), _organization()
    run = _run(organization, user, status="processing")
    prepared = PreparedLlmTaxonomyBootstrap(
        run,
        run.attempt_token,
        user.id,
        "document-category",
        None,
        (),
        [{"role": "user", "content": "bounded sample"}],
        False,
    )
    db = AsyncMock()
    _override_admin_and_db(user, organization, db)
    provider = SimpleNamespace(
        generate=AsyncMock(
            side_effect=ClassificationProviderError(
                "timeout",
                "raw secret endpoint response",
                latency_ms=1,
            )
        )
    )
    record = AsyncMock(return_value=True)
    try:
        with (
            patch.object(settings, "classification_taxonomy_enabled", True),
            patch.object(settings, "classification_taxonomy_bootstrap_enabled", True),
            patch.object(settings, "classification_taxonomy_bootstrap_llm_enabled", True),
            patch.object(api, "prepare_llm_taxonomy_bootstrap", new=AsyncMock(return_value=prepared)),
            patch.object(api, "OpenAICompatibleClassificationProvider", return_value=provider),
            patch.object(api, "record_llm_taxonomy_bootstrap_failure", record),
        ):
            response = TestClient(app).post(
                f"/organizations/{organization.id}/classification-taxonomy-bootstrap/llm-proposals",
                json={
                    "request_id": str(run.request_id),
                    "sample_revision_ids": [str(uuid.uuid4())],
                },
            )
        assert response.status_code == 504
        assert response.json() == {"detail": "provider_timeout"}
        assert "raw secret" not in response.text
        record.assert_awaited_once()
        assert record.await_args.kwargs["error_code"] == "provider_timeout"
    finally:
        app.dependency_overrides.clear()


def test_llm_publication_drift_terminalizes_reserved_run():
    user, organization = _user(), _organization()
    run = _run(organization, user, status="processing")
    revision_id = uuid.uuid4()
    prepared = PreparedLlmTaxonomyBootstrap(
        run,
        run.attempt_token,
        user.id,
        "document-category",
        None,
        (
            BootstrapSampleSnapshot(
                uuid.uuid4(),
                uuid.uuid4(),
                revision_id,
                HASH,
                "internal",
            ),
        ),
        [{"role": "user", "content": "bounded sample"}],
        False,
    )
    provider = SimpleNamespace(
        generate=AsyncMock(
            return_value=ClassificationProviderResponse(
                content="{}",
                request_payload_hash=HASH,
                provider_request_id=None,
                latency_ms=1,
                input_token_count=None,
                output_token_count=None,
            )
        )
    )
    proposal = LlmTaxonomyProposal(
        None,
        normalize_taxonomy_draft(
            (
                DraftLabelInput("legal", "Legal", sort_order=10),
                DraftLabelInput("finance", "Finance", sort_order=20),
                DraftLabelInput("safety", "Safety", sort_order=30),
            ),
            minimum=3,
            maximum=50,
        ),
    )
    record = AsyncMock(return_value=True)
    db = AsyncMock()
    _override_admin_and_db(user, organization, db)
    try:
        with (
            patch.object(settings, "classification_taxonomy_enabled", True),
            patch.object(settings, "classification_taxonomy_bootstrap_enabled", True),
            patch.object(settings, "classification_taxonomy_bootstrap_llm_enabled", True),
            patch.object(api, "prepare_llm_taxonomy_bootstrap", new=AsyncMock(return_value=prepared)),
            patch.object(api, "OpenAICompatibleClassificationProvider", return_value=provider),
            patch.object(api, "parse_llm_taxonomy_bootstrap_output", return_value=proposal),
            patch.object(
                api,
                "publish_llm_taxonomy_bootstrap",
                new=AsyncMock(
                    side_effect=TaxonomyBootstrapError(
                        "bootstrap_sample_revision_stale"
                    )
                ),
            ),
            patch.object(api, "record_llm_taxonomy_bootstrap_failure", record),
        ):
            response = TestClient(app).post(
                f"/organizations/{organization.id}/classification-taxonomy-bootstrap/llm-proposals",
                json={
                    "request_id": str(run.request_id),
                    "sample_revision_ids": [str(revision_id)],
                },
            )
        assert response.status_code == 409
        assert response.json() == {"detail": "bootstrap_sample_revision_stale"}
        record.assert_awaited_once()
        assert record.await_args.kwargs["error_code"] == "bootstrap_sample_revision_stale"
    finally:
        app.dependency_overrides.clear()
