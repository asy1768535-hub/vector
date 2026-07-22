from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.api import federated_retrieval as api
from app.auth.backend import current_cookie_user
from app.config import settings
from app.db import get_db
from app.main import app
from app.models.library import Library
from app.models.organization import Organization
from app.models.user import User
from app.services.federated_retrieval_contracts import (
    FederatedHit,
    FederatedLibraryTiming,
    FederatedRetrievalError,
    FederatedRetrievalResult,
    FederatedSourceProjection,
)
from app.services.library_compatibility_contracts import (
    CompatibilityAssessment,
    CompatibilityFingerprint,
    LibraryCompatibilityProfile,
    LibraryIncompatibility,
)
from app.services.organization_authorization import (
    OrganizationAdminContext,
    OrganizationAuthorizationError,
)


NOW = datetime(2026, 7, 22, 23, 30, tzinfo=timezone.utc)


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
        name="Organization",
        deployment_profile="hosted",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )


def _library(organization: Organization, slug: str) -> Library:
    return Library(
        id=uuid.uuid4(),
        organization_id=organization.id,
        slug=slug,
        name=f"Library {slug}",
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection=f"collection_{slug}",
        index_state="ready",
        created_at=NOW,
    )


def _result(organization: Organization, library: Library) -> FederatedRetrievalResult:
    profile = LibraryCompatibilityProfile(
        library=library,
        embedding=CompatibilityFingerprint("embedding-v1", "a" * 64, True),
        retrieval=CompatibilityFingerprint("retrieval-v1", "b" * 64, True),
        graph=None,
    )
    assessment = CompatibilityAssessment(
        organization_id=organization.id,
        channels=("text",),
        profiles=(profile,),
        compatible=True,
        incompatibilities=(),
    )
    return FederatedRetrievalResult(
        assessment=assessment,
        hits=(
            FederatedHit(
                rank=1,
                fusion_score=1 / 61,
                library_id=library.id,
                library_slug=library.slug,
                library_name=library.name,
                local_rank=1,
                local_score=0.9,
                title="Contract",
                content_excerpt="Termination clause",
                content_truncated=False,
                source=FederatedSourceProjection(
                    document_id="doc-1",
                    document_revision_id="rev-1",
                    chunk_id="chunk-1",
                    seq=1,
                ),
            ),
        ),
        timings=(FederatedLibraryTiming(library.slug, 1, 12),),
        total_elapsed_ms=14,
    )


def _body(slug: str) -> dict:
    return {
        "library_slugs": [slug],
        "query": "termination",
        "top_k": 1,
        "candidate_k": 3,
        "score_threshold": 0.0,
    }


def _admin_context(user: User, organization: Organization) -> OrganizationAdminContext:
    return OrganizationAdminContext(
        organization_id=organization.id,
        membership_id=uuid.uuid4(),
        user_id=user.id,
    )


def test_route_is_mounted_and_default_off():
    user = _user()
    organization = _organization()
    app.dependency_overrides[current_cookie_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with patch.object(settings, "federated_retrieval_enabled", False):
            response = TestClient(app).post(
                f"/organizations/{organization.id}/retrieval-tests",
                json=_body("legal"),
            )
        assert response.status_code == 404
        assert "/organizations/{organization_id}/retrieval-tests" in app.openapi()["paths"]
    finally:
        app.dependency_overrides.clear()


def test_route_requires_exact_organization_admin():
    user = _user()
    organization = _organization()
    run = AsyncMock()
    app.dependency_overrides[current_cookie_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with (
            patch.object(settings, "federated_retrieval_enabled", True),
            patch.object(
                api,
                "resolve_organization_admin",
                new=AsyncMock(
                    side_effect=OrganizationAuthorizationError(
                        "organization_admin_forbidden"
                    )
                ),
            ),
            patch.object(api, "run_federated_retrieval", new=run),
        ):
            response = TestClient(app).post(
                f"/organizations/{organization.id}/retrieval-tests",
                json=_body("legal"),
            )
        assert response.status_code == 403
        assert response.json() == {"detail": "forbidden"}
        run.assert_not_awaited()
    finally:
        app.dependency_overrides.clear()


def test_success_response_is_bounded_and_contains_exact_provenance():
    user = _user()
    organization = _organization()
    library = _library(organization, "legal")
    app.dependency_overrides[current_cookie_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with (
            patch.object(settings, "federated_retrieval_enabled", True),
            patch.object(
                api,
                "resolve_organization_admin",
                new=AsyncMock(return_value=_admin_context(user, organization)),
            ),
            patch.object(
                api,
                "run_federated_retrieval",
                new=AsyncMock(return_value=_result(organization, library)),
            ) as run,
        ):
            response = TestClient(app).post(
                f"/organizations/{organization.id}/retrieval-tests",
                json=_body(library.slug),
            )
        assert response.status_code == 200
        body = response.json()
        assert body["library_slugs"] == [library.slug]
        assert body["hits"][0]["source"]["document_revision_id"] == "rev-1"
        assert body["hits"][0]["source"]["chunk_id"] == "chunk-1"
        assert body["profiles"][0]["embedding_profile_sha256"] == "a" * 64
        assert body["timings"] == [
            {"library_slug": library.slug, "candidate_count": 1, "elapsed_ms": 12}
        ]
        for forbidden in (
            "embedding_base_url",
            "qdrant_collection",
            "source_config",
            "api_key",
            "password",
        ):
            assert forbidden not in response.text.lower()
        run.assert_awaited_once()
    finally:
        app.dependency_overrides.clear()


def test_incompatible_and_branch_failure_have_stable_bounded_errors():
    user = _user()
    organization = _organization()
    admin = _admin_context(user, organization)
    app.dependency_overrides[current_cookie_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with (
            patch.object(settings, "federated_retrieval_enabled", True),
            patch.object(
                api,
                "resolve_organization_admin",
                new=AsyncMock(return_value=admin),
            ),
            patch.object(
                api,
                "run_federated_retrieval",
                new=AsyncMock(
                    side_effect=FederatedRetrievalError(
                        "federated_scope_incompatible",
                        incompatibilities=(
                            LibraryIncompatibility(
                                "legal",
                                ("embedding_profile_mismatch",),
                            ),
                        ),
                    )
                ),
            ),
        ):
            conflict = TestClient(app).post(
                f"/organizations/{organization.id}/retrieval-tests",
                json=_body("legal"),
            )
        assert conflict.status_code == 409
        assert conflict.json()["detail"] == {
            "code": "federated_scope_incompatible",
            "incompatibilities": [
                {
                    "library_slug": "legal",
                    "reason_codes": ["embedding_profile_mismatch"],
                }
            ],
        }

        with (
            patch.object(settings, "federated_retrieval_enabled", True),
            patch.object(
                api,
                "resolve_organization_admin",
                new=AsyncMock(return_value=admin),
            ),
            patch.object(
                api,
                "run_federated_retrieval",
                new=AsyncMock(
                    side_effect=FederatedRetrievalError(
                        "federated_branch_failed",
                        message="provider secret diagnostic",
                    )
                ),
            ),
        ):
            failed = TestClient(app).post(
                f"/organizations/{organization.id}/retrieval-tests",
                json=_body("legal"),
            )
        assert failed.status_code == 502
        assert failed.json() == {"detail": "federated_branch_failed"}
        assert "provider secret diagnostic" not in failed.text
    finally:
        app.dependency_overrides.clear()


def test_request_bounds_reject_before_execution():
    user = _user()
    organization = _organization()
    run = AsyncMock()
    app.dependency_overrides[current_cookie_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with (
            patch.object(settings, "federated_retrieval_enabled", True),
            patch.object(
                api,
                "resolve_organization_admin",
                new=AsyncMock(return_value=_admin_context(user, organization)),
            ),
            patch.object(api, "run_federated_retrieval", new=run),
        ):
            body = _body("legal")
            body.update({"top_k": 10, "candidate_k": 5, "extra": "forbidden"})
            response = TestClient(app).post(
                f"/organizations/{organization.id}/retrieval-tests",
                json=body,
            )
        assert response.status_code == 422
        run.assert_not_awaited()
    finally:
        app.dependency_overrides.clear()
